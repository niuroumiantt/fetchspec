"""Micron product catalog under the product-first standard (inresearch framework/06, "NVIDIA 打样").

Product list first, then official specifications, kept as the vendor publishes them:

- The list is Micron's own sitemap (``https://www.micron.com/sitemap.xml``). Every ``/products/`` path is
  an entity: category and family pages (``family_or_directory``), current part pages (``named_product``,
  listing ``active``) and part pages under ``/products/obsolete/`` (``named_product``, listing ``obsolete``).
  SPD-data views and ``…/part-catalog`` index pages are counted, not catalogued as products.
- Every category/family page and every current part page is fetched. A part's specification is the
  official JSON component its page names (see adapters/micron.py): all of the vendor's name/value rows,
  original text, nothing mapped or converted. The vendor's own status row (Part Status Code) is kept as
  ``official_status``; availability stays ``not_verified``.
- Obsolete parts are listed from the sitemap but not fetched; their identity source is the sitemap
  snapshot and they are counted as not collected, never as failures.
- Coverage keeps the standard's two denominators apart: current named parts with official
  specifications / current named parts, and all catalogued entities with tables / all entities.
  A part whose official component returns no rows is a vendor specification gap, not a fetch failure.

State, raw bytes and outputs live under ``--out``; reruns are resumable and send conditional requests
(ETag / Last-Modified). Nothing here executes page script.

    PYTHONPATH=src python3 -m fetchspec.micron_catalog --out ~/Downloads/tempfetch-micron
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from xml.etree import ElementTree

from .adapters.micron import PART_PAGE, MicronProductAdapter
from .extraction import html_page
from .inventory import atomic_json, load_profile, utc_now
from .network import ProductFetcher

COMPANY = 'micron'
SITEMAP = 'https://www.micron.com/sitemap.xml'
DIRECTORY = 'https://www.micron.com/products'
OBSOLETE_PART = re.compile(r'^/products/obsolete/(?:[a-z0-9][a-z0-9-]*/)+part-catalog/part-detail/([a-z0-9][a-z0-9-]*)/?$')
DIRECTORY_PAGE = re.compile(r'^/products(?:/[a-z0-9][a-z0-9-]*)*/?$')
TITLE_SUFFIX = re.compile(r'\s*[|–-]\s*Micron(?: Technology(?:,? Inc\.?)?)?\s*$', re.I)
LIMITATIONS = [
    '产品清单以 Micron 官网 sitemap 为准；sitemap 没列出的不推断存在，列出但取不到的单列，不推断下架。',
    '停产（/products/obsolete/）零件只登记身份，不抓规格，计为未采集而非失败。',
    'official_status 是厂商零件状态码原文（如 Production），不等于已核实在售。',
    '规格是官方零件组件 JSON 的名称与取值原文，未映射跨产品通用字段；功耗等组件里没有的参数需另读数据手册。',
]


def _id(key):
    return COMPANY + '-' + hashlib.sha256(key.encode()).hexdigest()[:20]


def part_id(part_number_slug):
    """Same identity as the target-driven adapter, so delivered parts and catalog parts are one product."""
    return _id(part_number_slug)


def directory_id(path):
    return _id(path.casefold().rstrip('/'))


def title_case(slug):
    return ' '.join(w.upper() if len(w) <= 4 and re.search(r'\d', w) else w.capitalize() for w in slug.split('-'))


def classify(url):
    """(role, key) for one sitemap URL: directory / part / obsolete / spd / part_catalog_index / other."""
    p = urlsplit(url)
    if p.hostname != 'www.micron.com' or p.query or not p.path.startswith('/products'):
        return 'other', None
    path = p.path.rstrip('/') or '/products'
    if '/spd-data/' in path:
        return 'spd', None
    match = PART_PAGE.match(path)
    if match:
        return 'part', match.group(1)
    match = OBSOLETE_PART.match(path)
    if match:
        return 'obsolete', match.group(1)
    if path.endswith('/part-catalog'):
        return 'part_catalog_index', None
    if '/part-catalog' in path:
        return 'other', None
    if DIRECTORY_PAGE.match(path):
        return 'directory', path
    return 'other', None


def taxonomy_path(path):
    """Vendor path segments under /products, up to (not including) part-catalog."""
    segments = [s for s in path.split('/') if s]
    segments = segments[1:]  # drop 'products'
    if 'part-catalog' in segments:
        segments = segments[:segments.index('part-catalog')]
    return segments


class Catalog:
    def __init__(self, out, fetcher=None, profile=None):
        self.out = Path(out).expanduser()
        self.out.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.out / 'micron-catalog.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS pages(url TEXT PRIMARY KEY, role TEXT NOT NULL, key TEXT, sha TEXT, snapshot_path TEXT,
            observed_at TEXT, status TEXT, etag TEXT, last_modified TEXT, content_type TEXT, error TEXT, payload TEXT);
          CREATE TABLE IF NOT EXISTS sitemap(url TEXT PRIMARY KEY, role TEXT NOT NULL, key TEXT, lastmod TEXT);
        ''')
        self.profile = profile or load_profile(COMPANY)
        self.fetcher = fetcher
        self.adapter = MicronProductAdapter()
        self.requests = 0

    # -- fetching ------------------------------------------------------------------------------------
    def _fetcher(self):
        if self.fetcher is None:
            self.fetcher = ProductFetcher(self.profile)
        return self.fetcher

    def _store(self, body, suffix):
        sha = hashlib.sha256(body).hexdigest()
        relative = f'blobs/{sha[:2]}/{sha}.{suffix}'
        path = self.out / relative
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        return sha, relative

    def fetch(self, url, role, key=None, *, suffix='html', refresh=False, cap=None):
        """Fetch once per run state; conditional on the stored ETag/Last-Modified when refreshing."""
        row = self.db.execute('SELECT * FROM pages WHERE url=?', (url,)).fetchone()
        if row and row['sha'] and not refresh:
            return row
        headers = {}
        if row and row['sha']:
            if row['etag']:
                headers['If-None-Match'] = row['etag']
            if row['last_modified']:
                headers['If-Modified-Since'] = row['last_modified']
        observed = utc_now()
        self.requests += 1
        try:
            body, meta = self._fetcher().get(url, headers=headers, cap=cap)
        except HTTPError as exc:
            if exc.code == 304 and row and row['sha']:
                self.db.execute('UPDATE pages SET observed_at=?, status=? WHERE url=?', (observed, 'not_modified', url))
                self.db.commit()
                return self.db.execute('SELECT * FROM pages WHERE url=?', (url,)).fetchone()
            state = 'unavailable' if exc.code in (404, 410) else 'failed'
            self._record_error(url, role, key, observed, state, f'HTTP {exc.code}')
            return self.db.execute('SELECT * FROM pages WHERE url=?', (url,)).fetchone()
        except Exception as exc:  # network, policy, size: recorded per page, the run continues
            state = 'policy_blocked' if 'robots' in str(exc) or 'allowlist' in str(exc) else 'failed'
            self._record_error(url, role, key, observed, state, f'{type(exc).__name__}: {exc}')
            return self.db.execute('SELECT * FROM pages WHERE url=?', (url,)).fetchone()
        sha, relative = self._store(body, suffix)
        self.db.execute('INSERT OR REPLACE INTO pages VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        (url, role, key, sha, relative, observed, 'fetched', meta.get('etag'), meta.get('last_modified'),
                         meta.get('content_type', ''), None, json.dumps({'final_url': meta.get('final_url'),
                                                                         'http_status': meta.get('status')})))
        self.db.commit()
        return self.db.execute('SELECT * FROM pages WHERE url=?', (url,)).fetchone()

    def _record_error(self, url, role, key, observed, state, error):
        row = self.db.execute('SELECT sha FROM pages WHERE url=?', (url,)).fetchone()
        if row and row['sha']:  # keep the last good snapshot; note the failed recheck
            self.db.execute('UPDATE pages SET error=? WHERE url=?', (f'{state}: {error} at {observed}', url))
        else:
            self.db.execute('INSERT OR REPLACE INTO pages(url,role,key,observed_at,status,error) VALUES(?,?,?,?,?,?)',
                            (url, role, key, observed, state, error))
        self.db.commit()

    def body(self, row):
        return (self.out / row['snapshot_path']).read_bytes()

    # -- the product list ----------------------------------------------------------------------------
    def sync_sitemap(self):
        # Every run rechecks the list itself (conditionally): new parts must enter, unchanged bytes are not re-sent.
        row = self.fetch(SITEMAP, 'sitemap', suffix='xml', refresh=True, cap=self.profile['max_xml_bytes'])
        if not row or not row['sha']:
            raise RuntimeError('Micron sitemap unavailable: ' + str(row['error'] if row else 'no response'))
        root = ElementTree.fromstring(self.body(row))
        ns = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        entries = []
        for node in root.findall('s:url', ns):
            loc = (node.findtext('s:loc', default='', namespaces=ns) or '').strip()
            if loc:
                role, key = classify(loc)
                entries.append((loc, role, key, (node.findtext('s:lastmod', default='', namespaces=ns) or '').strip()))
        if not entries:
            raise RuntimeError('Micron sitemap has no <url> entries')
        with self.db:
            self.db.execute('DELETE FROM sitemap')
            self.db.executemany('INSERT OR REPLACE INTO sitemap VALUES(?,?,?,?)', entries)
        return row

    def collect(self, *, limit=None, refresh=False):
        self.sync_sitemap()
        for row in self.db.execute("SELECT url, key FROM sitemap WHERE role='directory' ORDER BY url").fetchall():
            self.fetch(row['url'], 'directory', row['key'], refresh=refresh)
        parts = self.db.execute("SELECT url, key FROM sitemap WHERE role='part' ORDER BY url").fetchall()
        for n, row in enumerate(parts[:limit] if limit else parts, 1):
            page = self.fetch(row['url'], 'part', row['key'], refresh=refresh)
            if page and page['sha']:
                parsed = self.adapter.parse(self.body(page), row['url'])
                component = parsed.get('component_url')
                if component:
                    self.fetch(component, 'component', row['key'], suffix='json', refresh=refresh)
            if n % 50 == 0:
                print(f'  {n}/{len(parts)} parts, {self.requests} requests', file=sys.stderr, flush=True)
        return self.requests

    # -- export --------------------------------------------------------------------------------------
    def _source(self, row, kind, fmt):
        meta = json.loads(row['payload'] or '{}')
        return {'source_url': row['url'], 'sha256': row['sha'], 'snapshot_path': row['snapshot_path'],
                'observed_at': row['observed_at'], 'format': fmt, 'language': 'en', 'kind': kind,
                'http': {'status': meta.get('http_status'), 'final_url': meta.get('final_url'),
                         'content_type': row['content_type'], 'etag': row['etag'], 'last_modified': row['last_modified']}}

    def export(self):
        pages = {r['url']: r for r in self.db.execute('SELECT * FROM pages')}
        sitemap_row = pages[SITEMAP]
        sitemap = self.db.execute('SELECT * FROM sitemap').fetchall()
        sources = {SITEMAP: self._source(sitemap_row, 'official_sitemap', 'xml')}
        directories, names = {}, {}
        for entry in (e for e in sitemap if e['role'] == 'directory'):
            path = entry['key']
            row = pages.get(entry['url'])
            name = ''
            if row is not None and row['sha']:
                parsed = html_page(self.body(row), entry['url'])
                # The <title> is the vendor's category name ("DDR SDRAM"); the <h1> is often a slogan.
                name = TITLE_SUFFIX.sub('', ' '.join((parsed.get('title') or parsed.get('heading') or '').split()))
                sources[entry['url']] = self._source(row, 'official_directory_page', 'html')
            segments = taxonomy_path(path)
            names[tuple(segments)] = name or (title_case(segments[-1]) if segments else 'Products')
            directories[path] = (entry, row)

        def taxonomy(segments):
            return [{'slug': s, 'name': names.get(tuple(segments[:i + 1]), title_case(s))} for i, s in enumerate(segments)]

        def parent_of(segments):
            for i in range(len(segments), -1, -1):
                path = '/products' + ''.join('/' + s for s in segments[:i])
                if path in directories:
                    return directory_id(path)
            return None

        products, counts = [], {}
        def count(key):
            counts[key] = counts.get(key, 0) + 1
        for path, (entry, row) in sorted(directories.items()):
            segments = taxonomy_path(path)
            fetched = row is not None and bool(row['sha'])
            source = sources[entry['url']] if fetched else sources[SITEMAP]
            products.append({
                'id': directory_id(path), 'name': names[tuple(segments)], 'kind': 'family_or_directory',
                'listing': 'directory', 'official_status': '', 'part_number': '',
                'parent_id': parent_of(segments[:-1]) if segments else None,
                'taxonomy': taxonomy(segments), 'category': ' / '.join(t['name'] for t in taxonomy(segments)) or 'Products',
                'categories': [t['name'] for t in taxonomy(segments)], 'availability': 'not_verified',
                'identity_status': 'official_page_observed' if fetched else 'official_sitemap_listed',
                'source_url': source['source_url'], 'source_sha256': source['sha256'],
                'observed_at': source['observed_at'], 'tables': [], 'attachments': [], 'official_resources': [],
                'official_pages': [{'url': source['source_url'], 'sha256': source['sha256']}] if fetched else [],
                'product_url': entry['url'], 'sitemap_lastmod': entry['lastmod'],
                'extraction_status': 'directory_no_specification_table' if fetched else (
                    'directory_page_' + (row['status'] if row is not None else 'pending'))})
            count('directory')
        for entry in (e for e in sitemap if e['role'] == 'part'):
            segments = taxonomy_path(urlsplit(entry['url']).path)
            page, component = pages.get(entry['url']), None
            name, tables, status, official_status, extra_sources = entry['key'].upper(), [], 'specification_search_pending', '', []
            if page is not None and page['sha']:
                parsed = self.adapter.parse(self.body(page), entry['url'])
                identity = self.adapter.identity(parsed, entry['url'])
                name = identity['name'] if identity else name
                sources[entry['url']] = self._source(page, 'official_part_page', 'html')
                component_url = parsed.get('component_url')
                component = pages.get(component_url) if component_url else None
                if component is not None and component['sha']:
                    extracted = self.adapter.component_tables(self.body(component), component_url)
                    sources[component_url] = self._source(component, 'official_part_specification_component', 'json')
                    for table in extracted['tables']:
                        tables.append({k: table[k] for k in ('index', 'section', 'rows', 'notes', 'is_specification', 'method')}
                                      | {'source_refs': [{'url': component_url, 'sha256': component['sha']}]})
                    document = json.loads(self.body(component))
                    official_status = next((d.get('value', '') for d in document.get('details', [])
                                            if isinstance(d, dict) and d.get('id') == 'production_status'), '') or ''
                    status = 'native_tables_extracted' if tables else 'vendor_specification_gap'
                    extra_sources = [{'url': component_url, 'sha256': component['sha']}]
                elif component_url and component is not None:
                    status = 'specification_' + component['status']
                elif not component_url:
                    status = 'vendor_specification_gap'  # the page names no specification component
                count('part_page_fetched')
            elif page is not None:
                status = 'part_page_' + page['status']
            source = sources.get(entry['url'], sources[SITEMAP])
            products.append({
                'id': part_id(entry['key']), 'name': name, 'kind': 'named_product', 'listing': 'active',
                'official_status': official_status, 'part_number': name, 'parent_id': parent_of(segments),
                'taxonomy': taxonomy(segments), 'category': ' / '.join(t['name'] for t in taxonomy(segments)),
                'categories': [t['name'] for t in taxonomy(segments)], 'availability': 'not_verified',
                'identity_status': 'official_page_observed' if source is not sources[SITEMAP] else 'official_sitemap_listed',
                'source_url': source['source_url'], 'source_sha256': source['sha256'], 'observed_at': source['observed_at'],
                'tables': tables, 'attachments': [], 'official_resources': [{'url': r['url'], 'role': 'specification_component'}
                                                                           for r in extra_sources],
                'official_pages': [{'url': source['source_url'], 'sha256': source['sha256']}] if source is not sources[SITEMAP] else [],
                'product_url': entry['url'], 'sitemap_lastmod': entry['lastmod'], 'extraction_status': status})
            count('part')
        for entry in (e for e in sitemap if e['role'] == 'obsolete'):
            segments = taxonomy_path(urlsplit(entry['url']).path)
            source = sources[SITEMAP]
            products.append({
                'id': part_id(entry['key']), 'name': entry['key'].upper(), 'name_basis': 'url_slug',
                'kind': 'named_product', 'listing': 'obsolete', 'official_status': 'Obsolete (listed under /products/obsolete/)',
                'part_number': entry['key'].upper(), 'parent_id': parent_of(segments), 'taxonomy': taxonomy(segments),
                'category': ' / '.join(t['name'] for t in taxonomy(segments)),
                'categories': [t['name'] for t in taxonomy(segments)], 'availability': 'not_verified',
                'identity_status': 'official_sitemap_listed', 'source_url': source['source_url'],
                'source_sha256': source['sha256'], 'observed_at': source['observed_at'], 'tables': [], 'attachments': [],
                'official_resources': [], 'official_pages': [], 'product_url': entry['url'],
                'sitemap_lastmod': entry['lastmod'], 'extraction_status': 'not_collected_obsolete'})
            count('obsolete')
        # One product per part number. Micron lists some parts under two families (and some both as current and
        # obsolete): keep one entity, every official listing (category + page) and the current listing's tables.
        unique = {}
        rank = {'active': 0, 'directory': 0, 'obsolete': 1}
        for p in products:
            p['listings'] = [{'listing': p['listing'], 'category': p['category'], 'product_url': p['product_url']}]
            old = unique.get(p['id'])
            if old is None:
                unique[p['id']] = p
                continue
            keep, other = (old, p) if (rank[old['listing']], not old['tables']) <= (rank[p['listing']], not p['tables']) else (p, old)
            keep['listings'] = old['listings'] + p['listings']
            keep['categories'] = sorted(set(keep['categories']) | set(other['categories']))
            keep['official_pages'] = list({s['url']: s for s in keep['official_pages'] + other['official_pages']}.values())
            unique[p['id']] = keep
        products = list(unique.values())
        for p in products:  # keep the delivery small: optional empty fields are left out
            for key in ('attachments', 'official_resources', 'official_pages', 'categories'):
                if not p.get(key):
                    p.pop(key, None)
            if len(p['listings']) == 1:
                p.pop('listings')
        ids = {p['id'] for p in products}
        for p in products:
            if p['parent_id'] not in ids:
                p['parent_id'] = None
        active = [p for p in products if p['listing'] == 'active']
        statuses = {}
        for p in active:
            statuses[p['extraction_status']] = statuses.get(p['extraction_status'], 0) + 1
        roles = {}
        for e in sitemap:
            roles[e['role']] = roles.get(e['role'], 0) + 1
        official = {}
        for p in active:
            official[p['official_status'] or '(none)'] = official.get(p['official_status'] or '(none)', 0) + 1
        coverage = {
            'complete': False, 'directory_url': DIRECTORY, 'standard': 'framework/06_acquisition.md 产品清单 → 官方规格 → 数据库 → 轻量展示',
            'sitemap': {'url': SITEMAP, 'sha256': sitemap_row['sha'], 'observed_at': sitemap_row['observed_at'],
                        'urls': len(sitemap), 'by_role': roles},
            'named_products_current': len(active),
            'named_products_current_with_specifications': sum(bool(p['tables']) for p in active),
            'entities': len(products), 'entities_with_tables': sum(bool(p['tables']) for p in products),
            'named_products_obsolete_listed': sum(p['listing'] == 'obsolete' for p in products),
            'directory_entities': sum(p['listing'] == 'directory' for p in products),
            'current_extraction_status': statuses, 'current_official_status': official,
            'vendor_specification_gaps': statuses.get('vendor_specification_gap', 0),
            'failed_or_unavailable': sum(v for k, v in statuses.items() if k.startswith(('part_page_', 'specification_'))
                                         and not k.endswith('pending')),
            'pending': statuses.get('specification_search_pending', 0),
            'requests_this_run': self.requests, 'limitations': LIMITATIONS}
        bundle = {'schema_version': 1, 'company_id': COMPANY, 'generated_at': utc_now(), 'coverage': coverage,
                  'product_map': {'policy': 'Products absent from a later sitemap are retained and flagged for review; '
                                            'removal needs an explicit, completed comparison.', 'entries': len(products)},
                  'products': sorted(products, key=lambda p: (p['listing'] != 'directory', p['category'], p['name'])),
                  'sources': list(sources.values())}
        atomic_json(self.out / 'catalog.json', bundle)
        atomic_json(self.out / 'product-sitemap.json', {
            'schema_version': 1, 'company_id': COMPANY, 'generated_at': bundle['generated_at'], 'directory_url': DIRECTORY,
            'coverage': coverage, 'entries': [{k: p.get(k) for k in ('id', 'name', 'parent_id', 'kind', 'listing', 'official_status',
                                                                    'category', 'product_url', 'source_url', 'source_sha256',
                                                                    'extraction_status')} for p in bundle['products']]})
        return bundle


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', required=True, type=Path, help='state, raw bytes and catalog.json go here')
    ap.add_argument('--limit', type=int, help='collect only the first N current parts (for a trial run)')
    ap.add_argument('--refresh', action='store_true', help='recheck known pages with conditional requests')
    ap.add_argument('--export-only', action='store_true', help='rebuild catalog.json from the stored state')
    args = ap.parse_args(argv)
    catalog = Catalog(args.out)
    if not args.export_only:
        catalog.collect(limit=args.limit, refresh=args.refresh)
    coverage = catalog.export()['coverage']
    print(json.dumps({k: coverage[k] for k in ('named_products_current', 'named_products_current_with_specifications',
                                               'entities', 'entities_with_tables', 'named_products_obsolete_listed',
                                               'directory_entities', 'current_extraction_status', 'current_official_status',
                                               'requests_this_run')}, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
