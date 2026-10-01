import unittest

from fetchspec import seeds
from fetchspec.adapters import adapter_for

PART = 'https://www.asteralabs.com/product-details/pt6162lx/'
FAMILY = 'https://www.asteralabs.com/products/scorpio-smart-fabric-switch/'
BRIEF = 'https://www.asteralabs.com/wp-content/uploads/2025/05/PB_Aries_PCIe_CXL_Smart_DSP_Retimers.pdf'
# Shapes observed 2026-10-01.
PART_HTML = ('<html><body><h1 class="h2">PT6162LX</h1><p>Astera Labs CXL/PCIe 5.0 x8 Low-Latency Smart Retimer</p>'
             '<a href="' + BRIEF + '" download>Product brief</a><a href="/careers/">Careers</a>'
             '<ul class="list-unstyled"><li> <span class="fw-medium"> Package: </span> 354-pin FC-CSP </li>'
             '<li> <span class="fw-medium"> Max PCIe Gen: </span> PCIe 6.x / CXL 3.x </li>'
             '<li> <span class="fw-medium"> Retimer Generation: </span> 3<sup>rd</sup> </li>'
             '<li class="d-flex"> <span class="fw-medium d-inline-block"> Pack Quantity: </span> <p>240, 2400</p> </li></ul>'
             '<ul><li class="fw-semibold"> Ordering: </li><li><a href="/x/">Contact Us</a></li></ul></body></html>').encode()
FAMILY_HTML = ('<html><body><h1>Scorpio Smart Fabric Switch</h1>'
               '<table><tr><td>Benefit</td><td>Lower latency</td></tr></table>'
               '<h2>Ordering Information</h2><table><tr><th>Series</th><th>Part Number</th><th>Protocol</th><th>Lanes</th></tr>'
               '<tr><td>P-Series</td><td>PF60321L</td><td>PCIe 6</td><td>32</td></tr></table></body></html>').encode()


class AsteraLabsAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = adapter_for('asteralabs')

    def test_scope(self):
        for url in (PART, FAMILY):
            self.assertTrue(self.adapter.page_allowed(url), url)
        for url in ('https://www.asteralabs.com/blog/x/', PART + '?x=1', BRIEF, 'https://www.asteralabs.com/wp-admin/'):
            self.assertFalse(self.adapter.page_allowed(url), url)

    def test_part_attributes_are_a_two_column_table_and_the_brief_is_an_attachment(self):
        page = self.adapter.parse(PART_HTML, PART)
        [table] = page['tables']
        self.assertEqual([[c['text'] for c in r] for r in table['rows']],
                         [['Package', '354-pin FC-CSP'], ['Max PCIe Gen', 'PCIe 6.x / CXL 3.x'],
                          ['Retimer Generation', '3 rd'], ['Pack Quantity', '240, 2400']])
        identity = self.adapter.identity(page, PART)
        self.assertEqual((identity['name'], identity['kind']), ('PT6162LX', 'named_product'))
        self.assertEqual([(c['url'], c['role']) for c in self.adapter.candidates(page, PART)], [(BRIEF, 'attachment')])

    def test_family_page_keeps_only_the_part_number_table(self):
        page = self.adapter.parse(FAMILY_HTML, FAMILY)
        self.assertEqual([t['rows'][1][1]['text'] for t in page['tables']], ['PF60321L'])
        self.assertEqual(self.adapter.identity(page, FAMILY)['kind'], 'family_or_directory')
        self.assertEqual(self.adapter.candidates(page, FAMILY), [])

    def test_repository_seeds(self):
        mine = [s for s in seeds.load() if s['company_id'] == 'asteralabs']
        self.assertEqual(sorted(t for s in mine for t in s['targets']), ['P.pcie-switch.spec', 'P.retimer.spec'])
        self.assertTrue(all(self.adapter.page_allowed(s['url']) for s in mine))


if __name__ == '__main__':
    unittest.main()
