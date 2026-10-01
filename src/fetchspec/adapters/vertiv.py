"""Vertiv: official English product catalog pages.

Multi-model pages carry a native HTML Models table. Single-model pages carry no HTML table;
their specification chart exists only as the literal pdfmake definition behind the page's own
Print button (``var docDefinitionSpecs``). We read those string literals as data, never execute
the script, and mark the table with its own method so it is never mistaken for an HTML table.
"""
import hashlib
import html
import re
from urllib.parse import urlsplit

from ..extraction import cell, html_page
from .base import ProductAdapter

# en-us first; other English regional catalogs (en-emea, en-asia, en-in, …) only carry products
# that have no en-us page. Identity drops the locale, so the same product dedups across regions.
CATALOG = re.compile(r'^/en-[a-z]{2,6}/products-catalog/(?:[A-Za-z0-9][A-Za-z0-9._-]*/)+$')
LOCALE = re.compile(r'^/en-[a-z]{2,6}(?=/)')
MODEL_HEADER = re.compile(r'^\s*(?:models?|型号)\s*$', re.I)
SPEC_SECTION = re.compile(r'specification|technical data|models|规格|型号', re.I)
MARKS = re.compile(r'[™®©]|&#(?:174|8482|169);')
PRINT_SPECS = re.compile(r"var\s+docDefinitionSpecs\s*=(.*?)</script>", re.S)
JS_STRING = r"'((?:[^'\\\n]|\\.)*)'"
CELL = re.compile(r"\{\s*text:\s*" + JS_STRING + r"\s*(?:,\s*style:\s*'(\w+)')?\s*(?:,\s*colSpan:\s*(\d+))?\s*\}")
ROW = re.compile(r"\[((?:\s*\{(?:[^{}'\[\]]|" + JS_STRING + r")*\}\s*,?)+)\s*(?://[^\n]*\s*)*\]")
JS_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.S)
EMPTY_VALUE = {'', 'n/a', 'na', '-', '—'}


def clean_name(value):
    return re.sub(r'\s+', ' ', MARKS.sub('', html.unescape(value or ''))).strip()


def _js_text(literal):
    simple = {'n': '\n', 't': '\t', 'r': '', 'b': '', 'f': '', 'v': ''}
    def unescape(match):
        code = match.group(1)
        if code[0] in 'ux' and len(code) > 1:
            return chr(int(code[1:], 16))
        return simple.get(code, code)
    return ' '.join(html.unescape(JS_ESCAPE.sub(unescape, literal)).split())


def print_spec_table(body_text):
    """The Print-button specification chart as a two-column table, or None."""
    match = PRINT_SPECS.search(body_text)
    if not match:
        return None
    block = match.group(1)
    start = block.find("style: 'specsTable'")
    if start < 0:
        return None
    rows = []
    for row in ROW.finditer(block, start):
        cells = [(_js_text(text), style or '', int(span or 1)) for text, style, span in CELL.findall(row.group(1))]
        cells = [c for c in cells if c[0] or c[2] > 1 or c[1].startswith('specsTableCell')]
        if not cells:
            continue
        if cells[0][1] in {'specsTableHeader', 'specsTableSection'}:
            rows.append([cell(cells[0][0], header=True, colspan=max(cells[0][2], 2))])
        else:
            rows.append([cell(text) for text, _, _ in cells])
    values = [r[-1]['text'] for r in rows if len(r) > 1]
    if not values:
        return None
    return {'index': 1, 'section': 'Specifications', 'rows': rows,
            'notes': '取自官方产品页"Print"按钮背后的规格表定义（pdfmake 字面值），未执行脚本。',
            # A chart whose every value is N/A states no specification.
            'is_specification': any(v.casefold() not in EMPTY_VALUE for v in values),
            'method': 'vendor_print_definition_literal_no_execution'}


class VertivProductAdapter(ProductAdapter):
    company_id = 'vertiv'

    def page_allowed(self, url):
        path = urlsplit(url).path
        path = path if path.endswith('/') else path + '/'
        return bool(CATALOG.match(path)) and not self.format(url)

    def parse(self, body, url):
        page = html_page(body, url)
        for table in page['tables']:
            first = [cell['text'] for cell in table['rows'][0]] if table['rows'] else []
            header = any(MODEL_HEADER.match(text) for text in first)
            # Vendor exception: the model comparison table has no "specification" wording.
            table['is_specification'] = bool(header or (table['is_specification'] and SPEC_SECTION.search(table['section'])))
            if header:
                table['section'] = table['section'] or 'Models'
        if not any(t['is_specification'] for t in page['tables']):
            printed = print_spec_table(body.decode('utf-8', 'replace'))
            if printed:
                printed['index'] = len(page['tables']) + 1
                page['tables'].append(printed)
        return page

    def identity(self, page, url):
        name = clean_name(page.get('heading') or page.get('title'))
        if not name:
            return None
        path = urlsplit(url).path.rstrip('/')
        product = any(t['is_specification'] for t in page.get('tables', []))
        key = LOCALE.sub('', path).casefold()  # Vertiv mixes case in a few slugs (powerdirect-3000-33kW)
        return {'id': 'vertiv-' + hashlib.sha256(key.encode()).hexdigest()[:20], 'name': name,
                'kind': 'named_product' if product else 'family_or_directory', 'parent_id': None}
