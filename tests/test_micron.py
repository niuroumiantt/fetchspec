import json
from pathlib import Path
import tempfile
import unittest

from fetchspec import seeds
from fetchspec.acquisition import collect
from fetchspec.adapters import adapter_for
from fetchspec.delivery_v2 import _validate_format

PART = 'https://www.micron.com/products/memory/hbm/hbm3e/part-catalog/part-detail/mt65b18g16120a00qh-92-a'
API = ('/content/micron/us/en/products/memory/hbm/hbm3e/part-catalog/part-detail/'
       '_jcr_content.products.json/getproductinfo/-/-/-/en_US/-/mt65b18g16120a00qh-92-a')
COMPONENT = 'https://www.micron.com' + API
# Shape observed 2026-10-01: the part page renders specs by script from the JSON it names in data-apiresource.
PART_HTML = ('<html><head><title>MT65B18G16120A00QH-92:A HBM3E part detail | Micron Technology Inc.</title></head><body>'
             '<h1>part detail</h1><div class="productspecs" data-apiresource="' + API + '"></div>'
             '<div data-apiresource="/content/micron/us/en/products/memory/hbm/hbm3e/part-catalog/part-detail/_jcr_content.download.json"></div>'
             '<a href="/products/memory/hbm/hbm3e">HBM3E</a></body></html>').encode()
COMPONENT_JSON = json.dumps({'response-code': '200', 'part-title': 'MT65B18G16120A00QH-92:A', 'details': [
    {'id': 'density', 'name': 'Component Density', 'value': '36GB'},
    {'id': 'speed', 'name': 'Speed', 'value': '1500MHz'},
    {'id': 'speed', 'name': 'Speed', 'value': '1500MHz'},            # the vendor repeats rows verbatim
    {'id': 'mts', 'name': 'MT/s', 'value': '9.2GTPS'},
    {'id': 'bad', 'name': 'Broken', 'value': None}]}).encode()


class Fetcher:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def prepare_robots(self):
        return {}

    def get(self, url, *, headers=None):
        self.calls.append(url)
        body, content_type = self.responses[url]
        return body, {'status': 200, 'content_type': content_type, 'final_url': url}


class MicronAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = adapter_for('micron')

    def test_scope_is_part_pages_and_their_own_component(self):
        self.assertTrue(self.adapter.page_allowed(PART))
        for url in ('https://www.micron.com/products/memory/hbm/hbm3e',
                    'https://www.micron.com/products/obsolete/obsolete-rdimm/part-catalog/part-detail/mt18htf6472dy-53eb2',
                    PART + '?x=1', COMPONENT):
            with self.subTest(url=url):
                self.assertFalse(self.adapter.page_allowed(url))
        self.assertTrue(self.adapter.component_allowed(COMPONENT, 'application/json;charset=utf-8'))
        self.assertFalse(self.adapter.component_allowed(COMPONENT, 'text/html'))
        self.assertFalse(self.adapter.component_allowed(COMPONENT.replace('products.json', 'download.json'), 'application/json'))

    def test_page_names_only_its_own_component_and_part_identity(self):
        page = self.adapter.parse(PART_HTML, PART)
        self.assertEqual(page['component_url'], COMPONENT)
        self.assertEqual([c['url'] for c in self.adapter.candidates(page, PART)], [COMPONENT])
        identity = self.adapter.identity(page, PART)
        self.assertEqual((identity['name'], identity['kind']), ('MT65B18G16120A00QH-92:A', 'named_product'))
        other = PART.replace('mt65b18g16120a00qh-92-a', 'mt65b12g16080a00qg-92-a')
        self.assertIsNone(self.adapter.parse(PART_HTML, other)['component_url'])
        self.assertIsNone(self.adapter.identity(page, other), 'title must name the part in the URL')

    def test_component_rows_are_vendor_text_with_exact_duplicates_collapsed(self):
        [table] = self.adapter.component_tables(COMPONENT_JSON, COMPONENT)['tables']
        self.assertEqual([[c['text'] for c in r] for r in table['rows']],
                         [['Component Density', '36GB'], ['Speed', '1500MHz'], ['MT/s', '9.2GTPS']])
        self.assertEqual(table['method'], 'official_component_json_no_execution')
        empty = self.adapter.component_tables(json.dumps({'response-code': '404', 'details': []}).encode(), COMPONENT)
        self.assertEqual((empty['tables'], empty['status']), ([], 'review_required_no_native_specification'))

    def test_collect_attaches_component_tables_to_the_part(self):
        with tempfile.TemporaryDirectory() as directory:
            fetcher = Fetcher({PART: (PART_HTML, 'text/html'), COMPONENT: (COMPONENT_JSON, 'application/json')})
            result = collect(Path(directory), 'micron', [PART], fetcher=fetcher, max_pages=3)
            self.assertEqual(fetcher.calls, [PART, COMPONENT])
            [product] = result['products']
            self.assertEqual(product['extraction_status'], 'native_tables_extracted')
            self.assertEqual(product['tables'][0]['source_refs'][0]['url'], COMPONENT)
            source = next(s for s in result['sources'] if s['source_url'] == COMPONENT)
            self.assertEqual(source['format'], 'json')

    def test_component_link_is_not_fetched_as_a_page_and_other_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            html_instead = Fetcher({PART: (PART_HTML, 'text/html'), COMPONENT: (b'<html>moved</html>', 'text/html')})
            result = collect(Path(directory), 'micron', [PART], fetcher=html_instead, max_pages=3)
            self.assertEqual(result['products'][0]['tables'], [])
            self.assertTrue(result['acquisition_report']['errors'])

    def test_package_admits_json_objects_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'c.json'
            path.write_bytes(COMPONENT_JSON)
            _validate_format(path, 'json')
            for body in (b'[1]', b'{"a":', b'<html>'):
                path.write_bytes(body)
                with self.assertRaises(ValueError):
                    _validate_format(path, 'json')

    def test_repository_micron_seeds_are_part_pages(self):
        micron = [s for s in seeds.load() if s['company_id'] == 'micron']
        self.assertEqual(sorted(t for s in micron for t in s['targets']), ['P.dram.spec', 'P.hbm.spec', 'P.ssd.spec'])
        self.assertTrue(all(self.adapter.page_allowed(s['url']) for s in micron))


if __name__ == '__main__':
    unittest.main()
