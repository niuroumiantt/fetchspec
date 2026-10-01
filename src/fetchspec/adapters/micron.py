"""Micron: per-part pages, whose specification rows come from the JSON component the page names.

A part page (``/products/…/part-catalog/part-detail/<part>``) renders its specifications by
script from ``data-apiresource=".../_jcr_content.products.json/getproductinfo/…/<part>"``. We fetch
that official component as the page's specification source, keep the vendor's name/value rows as
they are (exact duplicate rows collapsed), and never execute page script.
"""
import hashlib
import json
import re
from urllib.parse import urlsplit

from ..extraction import cell, html_page
from .base import ProductAdapter

PART_PAGE = re.compile(r'^/products/(?!obsolete/)(?:[a-z0-9][a-z0-9-]*/)+part-catalog/part-detail/([a-z0-9][a-z0-9-]*)/?$')
COMPONENT = re.compile(r'^/content/micron/us/en/products/(?:[a-z0-9][a-z0-9-]*/)+part-catalog/part-detail/'
                       r'_jcr_content\.products\.json/getproductinfo/-/-/-/en_US/-/([a-z0-9][a-z0-9-]*)$')
API_RESOURCE = re.compile(r'data-apiresource="(/content/micron/[^"<>\s]+)"')
TITLE = re.compile(r'^\s*([A-Z0-9][A-Z0-9:.-]+)\b')


class MicronProductAdapter(ProductAdapter):
    company_id = 'micron'

    def page_allowed(self, url):
        p = urlsplit(url)
        return p.hostname == 'www.micron.com' and not p.query and bool(PART_PAGE.match(p.path))

    def component_allowed(self, url, content_type):
        p = urlsplit(url)
        return p.hostname == 'www.micron.com' and not p.query and bool(COMPONENT.match(p.path)) and 'json' in content_type.lower()

    def parse(self, body, url):
        page = html_page(body, url)
        part = PART_PAGE.match(urlsplit(url).path)
        page['component_url'] = None
        for resource in API_RESOURCE.findall(body.decode('utf-8', 'replace')):
            candidate = self.normalize(resource, url)
            match = candidate and COMPONENT.match(urlsplit(candidate).path)
            if match and part and match.group(1) == part.group(1):
                page['component_url'] = candidate
                break
        return page

    def identity(self, page, url):
        part = PART_PAGE.match(urlsplit(url).path)
        title = TITLE.match(page.get('title') or '')
        if not part or not title or title.group(1).casefold().replace(':', '-') != part.group(1):
            return None
        return {'id': 'micron-' + hashlib.sha256(part.group(1).encode()).hexdigest()[:20], 'name': title.group(1),
                'kind': 'named_product', 'parent_id': None}

    def candidates(self, page, url, is_directory=False):
        target = page.get('component_url')
        return [{'url': target, 'label': 'Micron part specifications (official data component)',
                 'section': 'Specifications', 'role': 'specification', 'rank': 0}] if target else []

    def component_tables(self, body, url):
        document = json.loads(body.decode('utf-8'))
        details = document.get('details') if isinstance(document, dict) else None
        rows, seen = [], set()
        for item in details if isinstance(details, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not isinstance(item.get('value'), str):
                continue
            key = (item['name'].strip(), item['value'].strip())
            if key in seen or not all(key):
                continue
            seen.add(key)
            rows.append([cell(key[0]), cell(key[1])])
        if not rows or str(document.get('response-code')) != '200':
            return {'tables': [], 'method': 'official_component_json_no_execution',
                    'status': 'review_required_no_native_specification',
                    'uncertainty': 'The official part component returned no specification rows.'}
        table = {'index': 1, 'section': str(document.get('part-title') or 'Specifications'), 'rows': rows,
                 'notes': 'Micron 官方型号规格组件（零件页 data-apiresource 指向的 JSON）的名称与取值原文；完全相同的重复行只保留一行。',
                 'is_specification': True, 'method': 'official_component_json_no_execution',
                 'extraction_method': 'official_component_json_no_execution'}
        return {'tables': [table], 'method': table['method'], 'status': 'native_tables_extracted', 'uncertainty': None}
