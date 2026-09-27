"""Product-led discovery: immutable source snapshots and native vendor tables.

The inventory is deliberately not a claim about all shipping SKUs. Directory
entries, named product pages and unresolved families have separate denominators.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
import fcntl
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit, urlunsplit

from .inventory import InventoryFetcher, atomic_bytes, atomic_json, load_profile, run_inventory, utc_now


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def raw_text(self):
        text = ''.join(c.raw_text() if isinstance(c, Node) else c for c in self.children)
        if self.tag == 'sup':
            return text if text.strip() in {'™', '®', '©'} else '^{' + text.strip() + '}'
        if self.tag == 'sub':
            return '_{' + text.strip() + '}'
        return '\n' + text + '\n' if self.tag in {'div', 'p', 'br', 'li', 'h1', 'h2', 'h3', 'h4', 'td', 'th', 'tr'} else text

    def text(self):
        return ' '.join(self.raw_text().split())

    def walk(self, tag=None):
        if tag is None or self.tag == tag:
            yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk(tag)


class Document(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = Node('document')
        self.stack = [self.root]
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def content_nodes(root):
    """Discard site chrome, executable content and hidden consent widgets."""
    if root.tag in {'script', 'style', 'noscript', 'svg', 'nav', 'header', 'footer', 'form'}:
        return
    mark = root.attrs.get('class', '') + ' ' + root.attrs.get('id', '')
    if re.search(r'global.?nav|global.?footer|cookie|consent|breadcrumb', mark, re.I):
        return
    yield root
    for child in root.children:
        if isinstance(child, Node):
            yield from content_nodes(child)


def normalized(url, base):
    p = urlsplit(urljoin(base, url))
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
        return None
    return urlunsplit((p.scheme, p.netloc.lower(), p.path, p.query, ''))


def native_table(table):
    """Preserve original row/cell positions including spans; no guessed values."""
    rows = []
    for row in table.walk('tr'):
        cells = []
        for cell in row.children:
            if isinstance(cell, Node) and cell.tag in {'td', 'th'}:
                cells.append({'text': cell.text(), 'colspan': min(100, max(1, int(cell.attrs.get('colspan', '1')))),
                              'rowspan': min(100, max(1, int(cell.attrs.get('rowspan', '1')))), 'header': cell.tag == 'th'})
        if cells:
            rows.append(cells)
    return rows


def parse_page(body, url):
    root = Document(body.decode('utf-8', 'replace')).root
    nodes = list(content_nodes(root))
    heading = next((n.text() for n in nodes if n.tag == 'h1'), '')
    title = next((n.text() for n in root.walk('title')), '')
    section, links, tables, text = '', [], [], []
    headings, grids = {}, {}
    active = False
    for position, node in enumerate(nodes):
        if node.tag == 'h1':
            active = True
        if not active:
            continue
        if node.tag in {'h1', 'h2', 'h3', 'h4'}:
            section = node.text()
            level = int(node.tag[1])
            headings = {k: v for k, v in headings.items() if k < level}
            headings[level] = section
        context = ' / '.join(headings.values())
        spec_context = bool(re.search(r'specifications?|\bspecs\b|technical', context, re.I))
        if spec_context and node.tag == 'div' and 'nv-flexbox' in node.attrs.get('class', '').split():
            columns = [c for c in node.children if isinstance(c, Node)]
            if len(columns) >= 2 and all('nv-text' in c.attrs.get('class', '').split() for c in columns):
                grid = grids.setdefault(context, {'section': section, 'rows': [], 'notes': []})
                grid['rows'].append([{'text': c.text(), 'colspan': 1, 'rowspan': 1, 'header': False} for c in columns])
        if spec_context and context in grids and node.tag == 'p' and re.match(r'^(?:\*|\^\{|\d+\.)', node.text()):
            grids[context]['notes'].append(node.text())
        if node.tag in {'p', 'h1', 'h2', 'h3', 'h4', 'li'}:
            text.append(node.text())
        if node.tag == 'a' and node.attrs.get('href'):
            link = normalized(node.attrs['href'], url)
            if link:
                links.append({'url': link, 'label': node.text(), 'section': section})
        if node.tag == 'table':
            rows = native_table(node)
            # The vendor's own heading or field names must establish relevance.
            relevant = bool(re.search(r'specification|\bspecs\b|technical|GPU Memory|Form Factor|Memory Bandwidth', context + ' ' + node.text(), re.I))
            labels = {r[0]['text'].lower() for r in rows if r}
            relevant = relevant or len(labels & {'gpu', 'cpu', 'memory', 'storage', 'power', 'ai performance', 'dimensions', 'weight'}) >= 2
            notes = []
            # Sibling footnotes can qualify sparsity, peak performance and scope.
            descendants = {id(n) for n in node.walk()}
            for following in nodes[position + 1:]:
                if id(following) in descendants:
                    continue
                if following.tag in {'h1', 'h2', 'h3', 'h4', 'table'}:
                    break
                if following.tag == 'p' and following.text():
                    notes.append(following.text())
            tables.append({'index': len(tables) + 1, 'section': section, 'rows': rows,
                           'is_specification': relevant, 'text': node.text(), 'notes': '\n'.join(notes), 'method': 'html_table'})
    for grid in grids.values():
        tables.append({'index': len(tables) + 1, 'section': grid['section'], 'rows': grid['rows'],
            'is_specification': True, 'method': 'nvidia_official_spec_grid',
            'text': '\n'.join(' | '.join(c['text'] for c in r) for r in grid['rows']), 'notes': '\n'.join(dict.fromkeys(grid['notes']))})
    # Product family sites list individual models in their official navigation,
    # sometimes before H1 while the body only embeds a dynamic comparison grid.
    # Read only explicit model links within product paths, not all site chrome.
    if urlsplit(url).path.rstrip('/') != '/en-us/products':
        seen = {link['url'] for link in links}
        for anchor in root.walk('a'):
            link = normalized(anchor.attrs.get('href', ''), url)
            if (link and link not in seen and page_allowed(link)
                    and re.search(r'/(data-center|networking|geforce/(graphics-cards|laptops)|products|design-visualization|autonomous-machines)/', urlsplit(link).path)
                    and (MODEL.search(anchor.text()) or MODEL.search(re.sub(r'[-_/]', ' ', link)))):
                links.append({'url': link, 'label': anchor.text(), 'section': 'Official product navigation', 'role': 'model_navigation'})
                seen.add(link)
    canonical = next((normalized(n.attrs.get('href', ''), url) for n in root.walk('link')
                      if n.attrs.get('rel') == 'canonical'), None)
    return {'heading': heading, 'title': title, 'canonical': canonical, 'links': links,
            'tables': tables, 'text': '\n'.join(text)}


def page_allowed(url):
    p = urlsplit(url)
    if p.hostname not in OFFICIAL_PAGE_HOSTS or p.query:
        return False
    if p.hostname in REDIRECT_PAGE_PATHS:
        return p.path.rstrip('/') in REDIRECT_PAGE_PATHS[p.hostname]
    # www.nvidia.cn publishes its zh-CN sitemap with root-relative paths
    # (e.g. /networking/products/), unlike nvidia.com/zh-cn/.
    if p.hostname == 'www.nvidia.com' and not p.path.startswith(('/en-us/', '/zh-cn/')):
        return False
    if re.search(r'/(blogs?|news|events?|industries|case-studies|customer-stories|customer-success|research|careers|support|download|drivers|on-demand|gtc|privacy|about-nvidia|buy|shop|training|launchpad|contact|forums|community-portal|foundation|where-to-buy)(/|$)', p.path):
        return False
    return not Path(p.path).suffix or p.path.endswith(('.html', '.htm'))


def sitemap_product_category(url):
    """Return a provisional product-led category only for official catalog paths."""
    p = urlsplit(url)
    path = p.path
    if p.hostname == 'www.nvidia.com':
        path = re.sub(r'^/(?:en-us|zh-cn)(?=/)', '', path)
    rules = [
        (r'^/geforce/(?:graphics-cards|laptops)(?:/|$)', 'Gaming and Creating'),
        (r'^/networking/(?:products|data-processing-unit)(?:/|$)', 'Networking'),
        (r'^/data-center/products(?:/|$)', 'Data Center'),
        (r'^/autonomous-machines/embedded-systems(?:/|$)', 'Embedded Systems'),
        (r'^/design-visualization/(?:products|workstations)(?:/|$)', 'Professional Workstations'),
        (r'^/software(?:/|$)|^/ai-data-science/products(?:/|$)', 'Software'),
        (r'^/shield(?:/|$)', 'Gaming and Creating'),
        (r'^/products(?:/|$)', 'NVIDIA Products'),
    ]
    return next((category for pattern, category in rules if re.search(pattern, path, re.I)), None)


def sync_product_sitemap(root, db):
    """Seed only product-family URL candidates; preserve omissions for review."""
    base = Path(root).expanduser()
    latest = base / 'ledger/companies/nvidia/inventory/latest-attempt.json'
    if not latest.is_file():
        return {'status': 'not_observed', 'candidate_urls': 0, 'new': 0, 'changed': 0, 'unchanged': 0}
    summary = json.loads(latest.read_text())
    if summary.get('status') != 'sitemaps_complete':
        last_complete = latest.parent / 'latest-complete-sitemaps.json'
        if last_complete.is_file():
            candidate_summary = json.loads(last_complete.read_text())
            if candidate_summary.get('status') == 'sitemaps_complete':
                summary = candidate_summary
    manifest = (base / summary['url_manifest']).resolve()
    if not manifest.is_relative_to(base.resolve()) or not manifest.is_file():
        raise ValueError('official sitemap manifest is missing or outside the data root')
    db.execute('''CREATE TABLE IF NOT EXISTS product_sitemap_urls(
        url TEXT PRIMARY KEY, category TEXT NOT NULL, lastmod TEXT, role_data TEXT,
        state TEXT NOT NULL, observed_at TEXT NOT NULL)''')
    changes = Counter()
    for line in manifest.read_text().splitlines():
        item = json.loads(line)
        url = item.get('url', '')
        category = sitemap_product_category(url) if page_allowed(url) else None
        if not category:
            continue
        lastmods = sorted({s.get('lastmod_claim', '') for s in item.get('sources', []) if s.get('lastmod_claim')})
        lastmod = '|'.join(lastmods)
        roles = sorted({s.get('role', '') for s in item.get('sources', []) if s.get('role')})
        old = db.execute('SELECT * FROM product_sitemap_urls WHERE url=?', (url,)).fetchone()
        state = 'new' if old is None else ('changed' if old['lastmod'] != lastmod and lastmod else 'unchanged')
        changes[state] += 1
        page = db.execute('SELECT payload FROM pages WHERE url=?', (url,)).fetchone()
        db.execute('INSERT OR REPLACE INTO product_sitemap_urls VALUES(?,?,?,?,?,?)',
                   (url, category, lastmod, json.dumps(roles), 'observed' if page else 'queued', utc_now()))
        if page and state == 'unchanged':
            continue
        if page and state == 'new':
            continue
        parent = 'official-sitemap:' + category.casefold().replace(' ', '-')
        db.execute('INSERT OR IGNORE INTO frontier(url,depth,parent,category,label) VALUES(?,?,?,?,?)',
                   (url, 1, parent, category, 'Official sitemap product candidate'))
        pending = db.execute('SELECT state,error FROM frontier WHERE url=?', (url,)).fetchone()
        if not page and pending and pending['state'] == 'excluded':
            db.execute("UPDATE frontier SET state='pending',error=NULL,category=?,label=? WHERE url=?",
                       (category, 'Official sitemap product candidate', url))
        elif (not page and pending and pending['state'] == 'failed'
              and 'outside HTTPS host allowlist' in (pending['error'] or '')):
            # A code-reviewed official redirect-host allowlist may make a prior
            # candidate fetchable; retry that policy failure on the next sync.
            db.execute("UPDATE frontier SET state='pending',error=NULL,category=?,label=?,parent=? WHERE url=?",
                       (category, 'Official sitemap product candidate', parent, url))
        if state == 'changed':
            db.execute("UPDATE frontier SET state='pending',error=NULL WHERE url=?", (url,))
    db.commit()
    return {'status': summary.get('status'), 'run_id': summary.get('run_id'),
        'candidate_urls': sum(changes.values()), 'new': changes['new'], 'changed': changes['changed'],
        'unchanged': changes['unchanged'], 'lastmod_absent': sum(1 for row in db.execute('SELECT lastmod FROM product_sitemap_urls') if not row['lastmod']),
        'source_sitemaps': summary.get('sitemaps', []), 'observed_at': summary.get('finished_at')}


MODEL = re.compile(r'\b(?:[ABHLV]\d{2,3}[A-Z]*|GB\d{3}|GH\d{3}|RTX\s*(?:PRO\s*)?\d{3,4}|GeForce\s+(?:RTX|GTX)\s*\d+|ConnectX[-– ]?\d+|BlueField[-– ]?\d+|Quantum[-– ]?\d+|Spectrum[-– ]?\d+|DGX\s+(?:Spark|Station|[ABH]\d+)|Jetson\s+(?:AGX|Orin|Thor|Nano|TX\d)|SHIELD\s+TV)\b', re.I)
OFFICIAL_PAGE_HOSTS = {'www.nvidia.com', 'www.nvidia.cn', 'developer.nvidia.com',
                       'developer.nvidia.cn', 'networking-docs.nvidia.com'}
REDIRECT_PAGE_PATHS = {
    'developer.nvidia.com': {'/riva', '/topics/ai/generative-ai/riva', '/holoscan-for-media'},
    'developer.nvidia.cn': {'/riva', '/topics/ai/generative-ai/riva'},
    'networking-docs.nvidia.com': {'/software/lts-releases'},
}


def entity_kind(heading, url):
    if re.search(r'\b(?:Series|Family|Platform|Architecture)\b', heading, re.I):
        return 'family_or_directory'
    if MODEL.search(heading):
        return 'named_product'
    if '/software/' in url or re.search(r'\b(?:software|cloud|app|SDK|Nsight|NeMo|BioNeMo)\b', heading, re.I):
        return 'software_service'
    return 'family_or_directory'


def identifier(url):
    return 'nvidia-' + hashlib.sha256(url.encode()).hexdigest()[:20]


def website_page_identity(url):
    """Collapse exact EN/zh-CN URL-path counterparts across NVIDIA official hosts."""
    p = urlsplit(url)
    path = re.sub(r'^/(?:en-us|zh-cn)(?=/)', '', p.path)
    return identifier(urlunsplit(('https', 'www.nvidia.com', path.rstrip('/') or '/', '', '')))


def identity_path(url):
    p = urlsplit(url)
    return re.sub(r'^/(?:en-us|zh-cn)(?=/)', '', p.path).rstrip('/') or '/'


def source_receipt(page):
    """Compact delivery receipt; full HTML and parsed links stay on M5 blobs."""
    http = page.get('http', {})
    return {key: page[key] for key in ('source_url', 'requested_url', 'sha256', 'snapshot_path',
        'observed_at', 'heading', 'title', 'canonical', 'category', 'parent_url', 'depth', 'kind') if key in page} | {
        'http': {key: http[key] for key in ('status', 'final_url', 'content_type', 'etag', 'last_modified') if key in http}}


def sitemap_entry_for_page(page, sitemap_rows):
    """Resolve official sitemap evidence through the requested or final URL."""
    return sitemap_rows.get(page.get('requested_url')) or sitemap_rows.get(page.get('source_url'))


def product_identifier(name):
    """Stable product identity independent of the page that currently links it."""
    key = ' '.join(name.casefold().split())
    return 'nvidia-' + hashlib.sha256(('nvidia-product:' + key).encode()).hexdigest()[:20]


def section_products(page):
    """Extract explicitly named products and datasheet entry points by vendor section."""
    sections = {}
    for link in page.get('links', []):
        section = ' '.join(link.get('section', '').split())
        label = ' '.join(link.get('label', '').split())
        if not section.startswith('NVIDIA ') or not label or not MODEL.match(section.removeprefix('NVIDIA ')):
            continue
        if not re.search(r'\b(?:DPU|Processor|GPU|CPU|Adapter|Switch|Series|Platform|System)\b', section, re.I):
            continue
        # Only a card/portfolio "Explore …" link creates an entity. FAQ links,
        # articles, datasheet callouts and marketing headings add evidence only
        # after that official product-card identity is established.
        if not re.match(r'^Explore\s+', label, re.I):
            continue
        name = section if section.lower().startswith('nvidia ') else 'NVIDIA ' + section
        entry = sections.setdefault(name, {'name': name, 'resources': []})
        url = link.get('url', '')
        if ((urlsplit(url).hostname or '').endswith(('.nvidia.com', '.nvidia.cn'))
                and re.search(r'datasheet|specification|product brief|whitepaper', label + ' ' + url, re.I)):
            entry['resources'].append({'url': url, 'label': label, 'kind': 'official_resource_page',
                                       'access_status': 'not_checked'})
    for link in page.get('links', []):
        section = ' '.join(link.get('section', '').split())
        label = ' '.join(link.get('label', '').split())
        url = link.get('url', '')
        if (section in sections and (urlsplit(url).hostname or '').endswith(('.nvidia.com', '.nvidia.cn'))
                and re.search(r'datasheet|specification|product brief|whitepaper', label + ' ' + url, re.I)):
            sections[section]['resources'].append({'url': url, 'label': label, 'kind': 'official_resource_page',
                                                   'access_status': 'not_checked'})
    for item in sections.values():
        item['resources'] = list({r['url']: r for r in item['resources']}.values())
    return list(sections.values())


def enqueue_links(db, row, data):
    for link in data['links']:
        if not page_allowed(link['url']) or link['url'] == row['url']:
            continue
        label = link['label']
        directory = row['depth'] == 0
        hardware_path = re.search(r'/(?:data-center|networking|geforce/(?:graphics-cards|laptops)|design-visualization|products|autonomous-machines)/', urlsplit(link['url']).path)
        catalog_page = re.search(r'products|platform|graphics cards|laptops|networking', data['heading'], re.I)
        relevant = MODEL.search(label) or MODEL.search(link['section']) or MODEL.search(re.sub(r'[-_/]', ' ', link['url'])) or re.search(r'product|specification|technical|compare|portfolio|datasheet|hardware', label, re.I) or (catalog_page and hardware_path)
        if row['depth'] < 6 and (directory or relevant):
            category = link['section'] if directory else row['category']
            db.execute('INSERT OR IGNORE INTO memberships VALUES(?,?,?,?)', (link['url'], row['url'], category, label))
            db.execute('INSERT OR IGNORE INTO frontier(url,depth,parent,category,label) VALUES(?,?,?,?,?)',
                       (link['url'], row['depth'] + 1, row['url'], category, label))


def collect(root, *, max_pages=200, refresh=False, reparse=False, incremental=False):
    base = Path(root).expanduser() / 'product-catalog' / 'nvidia'
    base.mkdir(parents=True, exist_ok=True)
    with (base / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _collect(root, max_pages=max_pages, refresh=refresh, reparse=reparse, incremental=incremental)


def _collect(root, *, max_pages=200, refresh=False, reparse=False, incremental=False):
    root = Path(root).expanduser()
    base = root / 'product-catalog' / 'nvidia'
    base.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(base / 'discovery.sqlite3')
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE IF NOT EXISTS frontier(url TEXT PRIMARY KEY, depth INTEGER, parent TEXT, category TEXT, label TEXT, state TEXT DEFAULT 'pending', error TEXT);
    CREATE TABLE IF NOT EXISTS pages(url TEXT PRIMARY KEY, sha TEXT, observed_at TEXT, payload TEXT);
    CREATE TABLE IF NOT EXISTS history(url TEXT, sha TEXT, observed_at TEXT, PRIMARY KEY(url,sha));
    CREATE TABLE IF NOT EXISTS memberships(url TEXT, parent TEXT, category TEXT, label TEXT, PRIMARY KEY(url,parent,category));
    CREATE TABLE IF NOT EXISTS product_map(id TEXT PRIMARY KEY,name TEXT,parent_id TEXT,source_sha256 TEXT,source_url TEXT,kind TEXT,change_status TEXT,observed_at TEXT);
    CREATE TABLE IF NOT EXISTS product_map_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,product_id TEXT,observed_at TEXT,status TEXT,source_sha256 TEXT,payload TEXT);''')
    if incremental:
        inventory_report = run_inventory(load_profile('nvidia'), root)
        print(json.dumps({'stage': 'official_website_sitemap', 'status': inventory_report.get('status'),
            'candidate_urls': inventory_report.get('unique_url_candidates'),
            'reconciliation': inventory_report.get('reconciliation', {})}, ensure_ascii=False), flush=True)
    sitemap_summary = sync_product_sitemap(root, db)
    print(json.dumps({'stage': 'official_product_sitemap', **{k: v for k, v in sitemap_summary.items() if k != 'source_sitemaps'}}, ensure_ascii=False), flush=True)
    baseline = {r['id']: dict(r) for r in db.execute('SELECT id,name,parent_id,source_sha256 FROM product_map')}
    seed = 'https://www.nvidia.com/en-us/products/'
    db.execute('INSERT OR IGNORE INTO frontier(url,depth,parent,category,label) VALUES(?,0,?,?,?)', (seed, '', '', 'NVIDIA Products'))
    if refresh:
        db.execute("UPDATE frontier SET state='pending',error=NULL")
    elif incremental:
        # Revalidate only the company directory and its official category pages.
        # Product/source pages are touched only when newly linked or on --refresh.
        db.execute("UPDATE frontier SET state='pending',error=NULL WHERE depth<=2 AND state IN ('done','failed')")
    for existing in db.execute('SELECT url FROM frontier').fetchall():
        if existing['url'] != seed and not page_allowed(existing['url']):
            db.execute("UPDATE frontier SET state='excluded' WHERE url=?", (existing['url'],))
    if reparse:
        for saved in db.execute('SELECT url,payload FROM pages').fetchall():
            old = json.loads(saved['payload'])
            body = (root / old['snapshot_path']).read_bytes()
            if hashlib.sha256(body).hexdigest() != old['sha256']:
                raise ValueError('snapshot hash mismatch')
            old.update(parse_page(body, old['source_url']))
            row = db.execute('SELECT * FROM frontier WHERE url=?', (saved['url'],)).fetchone()
            enqueue_links(db, row, old)
            db.execute('UPDATE pages SET payload=? WHERE url=?', (json.dumps(old, ensure_ascii=False), saved['url']))
    db.commit()
    if reparse:
        result = export(db, base, baseline)
        db.close()
        return result
    fetcher = InventoryFetcher({'allowed_hosts': sorted(OFFICIAL_PAGE_HOSTS),
        'robots_hosts': sorted(OFFICIAL_PAGE_HOSTS),
        'delay_seconds': 1.0, 'timeout_seconds': 30, 'max_xml_bytes': 8 * 1024 * 1024})
    atomic_json(base / 'robots.json', fetcher.prepare_robots())
    # Three in-flight requests, still globally spaced by the robots crawl delay.
    rate_lock, last_start = threading.Lock(), [0.0]
    def retrieve(url, prior):
        worker = InventoryFetcher(fetcher.profile)
        worker.robots = fetcher.robots
        policy = worker.robots.get(urlsplit(url).netloc)
        with rate_lock:
            delay = max(1.0, (policy.delay or 0) if policy else 0)
            time.sleep(max(0, last_start[0] + delay - time.monotonic()))
            last_start[0] = time.monotonic()
        headers = {}
        if prior:
            if prior['http'].get('etag'):
                headers['If-None-Match'] = prior['http']['etag']
            if prior['http'].get('last_modified'):
                headers['If-Modified-Since'] = prior['http']['last_modified']
        try:
            return worker.get(url, headers=headers)
        except HTTPError as exc:
            if exc.code != 304 or not prior:
                raise
            body = (root / prior['snapshot_path']).read_bytes()
            if hashlib.sha256(body).hexdigest() != prior['sha256']:
                raise ValueError('cached snapshot hash mismatch')
            return body, {**prior['http'], 'status': 304}
    pool = ThreadPoolExecutor(max_workers=3)
    batch = []
    attempts = 0
    while attempts < max_pages:
        if not batch:
            rows = db.execute("""SELECT f.* FROM frontier f WHERE f.state='pending'
                ORDER BY CASE WHEN EXISTS (SELECT 1 FROM product_sitemap_urls s
                    WHERE s.url=f.url AND s.state!='observed') THEN 0 ELSE 1 END, f.depth, f.url LIMIT ?""",
                (min(3, max_pages-attempts),)).fetchall()
            for row in rows:
                prior = db.execute('SELECT payload FROM pages WHERE url=?', (row['url'],)).fetchone()
                batch.append((row, pool.submit(retrieve, row['url'], json.loads(prior[0]) if prior else None)))
        if not batch:
            break
        row, future = batch.pop(0)
        attempts += 1
        try:
            body, meta = future.result()
            if 'html' not in meta['content_type']:
                raise ValueError('expected HTML')
            sha = hashlib.sha256(body).hexdigest()
            blob = root / 'blobs' / sha[:2] / (sha + '.html')
            if not blob.exists():
                atomic_bytes(blob, body)
            data = parse_page(body, meta['final_url'])
            data.update({'source_url': meta['final_url'], 'requested_url': row['url'], 'sha256': sha,
                'snapshot_path': str(blob.relative_to(root)), 'observed_at': utc_now(),
                'category': row['category'], 'parent_url': row['parent'], 'depth': row['depth'],
                'kind': entity_kind(data['heading'], meta['final_url']), 'http': meta})
            db.execute('INSERT OR REPLACE INTO pages VALUES(?,?,?,?)', (row['url'], sha, data['observed_at'], json.dumps(data, ensure_ascii=False)))
            db.execute('INSERT OR IGNORE INTO history VALUES(?,?,?)', (row['url'], sha, data['observed_at']))
            db.execute("UPDATE product_sitemap_urls SET state='observed',observed_at=? WHERE url=?",
                       (data['observed_at'], row['url']))
            # Expand directories, then product-linked specification resources. Never
            # use a whole-site sitemap as an unbounded HTML download frontier.
            enqueue_links(db, row, data)
            db.execute("UPDATE frontier SET state='done',error=NULL WHERE url=?", (row['url'],))
            print(json.dumps({'page': attempts, 'heading': data['heading'], 'spec_tables': sum(t['is_specification'] for t in data['tables'])}, ensure_ascii=False), flush=True)
        except Exception as exc:
            db.execute("UPDATE frontier SET state='failed',error=? WHERE url=?", (str(exc)[:300], row['url']))
            print(json.dumps({'error': str(exc)[:200], 'url': row['url']}), flush=True)
        db.commit()
        export(db, base, baseline)
    result = export(db, base, baseline)
    pool.shutdown()
    db.close()
    return result


def export(db, base, baseline=None):
    seed = 'https://www.nvidia.com/en-us/products/'
    pages = [json.loads(r[0]) for r in db.execute('SELECT payload FROM pages ORDER BY url')]
    sitemap_rows = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_sitemap_urls'").fetchone():
        sitemap_rows = {r['url']: dict(r) for r in db.execute('SELECT * FROM product_sitemap_urls')}
    inventory_summary = None
    inventory_path = base.parent.parent / 'ledger/companies/nvidia/inventory/latest-attempt.json'
    if inventory_path.is_file():
        try:
            inventory_summary = json.loads(inventory_path.read_text())
        except (OSError, json.JSONDecodeError):
            inventory_summary = None
    entities = {}
    known_id_by_path = {}
    for known in db.execute('SELECT id,source_url FROM product_map'):
        if known['source_url'] and urlsplit(known['source_url']).hostname in {'www.nvidia.com', 'www.nvidia.cn'}:
            known_id_by_path.setdefault(identity_path(known['source_url']), known['id'])
    for page in pages:
        if page.get('depth', 1) == 0 or not page.get('heading') or not page_allowed(page['source_url']):
            continue
        url = page['source_url']
        key = known_id_by_path.get(identity_path(url), website_page_identity(url))
        categories = sorted({r[0] for r in db.execute('SELECT category FROM memberships WHERE url=?', (page['requested_url'],))} | {page['category']})
        candidate = {'id': key, 'name': page['heading'], 'category': ' / '.join(c for c in categories if c), 'categories': categories,
            'kind': entity_kind(page['heading'], url), 'availability': 'not_verified', 'identity_status': 'official_page_observed',
            'source_url': url, 'source_sha256': page['sha256'], 'observed_at': page['observed_at'],
            'tables': [t for t in page['tables'] if t['is_specification']],
            'attachments': [l for l in page['links'] if re.search(r'\.(pdf|docx?|pptx?|xlsx?)(?:$|\?)', l['url'], re.I)
                            and (urlsplit(l['url']).hostname or '').endswith(('.nvidia.com', '.nvidia.cn'))],
            'extraction_status': 'native_tables_extracted' if any(t['is_specification'] for t in page['tables']) else 'specification_search_pending',
            'official_pages': [{'url': url, 'sha256': page['sha256']} ]}
        # Match by the requested source URL first: language/legacy paths may
        # redirect to a canonical URL that is not itself in the XML sitemap.
        sitemap_entry = sitemap_entry_for_page(page, sitemap_rows)
        candidate['website_sitemap'] = {'matched': sitemap_entry is not None,
            'roles': json.loads(sitemap_entry['role_data']) if sitemap_entry else [],
            'lastmod_claims': sitemap_entry['lastmod'].split('|') if sitemap_entry and sitemap_entry['lastmod'] else [],
            'candidate_status': sitemap_entry['state'] if sitemap_entry else 'not_a_product_path'}
        old = entities.get(key)
        if old is None:
            entities[key] = candidate
        else:
            old['official_pages'] = list({p['url']: p for p in old.get('official_pages', []) + candidate['official_pages']}.values())
            old['categories'] = sorted(set(old.get('categories', []) + candidate['categories']))
            old['category'] = ' / '.join(old['categories'])
            old['tables'] = list({hashlib.sha256(json.dumps(t, sort_keys=True, ensure_ascii=False).encode()).hexdigest(): t
                                  for t in old['tables'] + candidate['tables']}.values())
            old['attachments'] = list({a['url']: a for a in old['attachments'] + candidate['attachments']}.values())
            sitemap = old['website_sitemap']
            sitemap['matched'] = sitemap['matched'] or candidate['website_sitemap']['matched']
            sitemap['roles'] = sorted(set(sitemap['roles'] + candidate['website_sitemap']['roles']))
            sitemap['lastmod_claims'] = sorted(set(sitemap['lastmod_claims'] + candidate['website_sitemap']['lastmod_claims']))
            # Prefer the English page as display identity while retaining both source snapshots.
            if url.startswith('https://www.nvidia.com/en-us/'):
                for field in ('name', 'source_url', 'source_sha256', 'observed_at', 'kind', 'extraction_status'):
                    old[field] = candidate[field]
            if not old['tables'] and candidate['tables']:
                old['tables'] = candidate['tables']
            old['extraction_status'] = 'native_tables_extracted' if old['tables'] else 'specification_search_pending'
    frontier = [dict(r) for r in db.execute('SELECT * FROM frontier ORDER BY depth,url')]
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='component_products'").fetchone():
        native_models = {re.sub(r'^NVIDIA\s+', '', e['name']).casefold() for e in entities.values()
                         if e['kind'] == 'named_product' and e['tables']}
        for row in db.execute('SELECT payload FROM component_products'):
            product = json.loads(row[0])
            if re.sub(r'^NVIDIA\s+', '', product['name']).casefold() not in native_models:
                entities[product['id']] = product
        pages.extend(json.loads(row[0]) for row in db.execute('SELECT payload FROM component_sources'))
    # Split explicitly named model sections from a platform/family page. Keep
    # the official page as identity evidence and represent non-file datasheet
    # links as resource entry points (not as downloaded attachments).
    expanded = dict(entities)
    for page in pages:
        if page.get('depth', 1) == 0 or not page.get('heading') or not page_allowed(page['source_url']):
            continue
        parent_id = known_id_by_path.get(identity_path(page['source_url']), website_page_identity(page['source_url']))
        parent = expanded.get(parent_id)
        if not parent:
            continue
        for child in section_products(page):
            product_id = product_identifier(child['name'])
            candidate = {
                'id': product_id, 'name': child['name'], 'category': parent['category'],
                'categories': parent['categories'], 'kind': 'named_product',
                'availability': 'not_verified', 'identity_status': 'official_product_section_observed',
                'parent_id': parent_id, 'product_url': page['source_url'],
                'source_url': page['source_url'], 'source_sha256': page['sha256'],
                'observed_at': page['observed_at'], 'tables': [], 'attachments': [],
                'official_resources': child['resources'],
                'official_pages': [{'url': page['source_url'], 'sha256': page['sha256']}],
                'website_sitemap': {'matched': page['source_url'] in sitemap_rows,
                    'roles': json.loads(sitemap_rows[page['source_url']]['role_data']) if page['source_url'] in sitemap_rows else [],
                    'lastmod_claims': sitemap_rows[page['source_url']]['lastmod'].split('|') if page['source_url'] in sitemap_rows and sitemap_rows[page['source_url']]['lastmod'] else [],
                    'candidate_status': sitemap_rows[page['source_url']]['state'] if page['source_url'] in sitemap_rows else 'not_a_product_path'},
                '_parent_depth': page.get('depth', 99),
                'extraction_status': 'specification_search_pending'}
            old_child = expanded.get(product_id)
            if old_child:
                resources = {r['url']: r for r in old_child.get('official_resources', [])}
                resources.update({r['url']: r for r in candidate['official_resources']})
                candidate['official_resources'] = list(resources.values())
                page_refs = {r['url']: r for r in old_child.get('official_pages', [])}
                page_refs.update({r['url']: r for r in candidate['official_pages']})
                candidate['official_pages'] = list(page_refs.values())
                if old_child.get('_parent_depth', 99) < candidate['_parent_depth']:
                    candidate.update(parent_id=old_child['parent_id'], product_url=old_child['product_url'],
                                     source_url=old_child['source_url'], source_sha256=old_child['source_sha256'],
                                     observed_at=old_child['observed_at'], category=old_child['category'],
                                     categories=old_child['categories'], _parent_depth=old_child['_parent_depth'])
            expanded[product_id] = candidate
    for product in expanded.values():
        product.pop('_parent_depth', None)
    previous = baseline if baseline is not None else {r['id']: dict(r) for r in db.execute('SELECT id,name,parent_id,source_sha256 FROM product_map')}
    changes = Counter()
    for product in expanded.values():
        old = previous.get(product['id'])
        status = 'new' if old is None else ('changed' if old['source_sha256'] != product['source_sha256'] or old['name'] != product['name'] or old['parent_id'] != product.get('parent_id') else 'unchanged')
        product['map_change_status'] = status
        changes[status] += 1
        db.execute('INSERT OR REPLACE INTO product_map VALUES(?,?,?,?,?,?,?,?)',
                   (product['id'], product['name'], product.get('parent_id'), product['source_sha256'],
                    product['source_url'], product['kind'], status, product['observed_at']))
        if status != 'unchanged':
            db.execute('INSERT INTO product_map_events(product_id,observed_at,status,source_sha256,payload) SELECT ?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM product_map_events WHERE product_id=? AND source_sha256=? AND status=?)',
                       (product['id'], product['observed_at'], status, product['source_sha256'], json.dumps(product, ensure_ascii=False), product['id'], product['source_sha256'], status))
    db.commit()
    counts = Counter(e['kind'] for e in expanded.values())
    sitemap_matched = sum(bool(e.get('website_sitemap', {}).get('matched')) for e in expanded.values())
    product_urls_observed = sum(r['state'] == 'observed' for r in sitemap_rows.values())
    sitemap_info = {'status': inventory_summary.get('status'), 'run_id': inventory_summary.get('run_id'),
        'observed_at': inventory_summary.get('finished_at'), 'candidate_urls': inventory_summary.get('unique_url_candidates'),
        'product_path_candidates': len(sitemap_rows), 'product_path_observed': product_urls_observed,
        'product_path_pending': len(sitemap_rows) - product_urls_observed,
        'matched_catalog_sources': sitemap_matched,
        'product_sitemaps_complete': bool(sitemap_rows) and product_urls_observed == len(sitemap_rows),
        'whole_website_inventory_complete': False,
        'sitemap_sources': [{'role': s.get('role'), 'url': s.get('url'), 'entries': s.get('entries'), 'sha256': s.get('sha256')}
                            for s in (inventory_summary or {}).get('sitemaps', [])]}
    bundle = {'schema_version': 1, 'company_id': 'nvidia', 'generated_at': utc_now(),
        'coverage': {'complete': False, 'directory_url': 'https://www.nvidia.com/en-us/products/',
            'directory_entries': sum(r['depth'] == 1 and r['parent'] == seed for r in frontier), 'pages_observed': len(pages),
            'pending_pages': sum(r['state'] == 'pending' for r in frontier), 'failed_pages': sum(r['state'] == 'failed' for r in frontier),
            'entity_counts': dict(counts), 'with_spec_tables': sum(bool(e['tables']) for e in entities.values()),
            'website_sitemap': sitemap_info,
            'limitations': ['官网目录入口不等于全部具体 SKU；产品身份、配置拆分和在售状态仍需核对。',
                '仅从官方目录和产品相关链接扩展；未解析的动态表格、PDF 及独立文档站规格保留待提取。',
                '参数保留官方表格、列名、合并单元格和脚注；尚未自动映射跨产品通用字段。']},
        'product_map': {'policy': 'Products absent from a partial observation are retained; removal requires an explicit, completed official-directory comparison and review.',
            'changes': dict(changes), 'entries': len(expanded)},
        'products': list(expanded.values()), 'sources': [source_receipt(page) for page in pages], 'frontier': frontier}
    atomic_json(base / 'catalog.json', bundle)
    atomic_json(base / 'product-sitemap.json', {'schema_version': 1, 'company_id': 'nvidia',
        'generated_at': bundle['generated_at'], 'directory_url': bundle['coverage']['directory_url'],
        'coverage': sitemap_info, 'entries': [{'product_id': e['id'], 'name': e['name'],
            'parent_id': e.get('parent_id'), 'category': e['category'], 'entity_kind': e['kind'],
            'source_url': e['source_url'], 'source_sha256': e['source_sha256'],
            'map_change_status': e.get('map_change_status'), 'extraction_status': e['extraction_status'],
            'website_sitemap': e.get('website_sitemap', {})} for e in expanded.values()]})
    return bundle['coverage']


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', default='~/.local/share/fetchspec')
    ap.add_argument('--max-pages', type=int, default=200)
    ap.add_argument('--refresh', action='store_true')
    ap.add_argument('--incremental', action='store_true', help='Revalidate only the official directory and category pages; retain all known product entries')
    ap.add_argument('--reparse', action='store_true', help='Re-extract existing snapshots without downloading them again')
    args = ap.parse_args(argv)
    if args.max_pages < 1:
        ap.error('--max-pages must be positive')
    if args.refresh and args.incremental:
        ap.error('--refresh and --incremental are mutually exclusive')
    print(json.dumps(collect(args.out, max_pages=args.max_pages, refresh=args.refresh, reparse=args.reparse, incremental=args.incremental), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
