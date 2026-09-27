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

from .inventory import InventoryFetcher, atomic_bytes, atomic_json, utc_now


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
    canonical = next((normalized(n.attrs.get('href', ''), url) for n in root.walk('link')
                      if n.attrs.get('rel') == 'canonical'), None)
    return {'heading': heading, 'title': title, 'canonical': canonical, 'links': links,
            'tables': tables, 'text': '\n'.join(text)}


def page_allowed(url):
    p = urlsplit(url)
    if p.hostname not in {'www.nvidia.com', 'www.nvidia.cn'} or p.query:
        return False
    if not p.path.startswith(('/en-us/', '/zh-cn/')):
        return False
    if re.search(r'/(blogs?|news|events?|industries|case-studies|customer-stories|customer-success|research|careers|support|download|drivers|on-demand|gtc|privacy|about-nvidia|buy|shop|training|launchpad|contact|forums|community-portal|foundation|where-to-buy)(/|$)', p.path):
        return False
    return not Path(p.path).suffix or p.path.endswith(('.html', '.htm'))


MODEL = re.compile(r'\b(?:[ABHLV]\d{2,3}[A-Z]*|GB\d{3}|GH\d{3}|RTX\s*(?:PRO\s*)?\d{3,4}|GeForce\s+(?:RTX|GTX)\s*\d+|ConnectX[-– ]?\d+|BlueField[-– ]?\d+|Quantum[-– ]?\d+|Spectrum[-– ]?\d+|DGX\s+(?:Spark|Station|[ABH]\d+)|Jetson\s+(?:AGX|Orin|Thor|Nano|TX\d)|SHIELD\s+TV)\b', re.I)


def entity_kind(heading, url):
    if re.search(r'\b(?:Series|Platform|Architecture)\b', heading, re.I):
        return 'family_or_directory'
    if MODEL.search(heading):
        return 'named_product'
    if '/software/' in url or re.search(r'\b(?:software|cloud|app|SDK|Nsight|NeMo|BioNeMo)\b', heading, re.I):
        return 'software_service'
    return 'family_or_directory'


def identifier(url):
    return 'nvidia-' + hashlib.sha256(url.encode()).hexdigest()[:20]


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


def collect(root, *, max_pages=200, refresh=False, reparse=False):
    base = Path(root).expanduser() / 'product-catalog' / 'nvidia'
    base.mkdir(parents=True, exist_ok=True)
    with (base / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _collect(root, max_pages=max_pages, refresh=refresh, reparse=reparse)


def _collect(root, *, max_pages=200, refresh=False, reparse=False):
    root = Path(root).expanduser()
    base = root / 'product-catalog' / 'nvidia'
    base.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(base / 'discovery.sqlite3')
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE IF NOT EXISTS frontier(url TEXT PRIMARY KEY, depth INTEGER, parent TEXT, category TEXT, label TEXT, state TEXT DEFAULT 'pending', error TEXT);
    CREATE TABLE IF NOT EXISTS pages(url TEXT PRIMARY KEY, sha TEXT, observed_at TEXT, payload TEXT);
    CREATE TABLE IF NOT EXISTS history(url TEXT, sha TEXT, observed_at TEXT, PRIMARY KEY(url,sha));
    CREATE TABLE IF NOT EXISTS memberships(url TEXT, parent TEXT, category TEXT, label TEXT, PRIMARY KEY(url,parent,category));''')
    seed = 'https://www.nvidia.com/en-us/products/'
    db.execute('INSERT OR IGNORE INTO frontier(url,depth,parent,category,label) VALUES(?,0,?,?,?)', (seed, '', '', 'NVIDIA Products'))
    if refresh:
        db.execute("UPDATE frontier SET state='pending',error=NULL")
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
    fetcher = InventoryFetcher({'allowed_hosts': ['www.nvidia.com', 'www.nvidia.cn'],
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
            rows = db.execute("SELECT * FROM frontier WHERE state='pending' ORDER BY depth,url LIMIT ?", (min(3, max_pages-attempts),)).fetchall()
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
            # Expand directories, then product-linked specification resources. Never
            # use a whole-site sitemap as an unbounded HTML download frontier.
            enqueue_links(db, row, data)
            db.execute("UPDATE frontier SET state='done',error=NULL WHERE url=?", (row['url'],))
            print(json.dumps({'page': attempts, 'heading': data['heading'], 'spec_tables': sum(t['is_specification'] for t in data['tables'])}, ensure_ascii=False), flush=True)
        except Exception as exc:
            db.execute("UPDATE frontier SET state='failed',error=? WHERE url=?", (str(exc)[:300], row['url']))
            print(json.dumps({'error': str(exc)[:200], 'url': row['url']}), flush=True)
        db.commit()
        export(db, base)
    result = export(db, base)
    pool.shutdown()
    db.close()
    return result


def export(db, base):
    pages = [json.loads(r[0]) for r in db.execute('SELECT payload FROM pages ORDER BY url')]
    entities = {}
    for page in pages:
        if page['depth'] == 0 or not page['heading'] or not page_allowed(page['source_url']):
            continue
        url = page['source_url']
        key = identifier(url)
        if key in entities:
            continue
        categories = sorted({r[0] for r in db.execute('SELECT category FROM memberships WHERE url=?', (page['requested_url'],))} | {page['category']})
        entities[key] = {'id': key, 'name': page['heading'], 'category': ' / '.join(c for c in categories if c), 'categories': categories,
            'kind': entity_kind(page['heading'], url), 'availability': 'not_verified', 'identity_status': 'official_page_observed',
            'source_url': url, 'source_sha256': page['sha256'], 'observed_at': page['observed_at'],
            'tables': [t for t in page['tables'] if t['is_specification']],
            'attachments': [l for l in page['links'] if re.search(r'\.(pdf|docx?|pptx?|xlsx?)(?:$|\?)', l['url'], re.I)
                            and (urlsplit(l['url']).hostname or '').endswith(('.nvidia.com', '.nvidia.cn'))],
            'extraction_status': 'native_tables_extracted' if any(t['is_specification'] for t in page['tables']) else 'specification_search_pending'}
    frontier = [dict(r) for r in db.execute('SELECT * FROM frontier ORDER BY depth,url')]
    counts = Counter(e['kind'] for e in entities.values())
    bundle = {'schema_version': 1, 'company_id': 'nvidia', 'generated_at': utc_now(),
        'coverage': {'complete': False, 'directory_url': 'https://www.nvidia.com/en-us/products/',
            'directory_entries': sum(r['depth'] == 1 for r in frontier), 'pages_observed': len(pages),
            'pending_pages': sum(r['state'] == 'pending' for r in frontier), 'failed_pages': sum(r['state'] == 'failed' for r in frontier),
            'entity_counts': dict(counts), 'with_spec_tables': sum(bool(e['tables']) for e in entities.values()),
            'limitations': ['官网目录入口不等于全部具体 SKU；产品身份、配置拆分和在售状态仍需核对。',
                '仅从官方目录和产品相关链接扩展；未解析的动态表格、PDF 及独立文档站规格保留待提取。',
                '参数保留官方表格、列名、合并单元格和脚注；尚未自动映射跨产品通用字段。']},
        'products': list(entities.values()), 'sources': pages, 'frontier': frontier}
    atomic_json(base / 'catalog.json', bundle)
    return bundle['coverage']


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', default='~/.local/share/fetchspec')
    ap.add_argument('--max-pages', type=int, default=200)
    ap.add_argument('--refresh', action='store_true')
    ap.add_argument('--reparse', action='store_true', help='Re-extract existing snapshots without downloading them again')
    args = ap.parse_args(argv)
    if args.max_pages < 1:
        ap.error('--max-pages must be positive')
    print(json.dumps(collect(args.out, max_pages=args.max_pages, refresh=args.refresh, reparse=args.reparse), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
