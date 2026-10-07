import hashlib
import re
from urllib.parse import urlsplit

from ..company import SupermicroAdapter
from ..extraction import html_page
from ..ownership import support_context
from .base import ProductAdapter


class SupermicroProductAdapter(ProductAdapter):
    company_id = 'supermicro'
    MODEL = re.compile(r'\b(?:SYS|AS|ARS|SBI|SSG|SRS|CSE|MBI|MBD|X1[0-9]|H1[0-9])[-A-Z0-9]+\b', re.I)

    def page_allowed(self, url):
        path = re.sub(r'^/(?:en|zh-cn|zh-tw)(?=/)', '', urlsplit(url).path.lower())
        return path.startswith('/products/') and not self.format(url)

    def parse(self, body, url):
        page = html_page(body, url)
        parser, links = SupermicroAdapter(self.profile).discover(body.decode('utf-8', 'replace'), url)
        page['links'].extend({'url': r['url'], 'label': r.get('label', ''), 'section': ''}
                             for r in links if r.get('method') == 'GET')
        page['tables'] = [t for t in page['tables'] if 'spec-key-features' not in t.get('html_class', '') and not re.search(r'^Key (?:Applications|Features)$', t['section'], re.I)]
        page['sku_models'] = parser.sku_rels
        return page

    def identity(self, page, url):
        if support_context(self.company_id, url):
            return None
        heading = page.get('heading') or page.get('title')
        if not heading:
            return None
        model = self.MODEL.search(heading)
        if not model and len(set(page.get('sku_models', []))) == 1:
            model = self.MODEL.search(page['sku_models'][0])
        name = model.group().upper() if model else heading
        kind = 'named_product' if model else 'family_or_directory'
        key = name.casefold() if model else re.sub(r'^/(?:en|zh-cn|zh-tw)(?=/)', '', urlsplit(url).path).rstrip('/')
        return {'id': 'supermicro-' + hashlib.sha256(key.encode()).hexdigest()[:20],
                'name': name, 'kind': kind, 'parent_id': None}
