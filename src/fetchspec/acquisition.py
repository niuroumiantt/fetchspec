"""Bounded product acquisition with resumable source observations and native tables.

This ledger is candidate evidence, never a research fact or adoption database.
"""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from urllib.error import HTTPError

from .adapters import adapter_for
from .adapters.base import language
from .company import document_kind, is_transient_error
from .extraction import extract_document
from .inventory import atomic_bytes, atomic_json, utc_now
from .network import ProductFetcher


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@contextmanager
def _ledger(root, company_id):
    base = root / 'acquisition' / company_id
    base.mkdir(parents=True, exist_ok=True)
    with (base / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = sqlite3.connect(base / 'sources.sqlite3')
        db.row_factory = sqlite3.Row
        db.executescript('''
          PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS frontier(url TEXT PRIMARY KEY, parent TEXT, depth INTEGER,
            role TEXT, state TEXT, priority INTEGER DEFAULT 0, etag TEXT, modified TEXT, latest_sha TEXT, error TEXT);
          CREATE TABLE IF NOT EXISTS source_versions(url TEXT, sha TEXT, payload TEXT, PRIMARY KEY(url,sha));
          CREATE TABLE IF NOT EXISTS links(parent TEXT, child TEXT, PRIMARY KEY(parent,child));
          CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY, url TEXT, observed_at TEXT,
            status INTEGER, sha TEXT, change_status TEXT);
          CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY, payload TEXT);
          CREATE TABLE IF NOT EXISTS source_products(url TEXT, product_id TEXT, PRIMARY KEY(url,product_id));
          CREATE TABLE IF NOT EXISTS source_owner_history(id INTEGER PRIMARY KEY,url TEXT,product_id TEXT,changed_at TEXT,reason TEXT);
          CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY, url TEXT, occurred_at TEXT,
            error TEXT, retryable INTEGER);
        ''')
        if 'priority' not in {r[1] for r in db.execute('PRAGMA table_info(frontier)')}:
            db.execute('ALTER TABLE frontier ADD COLUMN priority INTEGER DEFAULT 0')
        try:
            yield db
        finally:
            db.commit()
            db.close()


def _enqueue(db, url, parent='', depth=0, role='product', product_id=None, priority=0):
    db.execute("INSERT OR IGNORE INTO frontier(url,parent,depth,role,state,priority) VALUES(?,?,?,?,'pending',?)",
               (url, parent, depth, role, priority))
    if parent:
        db.execute('INSERT OR IGNORE INTO links VALUES(?,?)', (parent, url))
    if product_id:
        db.execute('INSERT OR IGNORE INTO source_products VALUES(?,?)', (url, product_id))


def _source(db, url, sha):
    row = db.execute('SELECT payload FROM source_versions WHERE url=? AND sha=?', (url, sha)).fetchone()
    return json.loads(row[0]) if row else None


def _product(db, product_id):
    row = db.execute('SELECT payload FROM products WHERE id=?', (product_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _save_product(db, value, baseline=None):
    previous = baseline or _product(db, value['id'])
    if previous:
        value['observed_at'] = previous['observed_at'] if _semantic(previous) == _semantic(value) else utc_now()
    db.execute('INSERT OR REPLACE INTO products VALUES(?,?)', (value['id'], _dump(value)))


def _semantic(value):
    if isinstance(value, dict):
        return {k: _semantic(v) for k, v in value.items()
                if k not in {'observed_at', 'generated_at', 'map_change_status', 'checked_at'}}
    return [_semantic(v) for v in value] if isinstance(value, list) else value


def _table_rank(table):
    method = table.get('extraction_method', table.get('method', ''))
    if 'pdf' in method:
        return 1
    if 'office' in method:
        return 2
    return 0


def _prefer_existing(db, owners, rank, url, refresh=False):
    products = [_product(db, owner) for owner in owners]
    def preferred(product):
        for table in (product or {}).get('tables', []):
            table_rank = _table_rank(table)
            if table_rank < rank:
                return True
            if table_rank == rank and not (refresh and any(r.get('url') == url for r in table.get('source_refs', []))):
                return True
        return False
    return bool(products) and all(preferred(product) for product in products)


def _has_tables(db, url, rank, refresh=False):
    owners = [r[0] for r in db.execute('SELECT product_id FROM source_products WHERE url=?', (url,))]
    return _prefer_existing(db, owners, rank, url, refresh)


def _get(fetcher, url, headers):
    for attempt in range(3):
        try:
            return fetcher.get(url, headers=headers)
        except Exception as exc:
            if isinstance(exc, HTTPError) and exc.code in {304, 404, 410, 429}:
                raise
            if not is_transient_error(type(exc).__name__ + ': ' + str(exc)) or attempt == 2:
                raise
            time.sleep(attempt + 1)


def collect(root, company_id, urls, *, fetcher=None, refresh=False, max_pages=20, known_catalog=None, reparse=False):
    """Return a catalog plus acquisition_report; urls are explicit official product seeds.

    Ordinary reruns resume pending/transient failures without re-downloading done URLs.
    refresh conditionally revalidates the explicit seeds. Directory expansion is capped
    at depth two and max_pages requests; non-directory pages only follow spec links.
    known_catalog anchors NVIDIA identities to the current imported product map.
    reparse verifies existing scoped snapshots and updates extraction without HTTP.
    """
    if refresh and reparse:
        raise ValueError('refresh and reparse are mutually exclusive')
    if max_pages < 1:
        raise ValueError('max_pages must be positive')
    root = Path(root).expanduser()
    adapter = adapter_for(company_id, known_catalog)
    known_products = {p['id']: p for p in (known_catalog or {}).get('products', [])}
    seeds = []
    for raw in urls:
        url = adapter.normalize(raw)
        if not url or not adapter.page_allowed(url):
            raise ValueError('seed is not an allowed English/Chinese official product page: ' + raw)
        seeds.append(url)
    if not seeds:
        raise ValueError('at least one explicit product seed is required')
    profile = {**adapter.profile, 'max_xml_bytes': 128 * 1024 * 1024}
    fetcher = fetcher or ProductFetcher(profile)
    report = {'attempted': 0, 'reparsed': 0, 'new_versions': 0, 'unchanged': 0, 'errors': [], 'complete': False}
    with _ledger(root, company_id) as db:
        # Known products provide parent/entity identity only; don't overwrite the
        # collection ledger with another source of current product state.
        promoted_urls = set()
        for url in seeds:
            prior_seed = db.execute('SELECT role,latest_sha FROM frontier WHERE url=?', (url,)).fetchone()
            _enqueue(db, url)
            if prior_seed and prior_seed['role'] != 'product':
                for owner in db.execute('SELECT product_id FROM source_products WHERE url=?', (url,)).fetchall():
                    db.execute('INSERT INTO source_owner_history(url,product_id,changed_at,reason) VALUES(?,?,?,?)',
                               (url, owner[0], utc_now(), 'explicit_product_seed_replaces_inferred_source_owner'))
                db.execute('DELETE FROM source_products WHERE url=?', (url,))
                db.execute("UPDATE frontier SET role='product',parent='',depth=0,priority=0,state='pending',error=NULL WHERE url=?", (url,))
                if prior_seed['latest_sha'] and not refresh:
                    promoted_urls.add(url)
            if refresh:
                db.execute("UPDATE frontier SET state='pending',error=NULL WHERE url=?", (url,))
        scope = _scope(db, seeds)
        for url in scope:
            if reparse:
                db.execute("UPDATE frontier SET state='pending',error=NULL WHERE url=? AND latest_sha IS NOT NULL", (url,))
            db.execute("UPDATE frontier SET state='pending',error=NULL WHERE state='retryable_error' AND url=?", (url,))
        db.commit()
        pending = any(r['state'] == 'pending' and r['url'] in scope and r['url'] not in promoted_urls for r in db.execute('SELECT url,state FROM frontier'))
        if pending and not reparse:
            try:
                fetcher.prepare_robots()
            except Exception as exc:
                report['errors'].append({'stage': 'robots', 'error': type(exc).__name__ + ': ' + str(exc)})
                return _catalog(db, company_id, report, _scope(db, seeds), known_catalog, root)
        while report['attempted'] < max_pages:
            scope = _scope(db, seeds)
            row = next((r for r in db.execute("""SELECT * FROM frontier WHERE state='pending'
                ORDER BY CASE role WHEN 'product' THEN 0 WHEN 'specification' THEN 1 ELSE 2 END,priority,depth,url""") if r["url"] in scope and (not reparse or r["latest_sha"])), None)
            if row is None:
                break
            if not reparse and row['role'] == 'attachment' and _has_tables(db, row['url'], row['priority'], refresh):
                db.execute("UPDATE frontier SET state='deferred_better_native_source' WHERE url=?", (row['url'],))
                continue
            report['attempted'] += 1
            offline = reparse or row['url'] in promoted_urls
            prior = _source(db, row['url'], row['latest_sha']) if row['latest_sha'] else None
            headers = {}
            if row['etag']:
                headers['If-None-Match'] = row['etag']
            if row['modified']:
                headers['If-Modified-Since'] = row['modified']
            try:
                try:
                    if offline:
                        body = (root / prior['snapshot_path']).read_bytes()
                        if _sha(body) != prior['sha256']:
                            raise ValueError('cached source SHA mismatch')
                        meta = prior['http']
                        report['reparsed'] += 1
                    else:
                        body, meta = _get(fetcher, row['url'], headers)
                except HTTPError as exc:
                    if exc.code != 304 or prior is None:
                        raise
                    body = (root / prior['snapshot_path']).read_bytes()
                    if _sha(body) != prior['sha256']:
                        raise ValueError('cached source SHA mismatch')
                    meta = {**prior['http'], 'status': 304}
                final = meta.get('final_url', row['url'])
                if not adapter.normalize(final):
                    raise ValueError('redirect outside allowed official English/Chinese scope')
                kind = document_kind(body, final, meta.get('content_type', ''))
                if kind is None and ('html' in meta.get('content_type', '').lower() or body.lstrip().lower().startswith((b'<!doctype html', b'<html'))):
                    kind = 'html'
                if kind == 'html' and not adapter.page_allowed(final):
                    raise ValueError('redirect outside official product page scope')
                if kind is None and row['role'] == 'specification' and adapter.component_allowed(final, meta.get('content_type', '')):
                    kind = 'json'  # an official data component the product page itself names
                if kind is None:
                    raise ValueError('unrecognized source content')
                sha = _sha(body)
                rel = f'blobs/{sha[:2]}/{sha}'
                path = root / rel
                if path.exists():
                    if _sha(path.read_bytes()) != sha:
                        raise ValueError('existing source SHA mismatch')
                else:
                    atomic_bytes(path, body)
                changed = 'unchanged' if row['latest_sha'] == sha else 'changed' if prior else 'new'
                report['unchanged' if changed == 'unchanged' else 'new_versions'] += 1
                observed = prior['observed_at'] if offline else utc_now()
                source = {'source_url': final, 'requested_url': row['url'], 'sha256': sha,
                          'snapshot_path': rel, 'observed_at': observed, 'language': language(final),
                          'kind': kind, 'format': kind, 'http': meta, 'change_status': changed,
                          'supersedes_sha256': row['latest_sha'] if changed == 'changed' else None}
                db.execute('INSERT OR IGNORE INTO source_versions VALUES(?,?,?)', (row['url'], sha, _dump(source)))
                db.execute('INSERT INTO observations(url,observed_at,status,sha,change_status) VALUES(?,?,?,?,?)',
                           (row['url'], observed, meta.get('status', 200), sha, changed))
                if kind == 'html':
                    page = adapter.parse(body, final)
                    parents = [r[0] for r in db.execute('SELECT product_id FROM source_products WHERE url=?', (row['url'],))]
                    identity = adapter.identity(page, final) if row['role'] != 'specification' else None
                    if identity is not None:
                        baseline_product = known_products.get(identity['id']) or _product(db, identity['id'])
                        product = json.loads(_dump(baseline_product or {**identity, 'category': '', 'categories': [], 'availability': 'not_verified'}))
                        # Keep explicitly imported legacy parent identity where available.
                        product.update(identity)
                        tables = [t for t in page.get('tables', []) if t.get('is_specification') and t.get('rows')]
                        for table in tables:
                            table['source_refs'] = [{'url': final, 'sha256': sha}]
                        product['tables'] = _replace_tables(product.get('tables', []), tables, final,
                                                            product.get('source_url'))
                        if tables or not product['tables']:
                            product.update(source_url=final, source_sha256=sha, observed_at=observed)
                        product.update(company_id=company_id, parser_revision='native-v2',
                                       product_url=product.get('product_url') or final,
                                       identity_status='official_product_page_observed',
                                       extraction_status='native_tables_extracted' if product['tables'] else 'specification_search_pending')
                        pages = product.get('official_pages', []) + [{'url': final, 'sha256': sha}]
                        product['official_pages'] = list({(p['url'], p['sha256']): p for p in pages}.values())
                        product.setdefault('attachments', [])
                        if not product.get('parent_id') and row['parent']:
                            parent = db.execute('SELECT product_id FROM source_products WHERE url=?', (row['parent'],)).fetchone()
                            if parent and parent[0] != product['id']:
                                product['parent_id'] = parent[0]
                        _save_product(db, product, baseline_product)
                        _enqueue(db, row['url'], product_id=product['id'])
                        parents = [product['id']]
                    elif row['role'] == 'specification':
                        _attach_tables(db, parents, page.get('tables', []), source)
                    is_directory = bool(identity and identity['kind'] == 'family_or_directory')
                    if row['depth'] < 2 or not is_directory:
                        for link in adapter.candidates(page, final, is_directory=is_directory):
                            if link['role'] != 'product' and _prefer_existing(db, parents, link['rank'], link['url'], refresh):
                                continue
                            if link['url'] == row['url'] or row['depth'] >= 3:
                                continue
                            # Product navigation gets its own entity; source carriers inherit ownership.
                            _enqueue(db, link['url'], row['url'], row['depth'] + 1, link['role'], priority=link['rank'])
                            if link['role'] != 'product':
                                if refresh:
                                    db.execute("UPDATE frontier SET state='pending',error=NULL WHERE url=? AND state IN ('done','deferred_better_native_source')", (link['url'],))
                                for parent in parents:
                                    _enqueue(db, link['url'], product_id=parent)
                else:
                    extracted = (adapter.component_tables(body, final) if kind == 'json'
                                 else adapter.document_tables(body, kind, final) or extract_document(body, kind))
                    owners = [r[0] for r in db.execute('SELECT product_id FROM source_products WHERE url=?', (row['url'],))]
                    _attach_tables(db, owners, extracted['tables'], source, extracted)
                db.execute("UPDATE frontier SET state='done',etag=?,modified=?,latest_sha=?,error=NULL WHERE url=?",
                           (meta.get('etag'), meta.get('last_modified'), sha, row['url']))
                db.commit()
            except Exception as exc:
                message = type(exc).__name__ + ': ' + str(exc)
                retryable = is_transient_error(message)
                state = 'unavailable' if isinstance(exc, HTTPError) and exc.code in {404, 410} else 'retryable_error' if retryable else 'review_required'
                db.execute("UPDATE frontier SET state=?,error=? WHERE url=?", (state, message, row['url']))
                db.execute('INSERT INTO errors(url,occurred_at,error,retryable) VALUES(?,?,?,?)', (row['url'], utc_now(), message, int(retryable)))
                db.commit()
                report['errors'].append({'url': row['url'], 'error': message, 'state': state})
                if isinstance(exc, HTTPError) and exc.code in {429, 503}:
                    break
        receipts = getattr(fetcher, 'policy_receipts', [])
        if receipts:
            policy_sha = _sha(_dump(receipts).encode())
            atomic_json(root / 'acquisition' / company_id / 'policies' / (policy_sha + '.json'), receipts)
            report['robots_receipt'] = str(Path('acquisition') / company_id / 'policies' / (policy_sha + '.json'))
        return _catalog(db, company_id, report, _scope(db, seeds), known_catalog, root)


def _attach_tables(db, owners, tables, source, extraction=None):
    for owner in owners:
        product = _product(db, owner)
        if product is None:
            continue
        native = [t for t in tables if t.get('is_specification', True) and t.get('rows')]
        for table in native:
            table['source_refs'] = [{'url': source['source_url'], 'sha256': source['sha256']}]
        product['tables'] = _replace_tables(product.get('tables', []), native, source['source_url'], product.get('source_url'))
        refs = [a for a in product.get('attachments', []) if a['url'] != source['source_url']]
        product['attachments'] = refs + [{'url': source['source_url'], 'sha256': source['sha256'], 'kind': source['kind']}]
        product['extraction_status'] = 'native_tables_extracted' if product['tables'] else (extraction or {}).get('status', 'specification_search_pending')
        if extraction and extraction.get('uncertainty'):
            product['extraction_uncertainty'] = extraction['uncertainty']
        _save_product(db, product)


def _replace_tables(previous, current, source_url, old_primary=None):
    retained = []
    for table in previous:
        refs = table.get('source_refs', [])
        if not refs:
            if old_primary != source_url:
                retained.append(table)
            continue
        remaining = [r for r in refs if r.get('url') != source_url]
        if remaining:
            retained.append({**table, 'source_refs': remaining})
    return retained + current


def _scope(db, seeds):
    scope = set(seeds)
    while True:
        expanded = scope | {r[1] for r in db.execute('SELECT parent,child FROM links') if r[0] in scope}
        if expanded == scope:
            return scope
        scope = expanded


def _catalog(db, company_id, report, scope, known_catalog=None, root=None):
    frontier = [dict(r) for r in db.execute('SELECT * FROM frontier ORDER BY depth,url') if r['url'] in scope]
    product_ids = {r[0] for r in db.execute('SELECT product_id,url FROM source_products') if r[1] in scope}
    report['pending'] = sum(r['state'] in {'pending', 'retryable_error'} for r in frontier)
    report['complete'] = False  # A bounded run never proves company-wide completeness.
    report['scope_exhausted'] = report['pending'] == 0
    products = [json.loads(r['payload']) for r in db.execute('SELECT id,payload FROM products ORDER BY id') if r['id'] in product_ids]
    known_products = {p['id']: p for p in (known_catalog or {}).get('products', [])}
    # The acquisition payload is a resume cache. A newer ProductStore export
    # remains authoritative even when this run did not reparse a finished URL.
    def timestamp(value):
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    products = [known_products[p['id']] if p['id'] in known_products and known_products[p['id']].get('observed_at') and timestamp(known_products[p['id']]['observed_at']) > timestamp(p['observed_at']) else p for p in products]
    sources = [json.loads(r['payload']) for r in db.execute('SELECT url,payload FROM source_versions ORDER BY url,sha') if r['url'] in scope]
    source_map = {(s['source_url'], s['sha256']): s for s in sources}
    known_sources = {(s['source_url'], s['sha256']): s for s in (known_catalog or {}).get('sources', [])}
    for product in products:
        refs = [(product['source_url'], product['source_sha256'])]
        refs += [(r['url'], r['sha256']) for t in product.get('tables', []) for r in t.get('source_refs', [])]
        refs += [(r['url'], r['sha256']) for r in product.get('official_pages', []) if r.get('sha256')]
        for key in refs:
            if key in source_map:
                continue
            source = known_sources.get(key)
            if source is None:
                # A previously collected auxiliary source can be outside the new seed scope.
                source = next((json.loads(r[0]) for r in db.execute('SELECT payload FROM source_versions')
                               if (json.loads(r[0])['source_url'], json.loads(r[0])['sha256']) == key), None)
            if source is None:
                raise ValueError('product references missing source evidence: ' + key[0])
            if root is None or _sha((root / source['snapshot_path']).read_bytes()) != source['sha256']:
                raise ValueError('known source SHA mismatch: ' + key[0])
            source_map[key] = source
    return {'schema_version': 1, 'company_id': company_id, 'generated_at': utc_now(),
            'products': products, 'sources': list(source_map.values()),
            'frontier': frontier, 'coverage': {'complete': False, 'bounded_explicit_product_scope': True},
            'acquisition_report': report}
