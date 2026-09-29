import json
from pathlib import Path
import tempfile
import unittest

from fetchspec.acquisition import collect
from fetchspec.adapters import ADAPTERS, adapter_for
from fetchspec.coverage import adapter_parts, build as coverage
from fetchspec.delivery_v2 import build_package, import_receipt
from fetchspec.product_map import sync_map
from fetchspec.targets import sync_targets
from test_delivery_v2 import item, receipt
from test_targets import upstream

UPS = 'https://www.vertiv.com/en-us/products-catalog/critical-power/uninterruptible-power-supplies-ups/liebert-exl-s1/'
CATEGORY = 'https://www.vertiv.com/en-us/products-catalog/critical-power/uninterruptible-power-supplies-ups/'
DATASHEET = 'https://www.vertiv.com/49f1a9/globalassets/products/critical-power/liebert-exls1_01.pdf'
# Shape observed on the live page (2026-09-29): a "Models" comparison table with no
# "specification" wording, one row naming each model, then that model's values.
UPS_HTML = ('<html><head><title>Liebert EXL S1</title></head><body>'
            '<h1>Vertiv&#8482; Liebert&#174; EXL S1 UPS 250-1200kW </h1>'
            '<table><tr><th>Models</th><th>Power Rating</th><th>Efficiency</th><th>Height</th></tr>'
            '<tr><td colspan="4">Vertiv™ Liebert® EXL S1 UPS 250-400kW</td></tr>'
            '<tr><td></td><td>250, 300, 400 kVA/kW</td><td>Up to 97% double conversion</td><td>79.1 in</td></tr></table>'
            '<a href="/49f1a9/globalassets/products/critical-power/liebert-exls1_01.pdf">Liebert EXL S1 625 - 1200 kVA Data Sheet SL-26095</a>'
            '<a href="/de-emea/products-catalog/critical-power/x/">Deutsch</a>'
            '</body></html>').encode()
CATEGORY_HTML = ('<html><body><h1>Uninterruptible Power Supplies (UPS)</h1>'
                 '<table><tr><td>Find the right UPS</td><td>Contact sales</td></tr></table>'
                 '<a href="' + UPS + '">Liebert EXL S1</a></body></html>').encode()


class Fetcher:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def prepare_robots(self):
        return {}

    def get(self, url, *, headers=None):
        self.calls.append(url)
        return self.responses[url], {'status': 200, 'content_type': 'text/html', 'final_url': url}


class VertivAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adapter = adapter_for('vertiv')

    def test_models_table_is_the_native_specification_and_marks_are_stripped(self):
        page = self.adapter.parse(UPS_HTML, UPS)
        self.assertEqual([t['is_specification'] for t in page['tables']], [True])
        identity = self.adapter.identity(page, UPS)
        self.assertEqual(identity['name'], 'Vertiv Liebert EXL S1 UPS 250-1200kW')
        self.assertEqual(identity['kind'], 'named_product')
        links = self.adapter.candidates(page, UPS)
        self.assertEqual([(l['role'], l['url']) for l in links], [('attachment', DATASHEET)])

    def test_scope_is_official_en_us_catalog_only(self):
        allowed = self.adapter.page_allowed
        self.assertTrue(allowed(UPS))
        self.assertTrue(allowed(CATEGORY))
        for url in ('https://www.vertiv.com/en-us/search/?q=ups', 'https://www.vertiv.com/en-us/about/',
                    DATASHEET, 'https://www.vertiv.com/login/'):
            with self.subTest(url=url):
                self.assertFalse(allowed(url))
        self.assertIsNone(self.adapter.normalize('https://www.vertiv.com/de-emea/products-catalog/x/'))
        self.assertIsNone(self.adapter.normalize('https://example.com/en-us/products-catalog/x/'))

    def test_category_page_is_a_directory_and_expands_to_the_product(self):
        fetcher = Fetcher({CATEGORY: CATEGORY_HTML, UPS: UPS_HTML})
        result = collect(self.root, 'vertiv', [CATEGORY], fetcher=fetcher, max_pages=2)
        kinds = {p['name']: p['kind'] for p in result['products']}
        self.assertEqual(kinds['Uninterruptible Power Supplies (UPS)'], 'family_or_directory')
        self.assertEqual(kinds['Vertiv Liebert EXL S1 UPS 250-1200kW'], 'named_product')
        product = next(p for p in result['products'] if p['kind'] == 'named_product')
        self.assertEqual(product['extraction_status'], 'native_tables_extracted')
        self.assertEqual(product['tables'][0]['rows'][2][1]['text'], '250, 300, 400 kVA/kW')
        self.assertEqual(fetcher.calls, [CATEGORY, UPS])  # the datasheet waits: a native table already exists

    def test_map_sync_refuses_a_company_without_an_official_sitemap(self):
        with self.assertRaisesRegex(ValueError, 'no official product sitemap'):
            sync_map(self.root, 'vertiv', fetcher=Fetcher({}))


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / 'state'
        self.snapshot = sync_targets(upstream(self.base / 'upstream'), self.state)

    def test_every_declared_part_belongs_to_a_registered_adapter(self):
        parts = adapter_parts()
        self.assertEqual(parts['ups'], ['vertiv'])
        self.assertIn('nvidia', parts['gpu'])
        self.assertTrue({a for owners in parts.values() for a in owners} <= set(ADAPTERS))

    def test_stages_follow_package_and_receipt_environment(self):
        report = coverage(self.state, self.snapshot)
        stages = {r['target_id']: r['stage'] for r in report['records']}
        self.assertEqual(stages, {'P.gpu.spec': 'adapter_ready', 'P.server.spec': 'adapter_ready'})
        self.assertEqual(report['summary']['adapter_ready'], 2)
        summary = build_package(self.state, self.snapshot, 'nvidia', [item(self.base)], collector_revision='test')
        self.assertEqual({r['target_id']: r['stage'] for r in coverage(self.state, self.snapshot)['records']}['P.gpu.spec'], 'packaged')
        import_receipt(self.state, receipt(summary), environment='local_receiver_validation')
        row = next(r for r in coverage(self.state, self.snapshot)['records'] if r['target_id'] == 'P.gpu.spec')
        self.assertEqual((row['stage'], row['receipt_environments']), ('received_validation_only', ['local_receiver_validation']))
        import_receipt(self.state, receipt(summary), environment='production')
        row = next(r for r in coverage(self.state, self.snapshot)['records'] if r['target_id'] == 'P.gpu.spec')
        self.assertEqual(row['stage'], 'received')
        self.assertEqual(row['upstream_status'], 'needed')  # copied from the snapshot, never inferred


if __name__ == '__main__':
    unittest.main()
