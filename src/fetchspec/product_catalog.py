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
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
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
    if root.tag in {'script', 'style', 'noscript', 'svg', 'nav', 'footer', 'form'}:
        return
    mark = root.attrs.get('class', '') + ' ' + root.attrs.get('id', '')
    # NVIDIA Networking Docs wraps each article's h1 in a plain <header>.
    # Drop the separate site-level header but preserve article headings.
    if root.tag == 'header' and (root.attrs.get('data-component') == 'header'
                                 or re.search(r'(^|\s)header(\s|$)', root.attrs.get('class', ''), re.I)):
        return
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
    query = p.query
    if p.hostname == 'resources.nvidia.com' and re.fullmatch(r'/en-us-[a-z0-9-]+/[a-z0-9-]+/?', p.path, re.I):
        # Resource viewer links commonly carry analytics IDs. They are not
        # part of product identity and can otherwise hide an allowed page
        # from the deterministic frontier's query-free URL policy.
        query = ''
    return urlunsplit((p.scheme, p.netloc.lower(), p.path, query, ''))


def product_identity_name(name):
    """Normalize official EN/zh labels to one durable model/entity key."""
    value = unescape(re.sub(r'<[^>]*>', ' ', name or '')).casefold()
    for source, target in [('张量核心', 'tensor core'), ('tensor 核心', 'tensor core'),
                           ('核心', 'core'), ('英伟达', 'nvidia'), ('显卡', 'graphics card'),
                           ('开发者套件', 'developer kit'), ('开发套件', 'developer kit'),
                           ('工作站版', 'workstation edition'), ('服务器版', 'server edition'),
                           ('系列', 'series'), ('数据表', ' '), ('规格表', ' '),
                           ('™', ''), ('®', ''), ('©', '')]:
        value = value.replace(source, target)
    value = re.sub(r'\bnvidia\b|\bgeforce\b', ' ', value)
    value = re.sub(r'\b(?:gpu|graphics card|product specs?|datasheet|data sheet|user manual|whitepaper|product brief)\b', ' ', value)
    value = re.sub(r'\s*\d+\s*$', '', value) if re.search(r'[¹²³⁴⁵⁶⁷⁸⁹⁰]$', value) else value
    value = re.sub(r'[^a-z0-9]+', ' ', value).strip()
    return ' '.join(value.split())


def native_table(table):
    """Preserve original row/cell positions including spans; no guessed values."""
    rows = []
    for row in table.walk('tr'):
        cells = []
        for cell in row.children:
            if isinstance(cell, Node) and cell.tag in {'td', 'th'}:
                def span(name):
                    try:
                        return min(100, max(1, int(cell.attrs.get(name) or 1)))
                    except (TypeError, ValueError):
                        return 1
                cells.append({'text': cell.text(), 'colspan': span('colspan'),
                              'rowspan': span('rowspan'), 'header': cell.tag == 'th'})
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
    # NVIDIA's resource library hosts PDF.js viewers; the viewer iframe is
    # the authoritative file URL even though its Download button is JS-only.
    for node in root.walk():
        if node.tag in {'iframe', 'embed'} and node.attrs.get('src'):
            link = normalized(node.attrs['src'], url)
            if link:
                links.append({'url': link, 'label': node.attrs.get('title', ''),
                              'section': section, 'role': 'embedded_official_document'})
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
    # NVIDIA's product-resource portal contains public datasheet landing
    # pages as well as gated campaigns and unrelated collateral. Follow only
    # one-level English resource-library pages discovered from an approved
    # NVIDIA product page; direct files stay with the document pipeline.
    if p.hostname == 'resources.nvidia.com':
        return bool(re.fullmatch(r'/en-us-[a-z0-9-]+/[a-z0-9-]+/?', p.path, re.I))
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


MODEL = re.compile(r'\b(?:[ABHLV]\d{2,3}[A-Z]*|GB\d{3}|GH\d{3}|SN\d{4}|RTX\s*(?:PRO\s*)?\d{3,4}|GeForce\s+(?:RTX|GTX)\s*\d+|ConnectX[-– ]?\d+|BlueField[-– ]?\d+|Quantum[-– ]?\d+|Spectrum[-– ]?\d+|DGX\s+(?:Spark|Station|Rubin(?:\s+NVL\d+)?|Vera\s+Rubin\s+NVL\d+|[ABH]\d+)|Jetson\s+(?:AGX|Orin|Thor|Nano|TX\d)|SHIELD\s+TV)\b', re.I)
OFFICIAL_PAGE_HOSTS = {'www.nvidia.com', 'www.nvidia.cn', 'developer.nvidia.com',
                       'developer.nvidia.cn', 'networking-docs.nvidia.com',
                       'resources.nvidia.com'}
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


def resource_product_heading(heading, url):
    """Turn a resource-viewer document title into the product it evidences."""
    if urlsplit(url).hostname != 'resources.nvidia.com':
        return heading
    clean = re.sub(r'\s+(?:Datasheet|Technical Brief|Product Brief)\s*$', '', heading, flags=re.I).strip()
    if clean == heading.strip():
        return heading
    if not clean.lower().startswith('nvidia ') and re.match(
            r'^(?:SN\d{4}|Spectrum(?:-X|-\d+)|Blackwell|DGX|RTX\s+PRO)\b', clean, re.I):
        clean = 'NVIDIA ' + clean
    return clean


def consolidate_resource_viewers(products):
    """Attach datasheet viewers to products instead of counting documents as products."""
    products = dict(products)
    resource_ids = [key for key, item in products.items()
                    if urlsplit(item.get('source_url', '')).hostname == 'resources.nvidia.com']

    def subject_tokens(name):
        value = product_identity_name(name)
        return {token for token in re.findall(r'[a-z]+|\d+', value)
                if token not in {'nvidia', 'datasheet', 'technical', 'product', 'brief'}}

    for key in resource_ids:
        evidence = products.get(key)
        if evidence is None:
            continue
        tokens = subject_tokens(evidence['name'])
        candidates = [item for item in products.values()
            if item['id'] != key
            and urlsplit(item.get('source_url', '')).hostname != 'resources.nvidia.com'
            and tokens and tokens <= subject_tokens(item['name'])]
        if not candidates:
            continue
        # Prefer an exact normalized title, then the shortest official title
        # containing the evidence subject (SN6000 -> SN6000 Series).
        target = min(candidates, key=lambda item: (
            product_identity_name(item['name']) != product_identity_name(evidence['name']),
            len(subject_tokens(item['name'])), item['name']))
        target['official_pages'] = list({page['url']: page for page in
            target.get('official_pages', []) + evidence.get('official_pages', [])}.values())
        target['attachments'] = list({item['url']: item for item in
            target.get('attachments', []) + evidence.get('attachments', [])}.values())
        target['official_resources'] = list({item['url']: item for item in
            target.get('official_resources', []) + evidence.get('official_resources', [])}.values())
        table_map = {}
        for table in target.get('tables', []) + evidence.get('tables', []):
            identity = hashlib.sha256(json.dumps(
                {'rows': table.get('rows'), 'notes': table.get('notes', '')},
                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            table_map.setdefault(identity, table)
        target['tables'] = list(table_map.values())
        if target['tables']:
            target['extraction_status'] = 'native_tables_extracted'
        products.pop(key)
    return products


def frontier_failure_state(exc):
    """Separate terminal vendor/policy outcomes from retryable crawl failures."""
    if isinstance(exc, HTTPError) and exc.code == 404:
        return 'unavailable'
    if 'redirect or URL outside HTTPS host allowlist' in str(exc):
        return 'policy_blocked'
    return 'failed'


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


def previous_page_identities(base):
    """Return only the prior delivered page entities, never stale map rows."""
    path = Path(base) / 'catalog.json'
    if not path.is_file():
        return {}, {}
    try:
        products = json.loads(path.read_text()).get('products', [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}, {}
    exact, localized_path = {}, {}
    for product in products:
        if product.get('kind') == 'named_product' or not product.get('id'):
            continue
        url = product.get('source_url', '')
        if not page_allowed(url):
            continue
        exact.setdefault(url, product['id'])
        localized_path.setdefault(identity_path(url), product['id'])
    return exact, localized_path


def source_receipt(page):
    """Compact delivery receipt; full HTML and parsed links stay on M5 blobs."""
    http = page.get('http', {})
    return {key: page[key] for key in ('source_url', 'requested_url', 'sha256', 'snapshot_path',
        'observed_at', 'heading', 'title', 'canonical', 'category', 'parent_url', 'depth', 'kind') if key in page} | {
        'http': {key: http[key] for key in ('status', 'final_url', 'content_type', 'etag', 'last_modified') if key in http}}


def pdf_spec_tables(path):
    """Extract only native text rows under an explicit PDF specification heading."""
    tool = shutil.which('pdftotext')
    if not tool:
        return []
    result = subprocess.run([tool, '-layout', '-nopgbrk', str(path), '-'],
                            capture_output=True, text=True, timeout=90, check=False)
    if result.returncode or not result.stdout.strip():
        return []
    return pdf_spec_tables_from_text(result.stdout)


def _pdf_right_panel_rows(lines, start, boundary):
    """Read a brochure's explicit right-hand spec panel without left prose."""
    rows = []
    label = value = ''
    separated = False

    def flush():
        nonlocal label, value
        if label and value:
            rows.append((' '.join(label.split()), ' '.join(value.split())))
        label = value = ''

    for raw in lines[start + 1:]:
        if re.search(r'\|\s*(?:Datasheet|Product Brief|Technical Brief)\s*\|', raw, re.I):
            break
        if len(raw) > boundary and raw[max(0, boundary - 3):boundary].strip():
            # No whitespace gutter: this is left-column prose crossing the x
            # coordinate, not text inside the spec panel.
            separated = True
            continue
        region = raw[boundary:] if len(raw) > boundary else ''
        content = region.strip()
        if not content:
            separated = True
            continue
        if re.search(r'\|\s*(?:Datasheet|Product Brief|Technical Brief)\s*\|', content, re.I):
            break
        if re.match(r'^(?:ready to get started|for more information|to learn more|准备好开始了吗|如需详细了解)', content, re.I):
            break
        if re.match(r'^(?:key\s+)?features\*?\s*$', content, re.I):
            break
        leading = len(region) - len(region.lstrip())
        speed = re.match(r'^((?:InfiniBand|Ethernet)\s+Speeds)\s+(.+)$', content, re.I)
        parts = re.split(r'\s{2,}', content)
        split = ((speed.group(1), speed.group(2)) if speed else
                 ((parts[0].strip(), ' '.join(p.strip() for p in parts[1:] if p.strip()))
                  if len(parts) >= 2 else None))
        if split and split[0] and split[1]:
            next_label, next_value = split
            fragments = (not separated and label and len(label.split()) <= 2
                         and re.match(r'^[a-z]', next_label)
                         and len(next_label.split()) == 1 and len(label.split()) + 1 <= 3)
            if label and value and not fragments:
                flush()
            if fragments:
                label += ' ' + next_label
                value += ' ' + next_value.lstrip('>•').strip()
            else:
                label, value = next_label, next_value.lstrip('>•').strip()
            separated = False
            continue
        if content.startswith(('>', '•')):
            item = content.lstrip('>•').strip()
            if label and item:
                value += ('; ' if value else '') + item
            separated = False
            continue
        if label and value and leading > 0:
            value += ' ' + content.lstrip('>•').strip()
            separated = False
            continue
        if label and value:
            # Chinese wrapped labels do not provide lowercase-word clues;
            # title-cased English text is more likely the next parameter.
            fragment = (not separated and (len(label.split()) + len(content.split()) <= 4
                        or bool(re.search(r'[\u3400-\u9fff]', content))))
            if not fragment:
                flush()
        label = (label + ' ' + content).strip()
        separated = False
    flush()
    return rows


def _pdf_bullet_panel(lines, start, boundary, section):
    """Preserve an explicit vendor Portfolio/Specifications bullet panel."""
    items, pending = [], ''
    for raw in lines[start + 1:]:
        if re.search(r'\|\s*(?:Datasheet|Product Brief|Technical Brief)\s*\|', raw, re.I):
            break
        if len(raw) > boundary and raw[max(0, boundary - 3):boundary].strip():
            continue
        content = (raw[boundary:] if len(raw) > boundary else '').strip()
        if not content:
            continue
        if re.match(r'^(?:key\s+)?features\*?\s*$', content, re.I):
            break
        if content.startswith(('>', '•')):
            value = content.lstrip('>•').strip()
            # Some vendor PDFs repeat the bullet glyph on a wrapped visual
            # line (for example ``1 GbE out-of-band`` / ``management port``).
            if pending and value[:1].islower():
                pending += ' ' + value
                continue
            if pending:
                items.append(pending)
            pending = value
        elif pending:
            pending += ' ' + content
    if pending:
        items.append(pending)
    if len(items) < 3:
        return None
    return {'section': section, 'rows': [
        [{'text': 'Official specification item', 'colspan': 1, 'rowspan': 1, 'header': False},
         {'text': item, 'colspan': 1, 'rowspan': 1, 'header': False}]
        for item in items]}


def _pdf_key_features(lines, start):
    """Parse explicit left-label/right-value Key Features tables."""
    rows = []
    label = ''
    values = []

    def flush():
        nonlocal label, values
        if label and values:
            rows.append((label, '; '.join(values)))
        label, values = '', []

    for raw in lines[start + 1:]:
        if re.search(r'\|\s*(?:Datasheet|Product Brief|Technical Brief)\s*\|', raw, re.I):
            break
        if not raw.strip():
            continue
        match = re.match(r'^\s*(?P<label>\S.*?)\s{2,}>\s*(?P<value>.+?)\s*$', raw)
        continuation = re.match(r'^\s{20,}>?\s*(?P<value>\S.+?)\s*$', raw)
        if match:
            flush()
            label = ' '.join(match.group('label').split())
            values = [' '.join(match.group('value').split())]
        elif label and continuation:
            value = ' '.join(continuation.group('value').lstrip('•').split())
            if value:
                values.append(value)
        elif rows or label:
            break
    flush()
    if len(rows) < 3:
        return None
    return {'section': 'Key Features', 'rows': [
        [{'text': label, 'colspan': 1, 'rowspan': 1, 'header': False},
         {'text': value, 'colspan': 1, 'rowspan': 1, 'header': False}]
        for label, value in rows]}


def _pdf_model_matrix(lines, start):
    """Preserve native multi-model PDF matrices and their wrapped cells."""
    header_index = next((i for i in range(start + 1, min(len(lines), start + 12))
        if re.match(r'^\s*Switch Model\b', lines[i], re.I)), None)
    if header_index is None:
        return None
    header = lines[header_index]
    models = [(m.start(), m.group()) for m in re.finditer(r'\bSN\d{4}(?:-[A-Z]+)?\b', header, re.I)]
    if len(models) < 2:
        return None
    positions = [position for position, _ in models]
    # Headers are centered above their columns, while values are left- or
    # right-aligned inside those columns.  Slicing at the header starts cuts
    # real values in half (``16-core`` became ``re``).  Use the midpoints
    # between model headings as the stable inter-column gutters and leave a
    # modest label gutter before the first centered heading.
    label_boundary = max(20, positions[0] - 15)
    boundaries = [label_boundary] + [
        (positions[index] + positions[index + 1]) // 2
        for index in range(len(positions) - 1)]
    raw_rows = [('Switch Model', [name for _, name in models])]
    for raw in lines[header_index + 1:]:
        if raw.lstrip().startswith('*') or re.search(r'\|\s*Datasheet\s*\|', raw, re.I):
            if len(raw_rows) > 1:
                break
            continue
        label = raw[:label_boundary].strip()
        values = [raw[boundary:(boundaries[i + 1] if i + 1 < len(boundaries) else None)].strip()
                  for i, boundary in enumerate(boundaries)]
        if not label and not any(values):
            continue
        if label and any(values):
            raw_rows.append((label, values))
        elif len(raw_rows) > 1:
            old_label, old_values = raw_rows[-1]
            if label:
                old_label += ' ' + label
            old_values = [(old_values[i] + ' ' + values[i]).strip() for i in range(len(models))]
            raw_rows[-1] = (old_label, old_values)
    if len(raw_rows) < 4:
        return None
    return {'section': 'Technical Specifications', 'rows': [[
        {'text': label, 'colspan': 1, 'rowspan': 1, 'header': row_index == 0},
        *[{'text': value, 'colspan': 1, 'rowspan': 1, 'header': row_index == 0}
          for value in values]] for row_index, (label, values) in enumerate(raw_rows)]}


def pdf_matrix_products(products):
    """Split explicit PDF model columns into first-class product records."""
    additions = []
    by_resource = {}
    for product in products:
        for resource in product.get('official_resources', []):
            by_resource.setdefault(resource.get('url'), []).append(product)
    for evidence in products:
        parents = by_resource.get(evidence.get('source_url'), [])
        parent = next((item for item in parents if re.search(r'\b(?:Series|Platform|Family)\b', item['name'], re.I)),
                      parents[0] if parents else evidence)
        for table in evidence.get('tables', []):
            rows = table.get('rows', [])
            if not rows or not rows[0] or rows[0][0].get('text') != 'Switch Model':
                continue
            headers = [cell.get('text', '').strip() for cell in rows[0][1:]]
            if len(headers) < 2 or not all(re.fullmatch(r'[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+', name, re.I)
                                           for name in headers):
                continue
            refs = table.get('source_refs', [])
            for column, model in enumerate(headers, 1):
                existing = next((item for item in products
                    if item.get('kind') == 'named_product'
                    and re.search(rf'\b{re.escape(model)}$', item.get('name', ''), re.I)), None)
                name = (existing['name'] if existing else
                        (model if model.lower().startswith('nvidia ') else 'NVIDIA ' + model))
                model_rows = []
                for row in rows[1:]:
                    if column >= len(row):
                        continue
                    label, value = row[0], row[column]
                    if label.get('text', '').strip() and value.get('text', '').strip():
                        model_rows.append([label, value])
                if not model_rows:
                    continue
                product_id = existing['id'] if existing else product_identifier(name)
                additions.append({'id': product_id, 'name': name,
                    'category': parent['category'], 'categories': parent.get('categories', []),
                    'kind': 'named_product', 'availability': 'not_verified',
                    'identity_status': 'official_pdf_model_matrix',
                    'parent_id': parent['id'], 'product_url': parent.get('source_url'),
                    'source_url': evidence['source_url'], 'source_sha256': evidence['source_sha256'],
                    'observed_at': evidence['observed_at'],
                    'tables': [{**table, 'section': model + ' — official PDF specifications',
                        'rows': model_rows, 'source_refs': refs}],
                    'attachments': evidence.get('attachments', []),
                    'official_resources': [],
                    'official_pages': evidence.get('official_pages', []),
                    'website_sitemap': evidence.get('website_sitemap', {'matched': False, 'roles': [],
                        'lastmod_claims': [], 'candidate_status': 'official_resource_not_product_path'}),
                    'extraction_status': 'native_tables_extracted'})
    return additions


def pdf_spec_tables_from_text(text):
    """Parse table-like native PDF text; kept separate for deterministic tests."""
    headings = re.compile(r'^\s*(?:(?:(?:technical|product|hardware)\s+)?specifications?\s*:?|规格)\s*$', re.I)
    inline_heading = re.compile(
        r'(?P<gap>\s{2,})(?P<title>(?:(?:technical|product|hardware)\s+)?specifications?\s*:?|规格)\s*$',
        re.I)
    stop = re.compile(r'^(?:ready to get started|for more information|to learn more|copyright|©|nvidia corporation|准备好开始了吗|如需详细了解)', re.I)
    output = []
    lines = text.splitlines()
    for start, heading in enumerate(lines):
        heading_match = headings.fullmatch(heading)
        inline_match = inline_heading.search(heading) if not heading_match else None
        matrix_heading = re.fullmatch(r'\s*Technical Specifications?\*?\s*', heading, re.I)
        if matrix_heading:
            matrix = _pdf_model_matrix(lines, start)
            if matrix:
                output.append(matrix)
                continue
        if not heading_match and not inline_match:
            continue
        # ``pdftotext -layout`` preserves page columns.  Some NVIDIA briefs put
        # prose in the left column and a specification panel in the right.  A
        # plain ``strip`` merges those independent columns into a bogus
        # key/value row.  When the heading itself starts well inside the page,
        # use that x-position as the table boundary and discard the left pane.
        section = (inline_match.group('title') if inline_match else heading).strip().rstrip(':')
        heading_indent = (inline_match.start('title') if inline_match
                          else len(heading) - len(heading.lstrip()))
        column_boundary = heading_indent if heading_indent >= 20 else 0
        if inline_match:
            rows = _pdf_right_panel_rows(lines, start, column_boundary)
            if len(rows) >= 3:
                output.append({'section': section, 'rows': [
                    [{'text': label, 'colspan': 1, 'rowspan': 1, 'header': False},
                     {'text': cell, 'colspan': 1, 'rowspan': 1, 'header': False}]
                    for label, cell in rows]})
            continue
        rows, unmatched = [], 0
        for raw in lines[start + 1:]:
            if (column_boundary and len(raw) > column_boundary
                    and raw[max(0, column_boundary - 3):column_boundary].strip()):
                # Long prose from the left pane can physically cross the
                # boundary on later pages.  It has no whitespace gutter and
                # must not be treated as a continuation of the last spec row.
                continue
            region = raw[column_boundary:] if column_boundary else raw
            value = region.strip()
            if not value:
                continue
            leading = len(region) - len(region.lstrip())
            columns = re.split(r'\s{2,}', value)
            if len(columns) >= 2:
                label = columns[0].strip()
                cell = ' '.join(part.strip() for part in columns[1:] if part.strip())
                if label and cell:
                    # A lower-case label fragment is the second visual line of
                    # the preceding label (for example ``Management`` /
                    # ``ports``), not a new specification.
                    if column_boundary and rows and label[:1].islower():
                        rows[-1] = (rows[-1][0] + ' ' + label,
                                    rows[-1][1] + ' ' + cell)
                        unmatched = 0
                        continue
                    rows.append((label, cell))
                    unmatched = 0
                    continue
            if stop.search(value):
                break
            if rows and len(value) < 160 and unmatched == 0:
                if column_boundary and leading >= 8:
                    rows[-1] = (rows[-1][0], rows[-1][1] + ' ' + value)
                    unmatched = 0
                else:
                    rows[-1] = (rows[-1][0] + ' ' + value, rows[-1][1])
                    unmatched = 1
            elif rows:
                break
        if len(rows) >= 3:
            output.append({'section': section, 'rows': [
                [{'text': label, 'colspan': 1, 'rowspan': 1, 'header': False},
                 {'text': cell, 'colspan': 1, 'rowspan': 1, 'header': False}]
                for label, cell in rows]})
    if not output:
        for start, heading in enumerate(lines):
            inline = re.search(r'(?P<gap>\s{2,})(?P<title>Portfolio|Specifications)\s*$', heading, re.I)
            if inline:
                table = _pdf_bullet_panel(lines, start, inline.start('title'), inline.group('title'))
                if table:
                    output.append(table)
                    break
        if not output:
            for start, heading in enumerate(lines):
                if re.fullmatch(r'\s*Key Features\s*', heading, re.I):
                    table = _pdf_key_features(lines, start)
                    if table:
                        output.append(table)
                        break
    return output


def pdf_attachment_specs(company_ledger, archive_root, products):
    """Import verified spec tables from product-linked, downloaded PDF attachments."""
    ledger_path = Path(company_ledger)
    if not ledger_path.is_file():
        return []
    archive_root = Path(archive_root)
    db = sqlite3.connect(f'file:{ledger_path}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        fetched = db.execute('''SELECT r.url,r.latest_sha,r.last_checked,b.path,b.kind
            FROM requests r JOIN blobs b ON b.sha=r.latest_sha
            WHERE r.state='done' AND b.kind='pdf' ORDER BY r.url''').fetchall()
    finally:
        db.close()
    files = {row['url']: row for row in fetched}
    generic = re.compile(r'line card|brochure|installation|user guide|release notes|announcement', re.I)
    spec_label = re.compile(r'datasheet|data sheet|specification|technical brief|product brief|数据表|规格表|技术简介', re.I)
    cache, receipts, handled = {}, {}, set()
    for product in products:
        for attachment in product.get('attachments', []):
            url = attachment.get('url', '')
            label = attachment.get('label', '')
            if url in handled or generic.search(label):
                continue
            if not spec_label.search(label) and product_identity_name(label) != product_identity_name(product['name']):
                continue
            row = files.get(url)
            if row is None or not row['path']:
                continue
            path = archive_root / row['path']
            if not path.is_file():
                continue
            sha, content = row['latest_sha'], path.read_bytes()
            if not content.startswith(b'%PDF-') or hashlib.sha256(content).hexdigest() != sha:
                continue
            tables = cache.setdefault(sha, pdf_spec_tables(path))
            if not tables:
                continue
            handled.add(url)
            exposure = attachment.get('source_url') or product.get('source_url')
            exposure_sha = attachment.get('source_sha256') or product.get('source_sha256')
            receipt = {'source_url': url, 'requested_url': url, 'sha256': sha,
                'snapshot_path': row['path'], 'observed_at': row['last_checked'] or utc_now(),
                'kind': 'official_pdf_attachment', 'http': {'content_type': 'application/pdf'}}
            receipts[(sha, url)] = receipt
            attachment.update(sha256=sha, snapshot_path=row['path'],
                source_url=exposure, source_sha256=exposure_sha)
            reference = {'url': url, 'sha256': sha, 'label': label, 'kind': 'official_pdf'}
            for extracted in tables:
                note = 'Extracted from native PDF text; OCR not used.'
                identity = hashlib.sha256(json.dumps({'rows': extracted['rows'], 'notes': note},
                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                existing = next((table for table in product.get('tables', []) if hashlib.sha256(json.dumps(
                    {'rows': table.get('rows'), 'notes': table.get('notes', '')}, sort_keys=True,
                    ensure_ascii=False).encode()).hexdigest() == identity), None)
                if existing:
                    existing['source_refs'] = list({(ref['url'], ref['sha256']): ref for ref in
                        existing.get('source_refs', []) + [reference]}.values())
                    continue
                table_index = max((table.get('index', 0) for table in product.get('tables', [])), default=0) + 1
                product.setdefault('tables', []).append({'index': table_index,
                    'section': extracted['section'], 'notes': note, 'rows': extracted['rows'],
                    'source_refs': [reference], 'extraction_method': 'pdftotext_layout'})
                product['extraction_status'] = 'native_tables_extracted'
    return list(receipts.values())


def networking_doc_products(company_ledger, archive_root):
    """Extract explicitly model-labelled native specs from NVIDIA hardware manuals."""
    ledger_path = Path(company_ledger)
    if not ledger_path.is_file():
        return [], []
    archive_root = Path(archive_root)
    connection = sqlite3.connect(f'file:{ledger_path}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    products, source_pages = {}, {}
    try:
        rows = connection.execute('''SELECT r.url,p.sha,p.path,p.title,p.breadcrumbs,p.observed_at
            FROM pages p JOIN requests r ON r.id=p.request
            WHERE r.url LIKE 'https://networking-docs.nvidia.com/%'
            ORDER BY r.url''').fetchall()
    finally:
        connection.close()
    for row in rows:
        title = row['title'] or ''
        family_match = re.search(r'\bNVIDIA\s+((?:ConnectX|BlueField|Spectrum|Quantum)[-\w]*)', title, re.I)
        if not family_match:
            continue
        url, sha = row['url'], row['sha']
        snapshot = archive_root / row['path']
        if not snapshot.is_file() or hashlib.sha256(snapshot.read_bytes()).hexdigest() != sha:
            continue
        parsed = parse_page(snapshot.read_bytes(), url)
        model_tables = []
        for table in parsed['tables']:
            match = re.match(r'^([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\s+(?:specifications?|specs)\b',
                             table.get('section', '').strip(), re.I)
            if match and table.get('is_specification') and table.get('rows'):
                model_tables.append((match.group(1), table))
        if not model_tables:
            continue
        family = 'NVIDIA ' + family_match.group(1)
        try:
            breadcrumbs = json.loads(row['breadcrumbs'] or '[]')
        except json.JSONDecodeError:
            breadcrumbs = []
        categories = []
        for crumb in breadcrumbs:
            value = ' '.join(str(crumb).split())
            if value in {'Networking', 'Adapters', 'DPUs', 'Ethernet Switches', 'InfiniBand'} and value not in categories:
                categories.append(value)
        if not categories:
            categories = ['Networking', 'Hardware']
        page = {'source_url': url, 'requested_url': url, 'sha256': sha,
            'snapshot_path': row['path'], 'observed_at': row['observed_at'],
            'heading': parsed['heading'], 'title': title, 'canonical': parsed.get('canonical'),
            'category': ' / '.join(categories), 'parent_url': '', 'depth': 0,
            'kind': 'official_hardware_manual',
            'http': {'status': 200, 'final_url': url, 'content_type': 'text/html'}}
        source_pages[(sha, url)] = page
        parent_id = product_identifier(family)
        page_ref = {'url': url, 'sha256': sha}
        parent = products.setdefault(parent_id, {'id': parent_id, 'name': family,
            'category': ' / '.join(categories), 'categories': categories,
            'kind': 'family_or_directory', 'availability': 'not_verified',
            'identity_status': 'official_hardware_manual_observed',
            'source_url': url, 'source_sha256': sha, 'observed_at': row['observed_at'],
            'parent_id': None, 'tables': [], 'attachments': [], 'official_resources': [],
            'official_pages': [], 'website_sitemap': {'matched': False, 'roles': [],
                'lastmod_claims': [], 'candidate_status': 'official_manual_not_product_path'},
            'extraction_status': 'specification_search_pending'})
        parent['official_pages'] = list({r['url']: r for r in parent['official_pages'] + [page_ref]}.values())
        for model, table in model_tables:
            table = {**table, 'source_refs': [page_ref]}
            name = family + ' ' + model
            product_id = product_identifier(name)
            product = products.setdefault(product_id, {'id': product_id, 'name': name,
                'category': ' / '.join(categories), 'categories': categories,
                'kind': 'named_product', 'availability': 'not_verified',
                'identity_status': 'official_part_number_specification_observed',
                'source_url': url, 'source_sha256': sha, 'observed_at': row['observed_at'],
                'parent_id': parent_id, 'tables': [], 'attachments': [], 'official_resources': [],
                'official_pages': [], 'website_sitemap': {'matched': False, 'roles': [],
                    'lastmod_claims': [], 'candidate_status': 'official_manual_not_product_path'},
                'extraction_status': 'native_tables_extracted'})
            product['official_pages'] = list({r['url']: r for r in product['official_pages'] + [page_ref]}.values())
            identity = hashlib.sha256(json.dumps({'rows': table['rows'], 'notes': table.get('notes', '')},
                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            existing_table = next((t for t in product['tables'] if hashlib.sha256(json.dumps(
                {'rows': t['rows'], 'notes': t.get('notes', '')}, sort_keys=True,
                ensure_ascii=False).encode()).hexdigest() == identity), None)
            if existing_table:
                existing_table['source_refs'] = list({(r['url'], r['sha256']): r for r in
                    existing_table.get('source_refs', []) + table['source_refs']}.values())
            else:
                product['tables'].append(table)
    return list(products.values()), list(source_pages.values())


def sitemap_entry_for_page(page, sitemap_rows):
    """Resolve official sitemap evidence through the requested or final URL."""
    return sitemap_rows.get(page.get('requested_url')) or sitemap_rows.get(page.get('source_url'))


def product_identifier(name):
    """Stable product identity independent of the page that currently links it."""
    key = product_identity_name(name)
    return 'nvidia-' + hashlib.sha256(('nvidia-product:' + key).encode()).hexdigest()[:20]


def comparison_model_identity(name, source_url):
    """Keep same-labelled models separate when NVIDIA declares a distinct portfolio."""
    display_name = name if name.lower().startswith('nvidia ') else 'NVIDIA ' + name
    path = urlsplit(source_url).path.casefold()
    if '/products/workstations/rtx-embedded/' in path and not re.search(r'\bembedded\b', display_name, re.I):
        return display_name + ' (Embedded GPU)', 'embedded_gpu'
    return display_name, None


def model_source_rank(url):
    """Prefer a model/family specification page over a generic comparison page."""
    path = urlsplit(url).path.casefold()
    if '/compare/' in path:
        return 0
    if re.search(r'/rtx-\d{3,4}/?$', path) or re.search(r'/rtx-pro-\d{4}/?$', path):
        return 4
    if re.search(r'/rtx-\d{4}-family/?$', path):
        return 3
    if '/50-series/' in path:
        return 2
    return 1


def source_preference(product):
    """Prefer a readable official model/spec page over a campaign or viewer shell."""
    host = urlsplit(product.get('source_url', '')).hostname
    return (100 if product.get('tables') else 0) + model_source_rank(product.get('source_url', '')) + (0 if host == 'resources.nvidia.com' else 1)


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
    # Older runs put every exception in one ``failed`` bucket. Preserve the
    # original error while reclassifying known terminal outcomes so coverage
    # does not claim that removed vendor URLs are active crawler failures.
    db.execute("UPDATE frontier SET state='unavailable' WHERE state='failed' AND error LIKE 'HTTP Error 404:%'")
    db.execute("UPDATE frontier SET state='policy_blocked' WHERE state='failed' AND error LIKE '%redirect or URL outside HTTPS host allowlist%'")
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
            state = frontier_failure_state(exc)
            db.execute("UPDATE frontier SET state=?,error=? WHERE url=?", (state, str(exc)[:300], row['url']))
            print(json.dumps({'state': state, 'error': str(exc)[:200], 'url': row['url']}), flush=True)
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
    prior_id_by_url, known_id_by_path = previous_page_identities(base)
    for page in pages:
        display_name = resource_product_heading(
            page.get('heading') or page.get('title') or '', page.get('source_url', ''))
        if page.get('depth', 1) == 0 or not display_name or not page_allowed(page['source_url']):
            continue
        url = page['source_url']
        kind = entity_kind(display_name, url)
        if urlsplit(url).hostname == 'resources.nvidia.com' and not re.search(
                r'^\s*(?:NVIDIA\s+)?(?:BlueField|ConnectX|Spectrum|Quantum|SN\d{4}|RTX|GeForce|Jetson|DGX|H100|H200|Grace|Blackwell)\b', display_name, re.I):
            continue
        # Named models use a canonical name key so the same model found on
        # an overview, comparison, localized route, and datasheet viewer is
        # one entity with multiple sources—not multiple products.
        key = (product_identifier(display_name) if kind == 'named_product' else
               prior_id_by_url.get(url, known_id_by_path.get(identity_path(url), website_page_identity(url))))
        categories = sorted({r[0] for r in db.execute('SELECT category FROM memberships WHERE url=?', (page['requested_url'],))} | {page['category']})
        candidate = {'id': key, 'name': display_name, 'category': ' / '.join(c for c in categories if c), 'categories': categories,
            'kind': kind, 'availability': 'not_verified', 'identity_status': 'official_page_observed',
            'source_url': url, 'source_sha256': page['sha256'], 'observed_at': page['observed_at'],
            'tables': [t for t in page['tables'] if t['is_specification']],
            'attachments': [{**l, 'source_url': url, 'source_sha256': page['sha256']}
                            for l in page['links']
                            if re.search(r'\.(pdf|docx?|pptx?|xlsx?)(?:$|\?)', l['url'], re.I)
                            and urlsplit(l['url']).hostname in {'www.nvidia.com', 'nvidia.com', 'www.nvidia.cn', 'nvidia.cn',
                                'images.nvidia.com', 'images.nvidia.cn', 'resources.nvidia.com', 'dam-cdn.nvd.orangelogic.com',
                                'networking-docs.nvidia.com', 'docs.nvidia.com'}],
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
            # Prefer an explicit model/spec page over an overview or resource
            # viewer shell while retaining every official source snapshot.
            if source_preference(candidate) > source_preference(old):
                for field in ('name', 'source_url', 'source_sha256', 'observed_at', 'kind', 'extraction_status'):
                    old[field] = candidate[field]
            if not old['tables'] and candidate['tables']:
                old['tables'] = candidate['tables']
            old['extraction_status'] = 'native_tables_extracted' if old['tables'] else 'specification_search_pending'
    # Some official portfolio pages expose several shipping models as columns
    # in one native specification table (for example RTX 5070 Ti / RTX 5070).
    # Split those columns into model records while preserving each source row,
    # the source page, and the parent family relationship.
    for page in pages:
        if not page.get('heading') or not page_allowed(page.get('source_url', '')):
            continue
        parent_id = known_id_by_path.get(identity_path(page['source_url']), website_page_identity(page['source_url']))
        parent = entities.get(parent_id)
        if not parent:
            continue
        for table in page.get('tables', []):
            if not table.get('is_specification') or not table.get('rows'):
                continue
            header = table['rows'][0]
            model_columns = [(index, ' '.join(cell.get('text', '').split()))
                for index, cell in enumerate(header)
                if entity_kind(' '.join(cell.get('text', '').split()), '') == 'named_product'
                and MODEL.search(cell.get('text', ''))]
            if not model_columns:
                continue
            # The first explicit model header determines how many leading
            # columns are labels. NVIDIA uses both one-label and
            # section+parameter two-label layouts.
            first_model_column = model_columns[0][0]
            for column, model in model_columns:
                name, variant_scope = comparison_model_identity(model, page['source_url'])
                normalized_name = re.sub(r'^NVIDIA\s+', '', name).casefold()
                existing = next((p for p in entities.values()
                    if p['kind'] == 'named_product'
                    and re.sub(r'^NVIDIA\s+', '', p['name']).casefold() == normalized_name), None)
                child_id = existing['id'] if existing else product_identifier(name)
                rows = []
                for source_row in table['rows'][1:]:
                    if column >= len(source_row):
                        continue
                    value = source_row[column]
                    label = next((cell for cell in reversed(source_row[:first_model_column]) if cell.get('text', '').strip()), None)
                    if not label or not value.get('text', '').strip():
                        continue
                    rows.append([label, value])
                if not rows:
                    continue
                child = {'id': child_id, 'name': name, 'category': parent['category'],
                    'categories': parent['categories'], 'kind': 'named_product',
                    'availability': 'not_verified', 'identity_status': 'official_comparison_column',
                    'parent_id': parent_id, 'product_url': page['source_url'],
                    'source_url': page['source_url'], 'source_sha256': page['sha256'],
                    'observed_at': page['observed_at'], 'tables': [{**table, 'rows': rows,
                        'section': model + ' — official comparison-table specifications'}],
                    'attachments': [], 'official_pages': [{'url': page['source_url'], 'sha256': page['sha256']}],
                    'website_sitemap': {'matched': page['source_url'] in sitemap_rows,
                        'roles': json.loads(sitemap_rows[page['source_url']]['role_data']) if page['source_url'] in sitemap_rows else [],
                        'lastmod_claims': sitemap_rows[page['source_url']]['lastmod'].split('|') if page['source_url'] in sitemap_rows and sitemap_rows[page['source_url']]['lastmod'] else [],
                        'candidate_status': sitemap_rows[page['source_url']]['state'] if page['source_url'] in sitemap_rows else 'not_a_product_path'},
                    'extraction_status': 'native_tables_extracted'}
                if variant_scope:
                    child.update(official_name=model, variant_scope=variant_scope,
                                 identity_status='official_comparison_column_scoped_by_parent')
                if existing:
                    child['official_pages'] = list({r['url']: r for r in existing.get('official_pages', []) + child['official_pages']}.values())
                    child['attachments'] = list({r['url']: r for r in existing.get('attachments', []) + child['attachments']}.values())
                    child['tables'] = list({hashlib.sha256(json.dumps({'rows': t['rows'], 'notes': t.get('notes', '')}, sort_keys=True, ensure_ascii=False).encode()).hexdigest(): t
                        for t in existing['tables'] + child['tables']}.values())
                    if model_source_rank(existing['source_url']) > model_source_rank(child['source_url']):
                        for field in ('source_url', 'source_sha256', 'observed_at', 'product_url', 'parent_id', 'website_sitemap'):
                            if field in existing:
                                child[field] = existing[field]
                entities[child_id] = child
    # The current GeForce 50-series component schema exposes several child
    # models only as a JavaScript comparison grid. Prefer already-collected
    # official per-model HTML tables when present, and correct earlier
    # component-only records whose citation pointed at the JS asset.
    for component_id, row in list(db.execute('SELECT id,payload FROM component_products')) if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='component_products'").fetchone() else []:
        product = json.loads(row)
        matching = next((e for e in entities.values()
            if e['kind'] == 'named_product'
            and re.sub(r'^NVIDIA\s+', '', e['name']).casefold() == re.sub(r'^NVIDIA\s+', '', product['name']).casefold()
            and e['tables']), None)
        if matching:
            matching['identity_status'] = 'official_model_page_and_component_observed'
            matching['attachments'] = list({a['url']: a for a in matching.get('attachments', []) + product.get('attachments', [])}.values())
            model_page = next((p for p in pages if p.get('source_url') == product.get('product_url')), None)
            if model_page:
                matching['official_pages'] = list({p['url']: p for p in matching.get('official_pages', []) + [
                    {'url': model_page['source_url'], 'sha256': model_page['sha256']} ]}.values())
            matching['tables'].extend(t for t in product['tables'] if t.get('method') == 'official_component_literal_no_execution')
            continue
        component_source = next((s for s in db.execute('SELECT payload FROM component_sources')
                                 if json.loads(s[0])['sha256'] == product['source_sha256']), None)
        if component_source:
            receipt = json.loads(component_source[0])
            product['source_url'] = product['product_url']
            product['source_sha256'] = hashlib.sha256((product['source_sha256'] + product['product_url']).encode()).hexdigest()
            # A component and its parent page are separate evidence. Keep both
            # as source records so the product's primary citation is a page.
            page = next((p for p in pages if p['source_url'] == product['product_url']), None)
            if page:
                product['source_sha256'] = page['sha256']
                product['observed_at'] = page['observed_at']
                product['official_pages'] = [{'url': product['product_url'], 'sha256': page['sha256']}]
                product['website_sitemap'] = sitemap_entry_for_page(page, sitemap_rows)
                if product.get('website_sitemap'):
                    entry = product['website_sitemap']
                    product['website_sitemap'] = {'matched': True,
                        'roles': json.loads(entry['role_data']),
                        'lastmod_claims': entry['lastmod'].split('|') if entry['lastmod'] else [],
                        'candidate_status': entry['state']}
                product['tables'].append({'index': len(product['tables']) + 1,
                    'section': product['name'] + ' — official dynamic component fields',
                    'rows': product['tables'][0]['rows'], 'method': 'official_component_literal_no_execution',
                    'is_specification': True, 'notes': 'Component values retained as supporting evidence; primary product citation is the official model page. Not a complete specification.'})
                product['extraction_status'] = 'native_tables_extracted'
                entities[component_id] = product
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
                files = {r['url']: r for r in old_child.get('attachments', [])}
                files.update({r['url']: r for r in candidate['attachments']})
                candidate['attachments'] = list(files.values())
                tables = {hashlib.sha256(json.dumps({'rows': t.get('rows'), 'notes': t.get('notes', '')},
                    sort_keys=True, ensure_ascii=False).encode()).hexdigest(): t
                    for t in old_child.get('tables', []) + candidate['tables']}
                candidate['tables'] = list(tables.values())
                if source_preference(old_child) > source_preference(candidate) or (
                        source_preference(old_child) == source_preference(candidate)
                        and old_child.get('_parent_depth', 99) < candidate['_parent_depth']):
                    candidate.update(parent_id=old_child.get('parent_id'), product_url=old_child.get('product_url', old_child['source_url']),
                                     source_url=old_child['source_url'], source_sha256=old_child['source_sha256'],
                                     observed_at=old_child['observed_at'], category=old_child['category'],
                                     categories=old_child['categories'], _parent_depth=old_child['_parent_depth'])
                candidate['extraction_status'] = 'native_tables_extracted' if candidate['tables'] else old_child.get('extraction_status', candidate['extraction_status'])
            expanded[product_id] = candidate
    expanded = consolidate_resource_viewers(expanded)
    archive_root = base.parent.parent
    doc_products, doc_pages = networking_doc_products(
        archive_root / 'ledger/companies/nvidia/crawl.sqlite', archive_root)
    pages.extend(p for p in doc_pages if not any(x.get('source_url') == p['source_url'] and x.get('sha256') == p['sha256'] for x in pages))
    for product in doc_products:
        old = expanded.get(product['id'])
        if old is None:
            expanded[product['id']] = product
            continue
        old['official_pages'] = list({p['url']: p for p in old.get('official_pages', []) + product['official_pages']}.values())
        old['categories'] = sorted(set(old.get('categories', []) + product['categories']))
        old['category'] = ' / '.join(old['categories'])
        table_map = {}
        for table in old.get('tables', []) + product.get('tables', []):
            key = hashlib.sha256(json.dumps({'rows': table.get('rows'), 'notes': table.get('notes', '')},
                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if key not in table_map:
                table_map[key] = table
            else:
                table_map[key]['source_refs'] = list({(r['url'], r['sha256']): r for r in
                    table_map[key].get('source_refs', []) + table.get('source_refs', [])}.values())
        old['tables'] = list(table_map.values())
        old['attachments'] = list({a['url']: a for a in old.get('attachments', []) + product.get('attachments', [])}.values())
        if not old.get('tables') and product.get('tables'):
            old.update(source_url=product['source_url'], source_sha256=product['source_sha256'],
                observed_at=product['observed_at'])
        if old['tables']:
            old['extraction_status'] = 'native_tables_extracted'
    for product in expanded.values():
        product.pop('_parent_depth', None)
    pdf_sources = pdf_attachment_specs(
        archive_root / 'ledger/companies/nvidia/crawl.sqlite', archive_root,
        list(expanded.values()))
    for product in pdf_matrix_products(list(expanded.values())):
        old = expanded.get(product['id'])
        if old:
            product['official_pages'] = list({p['url']: p for p in
                old.get('official_pages', []) + product['official_pages']}.values())
            product['attachments'] = list({p['url']: p for p in
                old.get('attachments', []) + product['attachments']}.values())
            product['tables'] = list({hashlib.sha256(json.dumps(
                {'rows': table.get('rows'), 'notes': table.get('notes', '')},
                sort_keys=True, ensure_ascii=False).encode()).hexdigest(): table
                for table in old.get('tables', []) + product['tables']}.values())
        expanded[product['id']] = product
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
            'unavailable_pages': sum(r['state'] == 'unavailable' for r in frontier),
            'policy_blocked_pages': sum(r['state'] == 'policy_blocked' for r in frontier),
            'entity_counts': dict(counts), 'with_spec_tables': sum(bool(e['tables']) for e in expanded.values()),
            'website_sitemap': sitemap_info,
            'limitations': ['官网目录入口不等于全部具体 SKU；产品身份、配置拆分和在售状态仍需核对。',
                '仅从官方目录和产品相关链接扩展；未解析的动态表格、PDF 及独立文档站规格保留待提取。',
                '厂商已删除的 404 页面单列为 unavailable；策略阻止的站外跳转单列为 policy_blocked，不计入可重试访问失败。',
                '参数保留官方表格、列名、合并单元格和脚注；尚未自动映射跨产品通用字段。']},
        'product_map': {'policy': 'Products absent from a partial observation are retained; removal requires an explicit, completed official-directory comparison and review.',
            'changes': dict(changes), 'entries': len(expanded)},
        'products': list(expanded.values()),
        'sources': list({(source['sha256'], source['source_url']): source for source in
            [source_receipt(page) for page in pages] + pdf_sources}.values()),
        'frontier': frontier}
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
