"""Delta Electronics: en-US product pages whose "Product Specifications" travel in the page payload.

deltaww.com is a Next.js site. The rendered HTML holds only the page outline; the content of
each section is in the same response's server-component payload (``self.__next_f.push``), where
the "Product Specifications" section either is HTML text (CDUs, battery systems: lists of
"Label: value" lines) or embeds a published Google document that Delta maintains as the
specification table (ORV3 power shelves and PSUs). The adapter reads the first from the bytes
it already has, as rows split only at the vendor's own colon, and follows the second as the
page's specification link. Nothing is executed and no other Google document is reachable.
"""
import hashlib
import html
import json
import re
from urllib.parse import urlsplit

from ..extraction import cell, html_page
from .base import ProductAdapter

PRODUCT = re.compile(r'^/en-US/products/[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$')
SPEC_DOC = re.compile(r'^/document/d/e/[A-Za-z0-9_-]{20,200}/pub$')
DOC_URL = re.compile(r'https://docs\.google\.com/document/d/e/[A-Za-z0-9_-]{20,200}/pub(?:\?embedded=true)?')
PUSH = re.compile(r'self\.__next_f\.push\((\[.*?\])\)</script>', re.S)
SPEC_REF = re.compile(r'"title":"Product Specifications","content":"(\$[0-9a-f]+|(?:[^"\\]|\\.)*)"')
BLOCK = re.compile(r'</?(?:p|li|ul|ol|div|br|h[1-6]|tr|table|tbody)\b[^>]*>', re.I)
TAG = re.compile(r'<[^>]+>')
METHOD = 'delta_spec_section_rows'


def _payload(body):
    text = []
    for chunk in PUSH.findall(body.decode('utf-8', 'replace')):
        try:
            value = json.loads(chunk)
        except ValueError:
            continue
        if len(value) > 1 and isinstance(value[1], str):
            text.append(value[1])
    return ''.join(text)


def spec_section(body):
    """The "Product Specifications" section HTML from the page payload, or ''."""
    text = _payload(body)
    match = SPEC_REF.search(text)
    if not match:
        return ''
    content = match.group(1)
    if not content.startswith('$'):
        return json.loads('"' + content + '"')
    # "$1c" points at a text chunk "1c:T<hex byte length>," elsewhere in the payload.
    ref = re.search(r'(?<![0-9A-Za-z])' + re.escape(content[1:]) + r':T([0-9a-f]+),', text)
    if not ref:
        return ''
    raw = text.encode('utf-8')
    start = len(text[:ref.end()].encode('utf-8'))
    return raw[start:start + int(ref.group(1), 16)].decode('utf-8', 'replace')


def section_rows(section):
    rows = []
    for block in BLOCK.split(section):
        text = ' '.join(html.unescape(TAG.sub(' ', block)).replace('\xa0', ' ').split()).strip(' |')
        if not text:
            continue
        label, sep, value = text.partition(':')
        if sep and label.strip() and value.strip() and len(label) <= 80:
            rows.append([cell(label.strip()), cell(value.strip())])
        else:
            rows.append([cell(text.rstrip(':').strip(), header=bool(sep) or not re.search(r'\d', text))])
    return rows


class DeltaProductAdapter(ProductAdapter):
    company_id = 'delta'

    def page_allowed(self, url):
        p = urlsplit(url)
        if p.query and not (p.hostname == 'docs.google.com' and p.query == 'embedded=true'):
            return False
        if p.hostname == 'docs.google.com':
            return bool(SPEC_DOC.match(p.path))  # only reachable as a Delta page's specification link
        return p.hostname == 'www.deltaww.com' and bool(PRODUCT.match(p.path))

    def parse(self, body, url):
        page = html_page(body, url)
        host = urlsplit(url).hostname
        if host == 'docs.google.com':
            for table in page['tables']:
                table['is_specification'] = True
                table['section'] = table['section'] or 'Product Specifications (Delta published document)'
            return page
        section = spec_section(body)
        page['spec_documents'] = sorted(set(DOC_URL.findall(section)))
        rows = section_rows(DOC_URL.sub('', section)) if section and not page['spec_documents'] else []
        if sum(1 for r in rows if len(r) == 2) >= 3:
            page['tables'] = [t for t in page['tables'] if t.get('is_specification')] + [{
                'index': len(page['tables']) + 1, 'section': 'Product Specifications', 'rows': rows,
                'notes': 'Delta 产品页"Product Specifications"段（页面负载内的 HTML 原文）；一行按厂商自己的冒号分成名称与取值，其余行原样保留。',
                'is_specification': True, 'method': METHOD}]
        return page

    def identity(self, page, url):
        if urlsplit(url).hostname != 'www.deltaww.com':
            return None
        title = ' '.join((page.get('heading') or page.get('title') or '').split())
        name = re.sub(r'^Products\s*-\s*|\s*-\s*Delta$', '', title).strip()
        if not name:
            return None
        key = urlsplit(url).path.casefold().rstrip('/')
        has_spec = any(t.get('is_specification') for t in page.get('tables', [])) or bool(page.get('spec_documents'))
        return {'id': 'delta-' + hashlib.sha256(key.encode()).hexdigest()[:20], 'name': name,
                'kind': 'named_product' if has_spec else 'family_or_directory', 'parent_id': None}

    def candidates(self, page, url, is_directory=False):
        return [{'url': doc.split('?', 1)[0] + '?embedded=true', 'label': 'Product Specifications', 'section': 'Product Specifications',
                 'role': 'specification', 'rank': 0} for doc in page.get('spec_documents', [])]
