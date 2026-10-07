"""Offline Supermicro crawl -> historical catalog supplement and document links.

The legacy ledger and bytes are read-only. Only verified HTML is bundled;
document hashes describe ledger contents, not extracted PDF specifications.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tarfile
from urllib.parse import urlsplit

from .adapters.supermicro import SupermicroProductAdapter
from .adapters.base import FORMATS, language
from .catalog_batch2 import decode
from .inventory import atomic_bytes, atomic_json, utc_now
from .products import timestamp
from .ownership import support_context, VERSION as OWNERSHIP_VERSION


def identity(page, url, adapter):
    if support_context('supermicro', url):
        return None
    # Old server titles sometimes omit SYS-, while their official URL supplies
    # it. Require the exact remaining model token in the captured title/heading.
    found = adapter.identity(page, url)
    headings = ' '.join([page.get('heading', ''), page.get('title', '')])
    model = re.search(r'\b(?:SYS|AS|ARS|SBI|SSG|SRS|CSE|MBI|MBD|AOC|AOM|SC|SSE)[-A-Z0-9+]+(?![A-Z0-9+])', headings, re.I)
    if not model:
        candidate = adapter.MODEL.search(urlsplit(url).path.rsplit('/', 1)[-1])
        if candidate:
            bare = candidate.group().split('-', 1)[-1]
            if re.search(r'(?<![A-Z0-9+-])' + re.escape(bare) + r'(?![A-Z0-9+-])', headings, re.I):
                model = candidate
    if model:
        name = model.group().upper()
        return {'id': 'supermicro-' + hashlib.sha256(name.casefold().encode()).hexdigest()[:20],
                'name': name, 'kind': 'named_product', 'parent_id': None}
    return found


def export(root, output, max_batch_bytes=4 * 1024 * 1024):
    root, output = Path(root).expanduser().resolve(), Path(output).expanduser().resolve()
    ledger = root / 'ledger/companies/supermicro/crawl.sqlite'
    if not ledger.is_file():
        raise ValueError('Supermicro legacy ledger is missing')
    if output == root or root.is_relative_to(output) or output.is_relative_to(root / 'ledger') or output.is_relative_to(root / 'blobs'):
        raise ValueError('output must be separate from legacy evidence')
    db = sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    adapter = SupermicroProductAdapter()
    products, sources, originals, page_products, page_links, skipped = {}, {}, {}, {}, {}, Counter()
    ownership_exclusions = []
    try:
        db.execute('BEGIN')  # one consistent read snapshot, even if crawl resumes
        total_pages = db.execute('SELECT count(*) FROM pages').fetchone()[0]
        pages = list(db.execute('SELECT p.*,r.url FROM pages p JOIN requests r ON r.id=p.request WHERE r.method=\'GET\' ORDER BY p.observed_at,r.url,p.sha'))
        for row in pages:
            url = adapter.normalize(row['url'])
            if not url or not adapter.page_allowed(url):
                skipped['outside_product_or_language_scope'] += 1
                continue
            timestamp(row['observed_at'])
            path = (root / row['path']).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError('missing or escaped HTML snapshot: ' + row['sha'])
            wire = path.read_bytes()
            if len(wire) > 16 * 1024 * 1024 or hashlib.sha256(wire).hexdigest() != row['sha']:
                raise ValueError('HTML snapshot size/hash mismatch: ' + row['sha'])
            page = adapter.parse(decode(wire), url)
            issue = support_context('supermicro', url)
            if issue:
                skipped['third_party_storage_support_page'] += 1
                ownership_exclusions.append({**issue, 'title': page.get('title'),
                                             'source_sha256': row['sha'], 'observed_at': row['observed_at']})
                continue
            item = identity(page, url, adapter)
            if not item:
                skipped['missing_product_heading'] += 1
                continue
            ref = {'url': url, 'sha256': row['sha']}
            tables = [{**t, 'index': i + 1, 'source_refs': [ref]} for i,t in enumerate(page['tables']) if t.get('is_specification')]
            item.update(company_id='supermicro', category='官方归档产品页', category_basis='captured_product_page',
                        taxonomy=[], availability='not_verified', product_url=url, source_url=url,
                        source_sha256=row['sha'], observed_at=row['observed_at'], tables=tables,
                        extraction_status='native_tables_extracted' if tables else 'specification_search_pending',
                        official_pages=[ref], attachments=[], archive_basis='legacy_html_snapshot')
            key = (url, row['sha'])
            sources[key] = {'source_url': url, 'sha256': row['sha'], 'snapshot_path': 'originals/' + row['sha'] + '.html',
                            'observed_at': row['observed_at'], 'kind': 'official_html_product_page', 'format': 'html', 'language': language(url)}
            originals[row['sha']] = wire
            page_products[row['request']] = item
            page_links[row['request']] = {adapter.normalize(link['url'],url) for link in page['links']}
            old = products.get(item['id'])
            # Native specifications outrank a translated/no-table view. Within
            # the same rank retain the latest observation; every source remains.
            rank = (bool(tables), language(url) == 'en', timestamp(item['observed_at']))
            if not old or rank > old[0]:
                products[item['id']] = (rank, item)
        documents = {}
        for row in db.execute("""SELECT b.*,r.url FROM blobs b JOIN requests r ON r.latest_sha=b.sha WHERE r.method='GET'
                              UNION SELECT b.*,r.url FROM blobs b JOIN observations o ON o.sha=b.sha
                              JOIN requests r ON r.id=o.request WHERE r.method='GET' ORDER BY sha,url"""):
            if row['kind'] not in FORMATS:
                continue
            url = adapter.normalize(row['url'])
            # All archived languages stay in the local material index; only
            # public allowlisted URLs enter the website supplement.
            if not url:
                parts = urlsplit(row['url'])
                if parts.scheme != 'https' or parts.username or parts.password or parts.port not in (None,443) or parts.hostname not in adapter.profile['allowed_hosts']:
                    continue
                url = row['url']
            d = documents.setdefault(row['sha'], {'id': row['sha'], 'format': row['kind'], 'urls': [], 'links': [],
                                                  'basis': 'legacy_ledger_document_identity_not_specification_extraction'})
            if url not in d['urls']:
                d['urls'].append(url)
        for row in db.execute('SELECT e.parent,e.label,e.first_seen,r.url,r.latest_sha FROM edges e JOIN requests r ON r.id=e.child WHERE r.latest_sha IS NOT NULL ORDER BY e.parent,r.url,e.label'):
            item = page_products.get(row['parent'])
            doc = documents.get(row['latest_sha'])
            if not item or not doc or row['url'] not in doc['urls'] or row['url'] not in page_links.get(row['parent'],set()):
                continue
            link = {'product_id': item['id'], 'url': row['url'], 'label': row['label'],
                    'source_url': item['source_url'], 'source_sha256': item['source_sha256']}
            if link not in doc['links']:
                doc['links'].append(link)
            selected = products[item['id']][1]
            attachment = {k:v for k,v in link.items() if k != 'product_id'}
            attachment['format'] = doc['format']
            attachment['relationship'] = 'captured_page_link_not_whole_product_specification'
            if attachment not in selected['attachments']:
                selected['attachments'].append(attachment)
        counts = dict(db.execute('SELECT kind,count(*) FROM blobs GROUP BY kind'))
        latest = db.execute('SELECT max(observed_at) FROM observations').fetchone()[0]
    finally:
        db.close()
    items = [products[k][1] for k in sorted(products)]
    if not items:
        raise ValueError('no captured product pages parsed; nothing exported')
    # Source/page associations are preserved independently from PDF bytes.
    materials = list(documents.values())
    common = {'schema_version': 1, 'company_id': 'supermicro', 'catalog_mode': 'historical_supplement',
              'authority': 'candidate_only', 'coverage': {'complete': False, 'limitations': [
                  'Offline legacy English/Chinese HTML snapshots; observed_at remains the original crawl time.',
                  'Only captured HTML native tables are extracted; document links include manuals/test reports and do not imply whole-product specifications.',
                  'Existing receiver product identities remain current; overlapping historical payloads are retained as source history.',
                  'Incomplete archived website inventory; not current availability, full reading or research adoption.']}}
    batches, pending = [], []
    def payload(group, material_group):
        refs = {(p['source_url'],p['source_sha256']) for p in group}
        for p in group:
            refs.update((a['source_url'],a['source_sha256']) for a in p['attachments'])
        for d in material_group:
            refs.update((l['source_url'],l['source_sha256']) for l in d['links'])
        return {**common, 'generated_at': utc_now(), 'products': group,
                'sources': [sources[k] for k in sorted(refs)], 'material_index': material_group}
    # Product and material batches stay below the HTTP receiver's body bound.
    for item in items:
        trial = payload(pending + [item], [])
        if len(json.dumps(trial,ensure_ascii=False).encode()) > max_batch_bytes:
            if not pending:
                raise ValueError('one product exceeds batch bound; review required')
            batches.append(payload(pending, [])); pending = []
        pending.append(item)
    if pending:
        batches.append(payload(pending, []))
    pending = []
    for doc in materials:
        trial = payload([], pending + [doc])
        if len(json.dumps(trial,ensure_ascii=False).encode()) > max_batch_bytes:
            if not pending:
                raise ValueError('one material exceeds batch bound; review required')
            batches.append(payload([], pending)); pending = []
        pending.append(doc)
    if pending:
        batches.append(payload([], pending))
    if sum(len(wire) for wire in originals.values()) + sum(len(json.dumps(p,ensure_ascii=False).encode()) for p in batches) > 510 * 1024 * 1024:
        raise ValueError('HTML supplement exceeds bundle bound; split scope before export')
    entries = []
    for i,batch in enumerate(batches):
        path = 'catalogs/batch-' + str(i + 1).zfill(5) + '.json'
        body = json.dumps(batch,ensure_ascii=False,sort_keys=True).encode()
        entries.append({'path': path, 'sha256': hashlib.sha256(body).hexdigest()})
        atomic_bytes(output / path, body)
    for sha,wire in originals.items():
        atomic_bytes(output / ('originals/' + sha + '.html'), wire)
    report = {'company_id':'supermicro', 'ledger_documents_by_format': counts, 'ledger_page_snapshots':total_pages,
              'latest_observation':latest, 'exported_entities':len(items), 'named_products':sum(p['kind']=='named_product' for p in items),
              'specification_tables':sum(len(p['tables']) for p in items), 'indexed_documents':len(materials),
              'linked_documents':sum(bool(d['links']) for d in materials), 'unassigned_documents':sum(not d['links'] for d in materials),
              'skipped_pages':dict(skipped), 'batch_count':len(batches), 'pdf_bytes_transferred':0,
              'ownership_policy_version': OWNERSHIP_VERSION, 'ownership_exclusions': ownership_exclusions}
    atomic_json(output / 'materials.json', materials)
    atomic_json(output / 'report.json', report)
    atomic_json(output / 'manifest.json', {'schema_version':1,'company_id':'supermicro','mode':'historical_supplement','batches':entries})
    bundle = output / 'catalog-bundle.tar.gz'
    temporary = output / '.catalog-bundle.tar.gz.incoming'
    with tarfile.open(temporary, 'w:gz') as tar:
        for name in ['manifest.json','report.json'] + [e['path'] for e in entries] + ['originals/' + sha + '.html' for sha in sorted(originals)]:
            tar.add(output / name, arcname=name, recursive=False)
    temporary.replace(bundle)
    return {**report, 'output':str(output), 'bundle':str(bundle)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('~/.local/share/fetchspec'))
    parser.add_argument('--output',type=Path,default=Path('~/.local/share/fetchspec/pipeline/legacy-supermicro'))
    args = parser.parse_args(argv)
    print(json.dumps(export(args.root,args.output),ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
