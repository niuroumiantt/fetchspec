from .. import product_catalog as legacy
from .base import ProductAdapter


class NvidiaProductAdapter(ProductAdapter):
    company_id = 'nvidia'

    def __init__(self, known_catalog=None):
        super().__init__(known_catalog)
        self.profile['allowed_hosts'] = sorted(set(self.profile['allowed_hosts']) | legacy.OFFICIAL_PAGE_HOSTS)
        self.profile['robots_hosts'] = self.profile['allowed_hosts']

    def page_allowed(self, url):
        return legacy.page_allowed(url)

    def parse(self, body, url):
        return legacy.parse_page(body, url)

    def identity(self, page, url):
        heading = page.get('heading') or page.get('title')
        if not heading:
            return None
        kind = legacy.entity_kind(heading, url)
        # Exact source/product references take precedence over name heuristics.
        matches = [p for p in self.known if url in {p.get('source_url'), p.get('product_url')}]
        same_name = [p for p in matches if legacy.product_identity_name(p.get('name', '')) == legacy.product_identity_name(heading)]
        match = same_name[0] if len(same_name) == 1 else None
        if match is None:
            same_kind = [p for p in matches if p.get('kind') == kind]
            if len(same_kind) == 1:
                match = same_kind[0]
            elif len(same_kind) > 1:
                raise ValueError('ambiguous existing NVIDIA entities for one source; requires product-section review')
        if match is None:
            localized = [p for p in self.known if p.get('kind') == kind and
                         legacy.identity_path(p.get('product_url', p.get('source_url', ''))) == legacy.identity_path(url)]
            if len(localized) == 1:
                match = localized[0]
            elif len(localized) > 1:
                raise ValueError('ambiguous existing localized NVIDIA entities; requires product-section review')
        if match is None and kind == 'named_product':
            named = [p for p in self.known if p.get('kind') == kind and
                     legacy.product_identity_name(p.get('name', '')) == legacy.product_identity_name(heading)]
            if len(named) == 1:
                match = named[0]
        if match:
            return {'id': match['id'], 'name': match['name'], 'kind': match['kind'], 'parent_id': match.get('parent_id')}
        return {'id': legacy.product_identifier(heading) if kind == 'named_product' else legacy.website_page_identity(url),
                'name': heading, 'kind': kind, 'parent_id': None}
