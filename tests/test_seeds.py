import json
from pathlib import Path
import tempfile
import unittest

from fetchspec import seeds
from fetchspec.adapters import adapter_for
from fetchspec.adapters.vertiv import print_spec_table
from fetchspec.extraction import html_page
from fetchspec.products import ProductStore
from fetchspec.targets import sync_targets
from test_targets import upstream
from test_vertiv_and_coverage import CATEGORY, CATEGORY_HTML, UPS, UPS_HTML, Fetcher

BASE = 'https://www.vertiv.com/en-us/products-catalog/thermal-management/high-density-solutions/'
CDU = BASE + 'vertiv-coolchip-cdu-600/'
SHELL = BASE + 'retired-page/'
# Shape observed on single-model pages (2026-10-01): no HTML table; the chart exists only as the
# pdfmake definition behind the Print button, with JS-escaped strings and HTML entities.
PRINT_DEFINITION = r"""<script>
    var docDefinitionSpecs = {
        content: [
            { style: 'pageHeader', table: { widths: ['*'], body: [[ { image: 'data:image/png;base64,AAAA' } ]] } },
            { style: 'specsTable', alignment: 'center', table: { widths: ['40%', '60%'], body: [
                [ { text: 'Vertiv™ CoolChip CDU 600', style: 'specsTableHeader', colSpan: 2 }, { text: '' } ],
                [ { text: 'Technical Specifications', style: 'specsTableSection', colSpan: 2 }, { text: '' } ],
                [ { text: 'Net Sensible Nominal Capacity kBtuh[kW]*', style: 'specsTableCellOdd' },
                  { text: '600 kW at 7.2\u2070F (4\u2070C) approach', style: 'specsTableCellOdd' }
                // ReSharper disable once ElidedTrailingElement
                ],
                // Coma is needed here
                [ { text: 'Vendor\'s note', style: 'specsTableCellEven' }, { text: 'Liebert&#174; XDU family', style: 'specsTableCellEven' } ],
            ] } }
        ],
        styles: { specsTableCellOdd: { fillColor: '#FFFFFF' } }
    };
    function getSpecsTable(action) { pdfMake.createPdf(docDefinitionSpecs).print(); }
</script>"""
CDU_HTML = ('<html><head><title>600 kW | Vertiv CoolChip 600 CDU</title></head><body>'
            '<div class><h1>Vertiv&#8482; CoolChip CDU 600</h1></div>' + PRINT_DEFINITION + '</body></html>').encode()
# Several retired Vertiv pages answer 200 with site chrome only: no title, no heading, no chart.
SHELL_HTML = b'<html><head><title>\r\n  </title></head><body><nav>Menu</nav><p>Sorry, an error was encountered.</p></body></html>'


def seed_file(directory, company, entries, name=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f'{name or company}.json').write_text(json.dumps(
        {'company_id': company, 'reviewed_at': '2026-10-01', 'seeds': entries}))


class SeedLoadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name) / 'seeds'

    def test_repository_seeds_are_valid_and_answer_part_rows(self):
        loaded = seeds.load()
        self.assertGreater(len(loaded), 20)
        for seed in loaded:
            self.assertTrue(all(t.startswith('P.') for t in seed['targets']), seed['url'])
        self.assertEqual(len({(s['url'], tuple(s['targets'])) for s in loaded}), len(loaded), 'no duplicate seeds')

    def test_valid_seed_is_normalized_and_defaults_apply(self):
        seed_file(self.dir, 'vertiv', [{'targets': ['P.ups.spec', 'P.ups.operation'], 'url': UPS, 'reason': ' 官方 Models 表 '}])
        [seed] = seeds.load(self.dir)
        self.assertEqual((seed['targets'], seed['reason'], seed['max_pages']), (['P.ups.operation', 'P.ups.spec'], '官方 Models 表', 3))
        self.assertEqual(seeds.by_target([seed]), {'P.ups.operation': [seed], 'P.ups.spec': [seed]})

    def test_invalid_seeds_are_rejected(self):
        good = {'targets': ['P.ups.spec'], 'url': UPS, 'reason': 'r'}
        cases = {
            'outside scope': {**good, 'url': 'https://www.vertiv.com/en-us/search/?q=ups'},
            'other host': {**good, 'url': 'https://example.com/en-us/products-catalog/x/'},
            'no targets': {**good, 'targets': []},
            'duplicate targets': {**good, 'targets': ['P.ups.spec', 'P.ups.spec']},
            'no reason': {**good, 'reason': '  '},
            'long reason': {**good, 'reason': 'x' * 301},
            'too many pages': {**good, 'max_pages': 21},
            'pages not int': {**good, 'max_pages': '3'},
        }
        for label, entry in cases.items():
            with self.subTest(label):
                seed_file(self.dir, 'vertiv', [entry])
                with self.assertRaises(ValueError):
                    seeds.load(self.dir)
        seed_file(self.dir, 'vertiv', [good], name='other')
        (self.dir / 'vertiv.json').unlink()
        with self.assertRaisesRegex(ValueError, 'match the file name'):
            seeds.load(self.dir)


class SeedRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / 'root'
        self.snapshot = sync_targets(upstream(base / 'upstream'), self.root)
        self.store = ProductStore(self.root)
        self.addCleanup(self.store.db.close)
        self.fetcher = Fetcher({CDU: CDU_HTML, SHELL: SHELL_HTML, CATEGORY: CATEGORY_HTML, UPS: UPS_HTML})
        self.seeds = [
            {'company_id': 'vertiv', 'url': CDU, 'targets': ['P.gpu.spec'], 'reason': 'reviewed chart', 'product': 'CDU 600', 'max_pages': 2},
            {'company_id': 'vertiv', 'url': SHELL, 'targets': ['P.server.spec'], 'reason': 'r', 'product': None, 'max_pages': 2},
            {'company_id': 'vertiv', 'url': CATEGORY, 'targets': ['P.server.spec'], 'reason': 'r', 'product': None, 'max_pages': 2},
            {'company_id': 'vertiv', 'url': UPS, 'targets': ['P.gone.spec'], 'reason': 'r', 'product': None, 'max_pages': 2},
        ]

    def bindings(self):
        return sorted(tuple(r) for r in self.store.db.execute('SELECT target_id, product_id, reason FROM bindings'))

    def test_dry_run_plans_without_requests(self):
        result = seeds.run(self.root, self.store, self.snapshot, self.seeds, dry_run=True, fetcher=self.fetcher)
        self.assertEqual(result['counts'], {'planned': 3, 'skipped': 1})
        self.assertEqual(self.fetcher.calls, [])

    def test_binds_only_the_product_at_the_seed_url_with_tables(self):
        result = seeds.run(self.root, self.store, self.snapshot, self.seeds, bind=True, fetcher=self.fetcher)
        status = {r['url']: r['status'] for r in result['results']}
        self.assertEqual(status, {CDU: 'collected', SHELL: 'no_product', CATEGORY: 'no_tables', UPS: 'skipped'})
        self.assertEqual(result['bound_targets'], ['P.gpu.spec'])
        [(target, product, reason)] = self.bindings()
        self.assertEqual((target, reason), ('P.gpu.spec', 'seed: reviewed chart'))
        cdu = next(r for r in result['results'] if r['url'] == CDU)
        self.assertEqual(cdu['bound']['products'], [product])
        # The category page linked to the UPS page and it was collected, but it is not the seed URL.
        self.assertIn(UPS, self.fetcher.calls)
        self.assertEqual(result['requests_used'], len(self.fetcher.calls))

    def test_budget_stops_the_run(self):
        result = seeds.run(self.root, self.store, self.snapshot, self.seeds, budget=1, bind=True, fetcher=self.fetcher)
        self.assertEqual([r['status'] for r in result['results']], ['collected', 'skipped', 'skipped', 'skipped'])
        self.assertEqual(result['results'][1]['why'], 'request budget used up')
        self.assertEqual(len(self.fetcher.calls), 1)

    def test_rows_closed_upstream_are_skipped_unless_asked(self):
        close = lambda rows: [{**t, 'status': 'delivered'} if t.get('team') == 'fetchspec' else t for t in rows]
        closed = {**self.snapshot, 'targets': close(self.snapshot['targets']),
                  'target_document': {**self.snapshot['target_document'], 'targets': close(self.snapshot['target_document']['targets'])}}
        result = seeds.run(self.root, self.store, closed, self.seeds[:1], fetcher=self.fetcher)
        self.assertEqual(result['results'][0]['why'], 'targets already sourced, assumed or delivered upstream')
        result = seeds.run(self.root, self.store, closed, self.seeds[:1], include_closed=True, fetcher=self.fetcher)
        self.assertEqual(result['results'][0]['status'], 'collected')


class VertivPrintChartTests(unittest.TestCase):
    def test_single_model_chart_is_read_from_literals_without_execution(self):
        adapter = adapter_for('vertiv')
        page = adapter.parse(CDU_HTML, CDU)
        [table] = page['tables']
        self.assertEqual(table['method'], 'vendor_print_definition_literal_no_execution')
        self.assertTrue(table['is_specification'])
        rows = [[c['text'] for c in r] for r in table['rows']]
        self.assertEqual(rows, [['Vertiv™ CoolChip CDU 600'], ['Technical Specifications'],
                                ['Net Sensible Nominal Capacity kBtuh[kW]*', '600 kW at 7.2⁰F (4⁰C) approach'],
                                ["Vendor's note", 'Liebert® XDU family']])
        self.assertTrue(table['rows'][1][0]['header'])
        identity = adapter.identity(page, CDU)
        self.assertEqual((identity['kind'], identity['name']), ('named_product', 'Vertiv CoolChip CDU 600'))

    def test_all_na_chart_is_not_a_specification(self):
        body = PRINT_DEFINITION.replace(r'600 kW at 7.2\u2070F (4\u2070C) approach', 'N/A').replace('Liebert&#174; XDU family', 'n/a')
        self.assertFalse(print_spec_table(body)['is_specification'])

    def test_native_models_table_wins_over_the_print_chart(self):
        body = UPS_HTML.replace(b'</body>', PRINT_DEFINITION.encode() + b'</body>')
        methods = [t['method'] for t in adapter_for('vertiv').parse(body, UPS)['tables']]
        self.assertEqual(methods, ['html_table'])

    def test_pages_without_a_chart_or_heading_have_no_identity(self):
        adapter = adapter_for('vertiv')
        self.assertIsNone(print_spec_table(SHELL_HTML.decode()))
        self.assertIsNone(adapter.identity(adapter.parse(SHELL_HTML, SHELL), SHELL))

    def test_valueless_attributes_do_not_break_parsing(self):
        page = html_page(b'<div class><section id><h1>Name</h1><table class><tr><td>Specification</td><td>1</td></tr></table></section></div>', CDU)
        self.assertEqual(page['heading'], 'Name')
        self.assertEqual(len(page['tables']), 1)


class RegionalAndDocPageTests(unittest.TestCase):
    def test_vertiv_regional_english_catalogs_and_mixed_case_share_identity(self):
        adapter = adapter_for('vertiv')
        us = 'https://www.vertiv.com/en-us/products-catalog/critical-power/dc-power-systems/powerdirect-3000-33kW/'
        latam = 'https://www.vertiv.com/en-latam/products-catalog/critical-power/dc-power-systems/PowerDirect-3000-33kw/'
        self.assertTrue(adapter.page_allowed(us) and adapter.page_allowed(latam))
        self.assertFalse(adapter.page_allowed('https://www.vertiv.com/de-emea/products-catalog/critical-power/x/'))
        page = adapter.parse(CDU_HTML, us)
        self.assertEqual(adapter.identity(page, us)['id'], adapter.identity(page, latam)['id'])

    def test_nvidia_networking_docs_specification_page_names_the_product(self):
        adapter = adapter_for('nvidia')
        url = 'https://networking-docs.nvidia.com/sn5000hw/specifications'
        self.assertTrue(adapter.page_allowed(url))
        self.assertFalse(adapter.page_allowed('https://networking-docs.nvidia.com/sn5000hw/introduction'))
        page = {'title': 'Specifications | NVIDIA Spectrum-4 SN5000 2U Switch Systems Hardware User Manual', 'tables': []}
        identity = adapter.identity(page, url)
        self.assertEqual((identity['kind'], identity['name']), ('named_product', 'NVIDIA Spectrum-4 SN5000 2U Switch Systems'))


if __name__ == '__main__':
    unittest.main()
