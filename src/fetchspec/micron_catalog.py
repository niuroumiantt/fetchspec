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
import shutil
import sqlite3
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from xml.etree import ElementTree

from .adapters.micron import PART_PAGE, MicronProductAdapter
from .extraction import cell, html_page
from .inventory import atomic_json, load_profile, utc_now
from .network import ProductFetcher

COMPANY = 'micron'
SITEMAP = 'https://www.micron.com/sitemap.xml'
DIRECTORY = 'https://www.micron.com/products'
OBSOLETE_PART = re.compile(r'^/products/obsolete/(?:[a-z0-9][a-z0-9-]*/)+part-catalog/part-detail/([a-z0-9][a-z0-9.-]*)/?$')
DIRECTORY_PAGE = re.compile(r'^/products(?:/[a-z0-9][a-z0-9-]*)*/?$')
TITLE_SUFFIX = re.compile(r'\s*[|–-]\s*Micron(?: Technology(?:,? Inc\.?)?)?\s*$', re.I)
# Families whose part pages name no specification component, and the vendor's own product brief that carries
# their specification table (a family-level table by capacity, with a part-number decoder). robots-allowed host only.
FAMILY_BRIEFS = {
    '6600-ion': {
        'url': 'https://www.micron.com/content/dam/micron/global/public/products/storage/ssds/data-center/6600/'
               '6600-ion-nvme-ssd-product-brief.pdf',
        'start': 'key specifications', 'end': 'Table 4',
        'section': 'Micron 6600 ION SSD key specifications (product brief, Table 4)',
        'part': re.compile(r'^MTFDL([A-Z]{2})(\d+T\d)Q'),
        'form_factor': {'AL': 'U.2 (15mm)', 'BQ': 'E3.S 1T (7.5mm)', 'BX': 'E3.L 1T (7.5mm)'},
        'capacity': {'30T7': '30.72TB', '61T4': '61.44TB', '122T8': '122.88TB', '245T7': '245.76TB'},
    },
}
SEGMENT = re.compile(r'\S(?:.*?\S)??(?=\s{2,}|$)')


def brief_rows(text, start, end):
    """Rows of a product-brief table from ``pdftotext -layout`` text: the label text left of the first value
    column, then the printed values in order (" | "); a value printed once across merged columns stays once.
    Returns (rows, raw block) so the original layout travels with the rows."""
    lines = text.splitlines()
    first = next(n for n, line in enumerate(lines) if start in line)
    last = next(n for n, line in enumerate(lines[first:], first) if line.strip().startswith(end))
    block = lines[first + 1:last]
    header = next(line for line in block if re.search(r'\d+\.\d+TB', line))
    threshold = min(m.start() for m in re.finditer(r'\d+\.\d+TB', header)) - 4
    # The brief prints availability as a check-mark glyph from a symbol font (U+F0FC, private use); show it as ✓.
    block = [line.replace('\uf0fc', '✓') for line in block]
    rows, pending = [], []
    for n, line in enumerate(block):
        segments = [(m.start(), m.group(0)) for m in SEGMENT.finditer(line)]
        labels = [s for at, s in segments if at < threshold]
        values = [s for at, s in segments if at >= threshold]
        if not values:
            pending = labels
            continue
        if not labels:
            # a label printed on the lines around its value ("100% 128KB sequential" / "writes")
            after = next(([s for at, s in (([(m.start(), m.group(0)) for m in SEGMENT.finditer(nxt)])) if at < threshold]
                          for nxt in block[n + 1:n + 2]), [])
            labels = [' '.join(pending[-1:] + after[-1:])] if pending or after else []
        rows.append([cell(' · '.join(labels)), cell(' | '.join(values))])
        pending = []
    return rows, '\n'.join(line.rstrip() for line in block if line.strip())


LIMITATIONS = [
    '产品清单以 Micron 官网 sitemap 为准；sitemap 没列出的不推断存在，列出但取不到的单列，不推断下架。',
    '停产（/products/obsolete/）零件只登记身份，不抓规格，计为未采集而非失败。',
    'official_status 是厂商零件状态码原文（如 Production），不等于已核实在售。',
    '规格是官方零件组件 JSON 的名称与取值原文，未映射跨产品通用字段；功耗等组件里没有的参数需另读数据手册。',
    '零件页不提供规格组件的系列（6600 ION）挂厂商系列产品简介 PDF 的规格总表：是系列级、按容量分列的表，不是逐型号规格；'
    '每个零件按简介的零件号规则解码容量与外形，单独计数。',
    'HBM3E 的产品简介与技术简介只放在 assets.micron.com（robots.txt 返回 403）与 Adobe 分发域（robots.txt Disallow: /），'
    '按规则不抓；官网可抓的 HBM 白皮书没有功耗数字，HBM 运行参数仍缺。',
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


def statuses_count(products, status):
    return sum(p['extraction_status'] == status for p in products)


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
        families = {seg for row in parts for seg in taxonomy_path(urlsplit(row['url']).path)}
        for family, brief in FAMILY_BRIEFS.items():
            if family in families:
                self.fetch(brief['url'], 'brief', family, suffix='pdf', refresh=refresh, cap=64 << 20)
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
            gap_reason = ''
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
                    gap_reason = '' if tables else 'specification_component_returned_no_rows'
                    extra_sources = [{'url': component_url, 'sha256': component['sha']}]
                elif component_url and component is not None:
                    status = 'specification_' + component['status']
                elif not component_url:
                    # The page names no specification component (2026-10-01: the whole 6600 ION family uses a
                    # template that only offers a datasheet accordion). Recorded, never guessed.
                    status, gap_reason = 'vendor_specification_gap', 'part_page_names_no_specification_component'
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
                'product_url': entry['url'], 'sitemap_lastmod': entry['lastmod'], 'extraction_status': status,
                **({'gap_reason': gap_reason} if gap_reason else {})})
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
        # Family product briefs: the vendor's own family table for parts whose page names no component.
        for family, brief in FAMILY_BRIEFS.items():
            row = pages.get(brief['url'])
            members = [p for p in products if p.get('gap_reason') == 'part_page_names_no_specification_component'
                       and family in [s['slug'] for s in p['taxonomy']]]
            if not members or row is None or not row['sha'] or not shutil.which('pdftotext'):
                continue
            text = subprocess.run(['pdftotext', '-layout', str(self.out / row['snapshot_path']), '-'],
                                  capture_output=True, timeout=120).stdout.decode('utf-8', 'replace')
            try:
                rows, _ = brief_rows(text, brief['start'], brief['end'])
            except StopIteration:
                continue  # the brief no longer carries the table: the gap stays a gap
            sources[brief['url']] = self._source(row, 'official_product_brief', 'pdf')
            for p in members:
                match = brief['part'].match(p['part_number'].upper())
                ff = brief['form_factor'].get(match.group(1), match.group(1) + '（简介零件号规则未列）') if match else '未能解码'
                capacity = brief['capacity'].get(match.group(2), match.group(2)) if match else '未能解码'
                p['tables'] = [{'index': 1, 'section': brief['section'], 'rows': rows, 'is_specification': True,
                                'method': 'vendor_product_brief_pdf_layout_rows',
                                'source_refs': [{'url': brief['url'], 'sha256': row['sha']}],
                                'notes': ('厂商系列产品简介 PDF 的规格总表（按容量分列；同一行的取值按印刷顺序以 " | " 分隔，'
                                          '跨列合并的取值只出现一次），是系列级而非逐型号规格。本零件按简介的零件号规则解码：'
                                          f'容量 {capacity}，外形 {ff}。原件见来源 PDF（SHA 回查）。')}]
                p['extraction_status'] = 'family_brief_table_extracted'
                p['brief_decoded'] = {'capacity': capacity, 'form_factor': ff, 'basis': 'vendor part-number scheme in the product brief'}
                p.setdefault('official_resources', []).append({'url': brief['url'], 'role': 'family_product_brief'})

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
            'named_products_current_with_part_specifications': statuses_count(active, 'native_tables_extracted'),
            'named_products_current_with_family_brief_only': statuses_count(active, 'family_brief_table_extracted'),
            'entities': len(products), 'entities_with_tables': sum(bool(p['tables']) for p in products),
            'named_products_obsolete_listed': sum(p['listing'] == 'obsolete' for p in products),
            'directory_entities': sum(p['listing'] == 'directory' for p in products),
            'current_extraction_status': statuses, 'current_official_status': official,
            'vendor_specification_gaps': statuses.get('vendor_specification_gap', 0),
            'vendor_specification_gaps_by_family': dict(sorted(
                {f: sum(1 for p in active if p['extraction_status'] == 'vendor_specification_gap' and p['category'] == f)
                 for f in {p['category'] for p in active if p['extraction_status'] == 'vendor_specification_gap'}}.items())),
            'unavailable_by_family': dict(sorted(
                {f: sum(1 for p in active if p['extraction_status'].endswith('unavailable') and p['category'] == f)
                 for f in {p['category'] for p in active if p['extraction_status'].endswith('unavailable')}}.items())),
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
                                               'named_products_current_with_part_specifications',
                                               'named_products_current_with_family_brief_only',
                                               'entities', 'entities_with_tables', 'named_products_obsolete_listed',
                                               'directory_entities', 'current_extraction_status', 'current_official_status',
                                               'requests_this_run')}, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
