"""Official product-sitemap change candidates; never product or retirement facts."""
from collections import Counter
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3

from .adapters import adapter_for
from .inventory import atomic_json, run_inventory, utc_now
from .product_catalog import sitemap_product_category
from .network import ProductFetcher


def sitemap_profile(company_id):
    profile = dict(adapter_for(company_id).profile)
    if company_id == 'supermicro':
        # These official product sitemaps include their published locale URLs.
        # Do not traverse support, news, image, FAQ or the resource archive.
        profile['sitemaps'] = [s for s in profile['sitemaps'] if s['role'] in {'system', 'chassis', 'motherboard', 'accessories'}]
        profile.pop('sitemap_index_select', None)
    if not profile.get('sitemaps') and not profile.get('sitemap_index'):
        raise ValueError(company_id + ' declares no official product sitemap; discover through bounded collect from its category pages')
    profile['known_gaps'] = ['Product sitemap entries are URL candidates, not verified products. Partial omissions never retire products.']
    return profile


def sync_map(root, company_id, *, fetcher=None, known_catalog=None):
    """Read only sitemap metadata and return a reviewable new/changed URL plan.

    Existing catalog URLs establish a baseline on the first observation, avoiding
    a mass refresh of migrated products. lastmod is a vendor change claim, not a
    content hash; collect must still conditionally fetch and verify actual bytes.
    """
    root = Path(root).expanduser()
    adapter = adapter_for(company_id, known_catalog)
    base = root / 'acquisition' / company_id
    base.mkdir(parents=True, exist_ok=True)
    with (base / 'map.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        profile = sitemap_profile(company_id)
        fetcher = fetcher or ProductFetcher(profile)
        inventory = run_inventory(profile, root, fetcher=fetcher)
        receipts = getattr(fetcher, 'policy_receipts', [])
        if receipts:
            policy_sha = hashlib.sha256(json.dumps(receipts, sort_keys=True).encode()).hexdigest()
            policy_path = base / 'policies' / (policy_sha + '.json')
            atomic_json(policy_path, receipts)
            inventory['robots_receipt'] = str(policy_path.relative_to(root))
        found = {}
        if inventory.get('url_manifest'):
            for line in (root / inventory['url_manifest']).read_text().splitlines():
                row = json.loads(line)
                url = adapter.normalize(row['url'])
                if not url or not adapter.page_allowed(url):
                    continue
                if company_id == 'nvidia' and sitemap_product_category(url) is None:
                    continue
                claims = sorted({s['lastmod_claim'] for s in row.get('sources', []) if s.get('lastmod_claim')})
                found[url] = {'lastmod_claims': claims, 'sitemap_sources': row.get('sources', [])}
        known_urls = {s.get('source_url') for s in (known_catalog or {}).get('sources', [])}
        for p in (known_catalog or {}).get('products', []):
            known_urls.update([p.get('source_url'), p.get('product_url')])
            known_urls.update(r.get('url') for r in p.get('official_pages', []))
        db = sqlite3.connect(base / 'product-map.sqlite3')
        db.row_factory = sqlite3.Row
        try:
            db.executescript('''CREATE TABLE IF NOT EXISTS candidates(
              candidate_id TEXT PRIMARY KEY,url TEXT UNIQUE,lastmod_claims TEXT,payload TEXT,
              first_seen TEXT,last_seen TEXT,state TEXT,pending_reason TEXT);
              CREATE TABLE IF NOT EXISTS map_runs(id TEXT PRIMARY KEY,observed_at TEXT,status TEXT,report TEXT);
              CREATE TABLE IF NOT EXISTS map_events(id INTEGER PRIMARY KEY,url TEXT,run_id TEXT,status TEXT,payload TEXT);
            ''')
            if 'pending_reason' not in {r[1] for r in db.execute('PRAGMA table_info(candidates)')}:
                db.execute('ALTER TABLE candidates ADD COLUMN pending_reason TEXT')
            previous = {r['url']: dict(r) for r in db.execute('SELECT * FROM candidates')}
            observed = utc_now()
            counts, plan = Counter(), []
            for url, payload in sorted(found.items()):
                old = previous.get(url)
                if old is None:
                    status = 'unchanged' if url in known_urls else 'new'
                else:
                    status = 'changed' if json.loads(old['lastmod_claims']) != payload['lastmod_claims'] else 'unchanged'
                counts[status] += 1
                candidate_id = company_id + '-url-' + hashlib.sha256(url.encode()).hexdigest()[:20]
                pending_reason = status if status in {'new', 'changed'} else old.get('pending_reason') if old else None
                data = {**payload, 'url': url, 'candidate_id': candidate_id,
                        'change_status': status, 'pending_reason': pending_reason, 'pending_until_reviewed_or_collected': bool(pending_reason), 'known_product_source': url in known_urls,
                        'identity_status': 'unverified_sitemap_url_candidate'}
                db.execute('''INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
                  lastmod_claims=excluded.lastmod_claims,payload=excluded.payload,last_seen=excluded.last_seen,state=excluded.state,pending_reason=excluded.pending_reason''',
                           (candidate_id, url, json.dumps(payload['lastmod_claims']), json.dumps(data, ensure_ascii=False),
                            old['first_seen'] if old else observed, observed, 'observed', pending_reason))
                if pending_reason:
                    plan.append(data)
                if status in {'new', 'changed'}:
                    db.execute('INSERT INTO map_events(url,run_id,status,payload) VALUES(?,?,?,?)',
                               (url, inventory['run_id'], status, json.dumps(data, ensure_ascii=False)))
            complete = inventory.get('status') == 'sitemaps_complete'
            if complete:
                for url in sorted(set(previous) - set(found)):
                    counts['missing_review'] += 1
                    db.execute("UPDATE candidates SET state='missing_review' WHERE url=?", (url,))
                    if previous[url]['state'] != 'missing_review':
                        db.execute('INSERT INTO map_events(url,run_id,status,payload) VALUES(?,?,?,?)',
                                   (url, inventory['run_id'], 'missing_review', '{}'))
            for url in sorted(set(previous) - set(found)):
                if previous[url].get('pending_reason'):
                    entry = json.loads(previous[url]['payload'])
                    plan.append({**entry, 'change_status': 'missing_review' if complete else 'unobserved_in_incomplete_run', 'pending_until_reviewed_or_collected': True})
            report = {'company_id': company_id, 'run_id': inventory['run_id'], 'observed_at': observed,
                      'inventory_status': inventory.get('status'), 'sitemaps_complete': complete,
                      'counts': {k: counts[k] for k in ['new', 'changed', 'unchanged', 'missing_review']},
                      'candidate_urls_observed': len(found), 'pending_candidates': len(plan), 'plan': plan,
                      'errors': inventory.get('errors', []), 'source_manifest': inventory.get('url_manifest'),
                      'product_completeness_claimed': False, 'products_retired': 0,
                      'policy': 'Review candidate scope against a target before collecting; new/lastmod changes remain pending until explicitly acknowledged after collection or review. Missing URLs require review and never retire products.'}
            db.execute('INSERT INTO map_runs VALUES(?,?,?,?)', (inventory['run_id'], observed,
                       inventory.get('status'), json.dumps(report, ensure_ascii=False)))
            db.commit()
            atomic_json(base / 'map-plan.json', report)
            return report
        finally:
            db.close()


def acknowledge_map(root, company_id, urls):
    """Explicit operator/collector acknowledgement; never infers completion from lastmod."""
    adapter_for(company_id)  # Validate company/path before opening local state.
    base = Path(root).expanduser() / 'acquisition' / company_id
    path = base / 'product-map.sqlite3'
    if not path.exists():
        return {'acknowledged': 0}
    with (base / 'map.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = sqlite3.connect(path)
        try:
            count = 0
            for url in sorted(set(urls)):
                result = db.execute('UPDATE candidates SET pending_reason=NULL WHERE url=? AND pending_reason IS NOT NULL', (url,))
                count += result.rowcount
                if result.rowcount:
                    db.execute('INSERT INTO map_events(url,run_id,status,payload) VALUES(?,?,?,?)',
                               (url, utc_now(), 'explicit_acknowledgement', '{}'))
            db.commit()
            current_plan = base / 'map-plan.json'
            if current_plan.exists():
                report = json.loads(current_plan.read_text())
                report['plan'] = [p for p in report.get('plan', []) if p['url'] not in set(urls)]
                report['pending_candidates'] = len(report['plan'])
                report['acknowledged_at'] = utc_now()
                atomic_json(current_plan, report)
            return {'acknowledged': count}
        finally:
            db.close()
