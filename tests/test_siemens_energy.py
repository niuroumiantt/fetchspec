from pathlib import Path
import tempfile
import unittest

from fetchspec import seeds
from fetchspec.acquisition import collect
from fetchspec.adapters import adapter_for
from test_vertiv_and_coverage import Fetcher

BASE = 'https://www.siemens-energy.com/global/en/home/products-services/product/'
SGT = BASE + 'sgt-800.html'
GIS = BASE + 'eu-life-blue-420kV-gis.html'
# Shape observed 2026-10-01: performance tables with one column per rating and no "specification" wording.
SGT_HTML = ('<html><head><title>SGT-800 gas turbine: substantial steam-raising capability</title></head><body>'
            '<h1>SGT-800 gas turbine: substantial steam-raising capability</h1>'
            '<h3>Performance data for simple cycle power generation</h3>'
            '<table><tr><th></th><th>62 MW rating</th><th>57 MW rating</th></tr>'
            '<tr><td>Gross power output</td><td>62.5 MW(e)</td><td>57.0 MW(e)</td></tr>'
            '<tr><td>Gross heat rate</td><td>8,759 kJ/kWh (8,302 Btu/kWh)</td><td>8,970 kJ/kWh (8,502 Btu/kWh)</td></tr></table>'
            '<h3>Physical dimensions and weight</h3>'
            '<table><tr><td>Length</td><td>20 m</td></tr></table>'
            '<h3>Customer references</h3>'
            '<table><tr><td>Plant</td><td>Country</td></tr><tr><td>Example</td><td>Sweden</td></tr></table>'
            '</body></html>').encode()
GIS_HTML = (b'<html><head><title>LIFE Blue GIS 420 kV</title></head><body>'
            b'<h1>LIFE Blue GIS 420 kV - high-voltage and SF6-free</h1><p>Clean air insulation.</p></body></html>')


class SiemensEnergyAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = adapter_for('siemens-energy')

    def test_scope_is_global_english_product_pages(self):
        self.assertTrue(self.adapter.page_allowed(SGT))
        self.assertTrue(self.adapter.page_allowed(GIS))
        for url in ('https://www.siemens-energy.com/global/en/home/products-services/product-offerings/gas-turbines.html',
                    'https://www.siemens-energy.com/us/en/home/products-services/product/sgt-800.html',
                    'https://www.siemens-energy.com/global/en/home/stories/gas-turbine-iowa-power-demand.html',
                    SGT + '?x=1', 'https://www.siemens.com/en-us/products/energy-systems/nxair/'):
            with self.subTest(url=url):
                self.assertFalse(self.adapter.page_allowed(url))

    def test_named_sections_are_specifications_and_others_are_not(self):
        page = self.adapter.parse(SGT_HTML, SGT)
        self.assertEqual([(t['section'], t['is_specification']) for t in page['tables']],
                         [('Performance data for simple cycle power generation', True),
                          ('Physical dimensions and weight', True), ('Customer references', False)])
        identity = self.adapter.identity(page, SGT)
        self.assertEqual((identity['name'], identity['kind']), ('SGT-800 gas turbine', 'named_product'))

    def test_page_without_tables_is_a_family_page(self):
        page = self.adapter.parse(GIS_HTML, GIS)
        self.assertEqual(self.adapter.identity(page, GIS)['kind'], 'family_or_directory')

    def test_collect_keeps_vendor_values(self):
        with tempfile.TemporaryDirectory() as directory:
            result = collect(Path(directory), 'siemens-energy', [SGT], fetcher=Fetcher({SGT: SGT_HTML}), max_pages=2)
            [product] = result['products']
            self.assertEqual(product['extraction_status'], 'native_tables_extracted')
            self.assertEqual(product['tables'][0]['rows'][2][1]['text'], '8,759 kJ/kWh (8,302 Btu/kWh)')

    def test_repository_seeds(self):
        mine = [s for s in seeds.load() if s['company_id'] == 'siemens-energy']
        self.assertEqual(sorted(t for s in mine for t in s['targets']),
                         ['P.gas-turbine.operation', 'P.gas-turbine.spec', 'P.transformer.spec'])
        self.assertTrue(all(self.adapter.page_allowed(s['url']) for s in mine))


if __name__ == '__main__':
    unittest.main()
