"""Siemens Energy: global English product pages with native HTML performance and comparison tables.

Scope is ``/global/en/home/products-services/product/<slug>.html``. Gas turbine pages carry
"Performance data …" and "Physical dimensions and weight" tables (one column per rating);
the power transformer page carries a type comparison table. These tables do not use the word
"specification", so the adapter names the sections it accepts instead of guessing from values.
"""
import hashlib
import re
from urllib.parse import urlsplit

from ..extraction import html_page
from .base import ProductAdapter

PRODUCT = re.compile(r'^/global/en/home/products-services/product/([A-Za-z0-9][A-Za-z0-9-]*)\.html$')
SPEC_SECTION = re.compile(r'performance data|physical dimensions|technical data|specification|comparison of', re.I)
SPEC_HEADER = re.compile(r'^\s*(?:specifications?|technical data)\s*$', re.I)


class SiemensEnergyProductAdapter(ProductAdapter):
    company_id = 'siemens-energy'

    def page_allowed(self, url):
        p = urlsplit(url)
        return p.hostname == 'www.siemens-energy.com' and not p.query and bool(PRODUCT.match(p.path))

    def parse(self, body, url):
        page = html_page(body, url)
        for table in page['tables']:
            first = table['rows'][0][0]['text'] if table['rows'] and table['rows'][0] else ''
            table['is_specification'] = bool(SPEC_SECTION.search(table['section']) or SPEC_HEADER.match(first))
        return page

    def identity(self, page, url):
        match = PRODUCT.match(urlsplit(url).path)
        heading = ' '.join((page.get('heading') or page.get('title') or '').split())
        if not match or not heading:
            return None
        # "SGT-800 gas turbine: substantial steam-raising capability" -> the part before the claim.
        name = heading.split(':', 1)[0].strip() or heading
        product = any(t['is_specification'] for t in page.get('tables', []))
        return {'id': 'siemens-energy-' + hashlib.sha256(match.group(1).casefold().encode()).hexdigest()[:20],
                'name': name, 'kind': 'named_product' if product else 'family_or_directory', 'parent_id': None}
