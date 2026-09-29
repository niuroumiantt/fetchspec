"""Vertiv: official en-us product catalog pages with a native HTML Models table."""
import hashlib
import html
import re
from urllib.parse import urlsplit

from ..extraction import html_page
from .base import ProductAdapter

CATALOG = re.compile(r'^/en-us/products-catalog/(?:[a-z0-9-]+/)+$')
MODEL_HEADER = re.compile(r'^\s*(?:models?|型号)\s*$', re.I)
SPEC_SECTION = re.compile(r'specification|technical data|models|规格|型号', re.I)
MARKS = re.compile(r'[™®©]|&#(?:174|8482|169);')


def clean_name(value):
    return re.sub(r'\s+', ' ', MARKS.sub('', html.unescape(value or ''))).strip()


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
        return page

    def identity(self, page, url):
        name = clean_name(page.get('heading') or page.get('title'))
        if not name:
            return None
        path = urlsplit(url).path.rstrip('/')
        product = any(t['is_specification'] for t in page.get('tables', []))
        key = re.sub(r'^/en-us', '', path)
        return {'id': 'vertiv-' + hashlib.sha256(key.encode()).hexdigest()[:20], 'name': name,
                'kind': 'named_product' if product else 'family_or_directory', 'parent_id': None}
