"""Astera Labs: per-part product pages with an attribute list.

``/product-details/<part>/`` pages (Aries retimers, Leo CXL controllers …) end with a list of
``<li><span class="fw-medium">Label:</span> value</li>`` attributes (package, max PCIe
generation, lanes, revision …) and link the family's product brief PDF. The attributes become
one two-column table, values as printed; the brief is offered as an attachment and is deferred
whenever the page's own attributes are present. Families without part pages (Scorpio switches)
list their parts in an "Ordering Information" table on ``/products/<family>/``; only tables whose
header names "Part Number" count there.
"""
import hashlib
import html
import re
from urllib.parse import urlsplit

from ..extraction import cell, html_page
from .base import ProductAdapter

PRODUCT = re.compile(r'^/product-details/([A-Za-z0-9][A-Za-z0-9-]*)/$')
FAMILY = re.compile(r'^/products/([a-z0-9][a-z0-9-]*)/$')
BRIEF = re.compile(r'^/wp-content/uploads/\d{4}/\d{2}/[A-Za-z0-9._-]+\.pdf$')
ATTRIBUTE = re.compile(r'<li\b[^>]*>\s*<span class="[^"]*\bfw-medium\b[^"]*">(.*?)</span>(.*?)</li>', re.S | re.I)
TAG = re.compile(r'<[^>]+>')
METHOD = 'asteralabs_product_attributes'


def _text(fragment):
    return ' '.join(html.unescape(TAG.sub(' ', fragment)).replace('\xa0', ' ').split())


def attribute_rows(body_text):
    rows = []
    for label, value in ATTRIBUTE.findall(body_text):
        label, value = _text(label).rstrip(':').strip(), _text(value)
        if label and value and label.casefold() != 'ordering':
            rows.append([cell(label), cell(value)])
    return rows


class AsteraLabsProductAdapter(ProductAdapter):
    company_id = 'asteralabs'

    def page_allowed(self, url):
        p = urlsplit(url)
        return p.hostname == 'www.asteralabs.com' and not p.query and bool(PRODUCT.match(p.path) or FAMILY.match(p.path))

    def parse(self, body, url):
        page = html_page(body, url)
        if FAMILY.match(urlsplit(url).path):
            for table in page['tables']:
                header = ' '.join(c['text'] for c in table['rows'][0]).casefold() if table['rows'] else ''
                table['is_specification'] = 'part number' in header
            page['tables'] = [t for t in page['tables'] if t['is_specification']]
            return page
        rows = attribute_rows(body.decode('utf-8', 'replace'))
        page['tables'] = [{'index': 1, 'section': 'Product attributes', 'rows': rows,
                           'notes': 'Astera Labs 零件页的属性列表原文（名称: 值）。', 'is_specification': True,
                           'method': METHOD}] if len(rows) >= 2 else []
        return page

    def identity(self, page, url):
        path = urlsplit(url).path
        part, family = PRODUCT.match(path), FAMILY.match(path)
        name = ' '.join((page.get('heading') or '').split())
        if not (part or family) or not name:
            return None
        key = (part or family).group(1).casefold() if part else 'family:' + family.group(1)
        return {'id': 'asteralabs-' + hashlib.sha256(key.encode()).hexdigest()[:20], 'name': name,
                'kind': 'named_product' if part and page.get('tables') else 'family_or_directory', 'parent_id': None}

    def candidates(self, page, url, is_directory=False):
        if FAMILY.match(urlsplit(url).path):
            return []  # a family page answers from its own ordering table; part pages are seeded explicitly
        rows = []
        for link in page.get('links', []):
            target = self.normalize(link['url'], url)
            if target and BRIEF.match(urlsplit(target).path) and 'brief' in (link.get('label', '') + target).casefold():
                rows.append({**link, 'url': target, 'role': 'attachment', 'rank': 1})
        return rows
