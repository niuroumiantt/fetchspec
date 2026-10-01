import unittest

from fetchspec import seeds
from fetchspec.adapters import adapter_for
from fetchspec.siemens_catalog import METHOD, tables_from_text

# pdftotext -layout shapes observed 2026-10-01 (HA 25.73 p.33, 8PS brochure p.30, S8plus p.41), trimmed.
NXAIR_PAGE = '''                                                                                           Technical data
                                                                                                 Electrical data


Electrical data

 Rated values up to 40 kA
 Rated voltage                                                    kV             7.2          12       17.5
 Rated frequency                                                  Hz                        50/60
 Rated short-duration power-frequency                                                  1)
                                                                          kV     20          28 1)     38
 withstand voltage (phase-to-phase, phase-to-earth)
 Rated short-circuit breaking current                              max. kA                    40
 Rated peak withstand current 2)                                   max. kA                 100 / 104
 Enclosure                                                           Standard               IP3XD                    R-HA25-708 psd

1) 32 kV or 42 kV optional for GOST standard
2) Values for 50 Hz: 100 kA, 60 Hz: 104 kA

                                                                         Medium-Voltage Switchgear Type NXAIR up to 17.5 kV, up to 40 kA, Air-Insulated · Siemens HA 25.73 · 2023           33
'''
BUSWAY_PAGE = '''Technical data                                                                                                      LI system
Rated insulation voltage Ui                             1,000 V AC                                                                                                                                      6
Rated operational voltage Ue                            1,000 V AC                                                                                6                                         7
Rated current InA                                       800 A to 6,300 A
Rated short-time withstand current Icw (1 s)            Up to 150 kA
'''
DRAWING_PAGE = '''                                                                                           Technical data
                                                                                                 Product range, single busbar
 Circuit-breaker panel      CB-f
 Rated voltage                kV     7.2          12
 Rated current                A      4000
 Busbar                       A      4000
'''
PROSE_PAGE = '''SIVACON S8plus at a glance   Safety   Digitalization   Technology   Service   Technical data & project checklist
Rated operational voltage Ue   Main circuit        up to 690 V
Main busbars, horizontal       Rated current       up to 7,010 A
Dimensions                     Height              2,000, 2,200 mm
Installation                   Ambient             +35 °C
'''


class SiemensCatalogTests(unittest.TestCase):
    def tables(self, *pages):
        return tables_from_text('\f'.join(pages))

    def test_rows_keep_layout_without_re_columning(self):
        [table] = self.tables(NXAIR_PAGE)
        self.assertEqual((table['page'], table['section'], table['method']), (1, 'p.1 · Electrical data', METHOD))
        rows = [[c['text'] for c in r] for r in table['rows']]
        self.assertIn(['Rated voltage', 'kV', '7.2', '12', '17.5'], rows)
        self.assertIn(['Rated short-circuit breaking current', 'max. kA', '40'], rows, 'one value spanning columns stays one cell')
        # A wrapped label stays split over its printed lines; values stay on their own line.
        index = rows.index(['Rated short-duration power-frequency', '1)'])
        self.assertEqual(rows[index + 1:index + 3], [['kV', '20', '28 1)', '38'], ['withstand voltage (phase-to-phase, phase-to-earth)']])
        self.assertIn(['Enclosure', 'Standard', 'IP3XD'], rows, 'figure reference codes are dropped')
        voltage = table['rows'][rows.index(['Rated voltage', 'kV', '7.2', '12', '17.5'])]
        self.assertEqual([c['x'] for c in voltage], [1, 66, 81, 94, 103])
        self.assertIn('1) 32 kV or 42 kV optional for GOST standard', table['notes'])
        self.assertFalse(any('Siemens HA 25.73' in c['text'] for r in table['rows'] for c in r), 'running footer dropped')

    def test_section_on_the_header_line_and_split_callouts(self):
        [table] = self.tables(BUSWAY_PAGE)
        self.assertEqual(table['section'], 'p.1 · LI system')
        rows = [[c['text'] for c in r] for r in table['rows']]
        self.assertEqual(rows[1], ['Rated operational voltage Ue', '1,000 V AC', '6', '7'])

    def test_drawings_tab_bars_and_soft_hyphens(self):
        self.assertEqual(self.tables(DRAWING_PAGE, PROSE_PAGE), [], 'product-range drawings and pages without the header are skipped')
        [table] = self.tables(NXAIR_PAGE.replace('breaking current', '\xadbreaking\x08current'))
        self.assertIn('Rated short-circuit breaking current', [r[0]['text'] for r in table['rows']])
        self.assertEqual([t['page'] for t in self.tables(PROSE_PAGE, BUSWAY_PAGE, NXAIR_PAGE)], [2, 3])


class SiemensAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = adapter_for('siemens')

    def test_scope_and_followed_documents(self):
        page = 'https://www.siemens.com/en-us/products/energy-systems/nxair/'
        self.assertTrue(self.adapter.page_allowed(page))
        for url in ('https://www.siemens.com/en-us/company/insights/x/', page + '?x=1',
                    'https://www.siemens.com/de-de/products/energy-systems/nxair/'):
            self.assertFalse(self.adapter.page_allowed(url), url)
        asset = 'https://assets.new.siemens.com/siemens/assets/api/uuid:14493ba2-f30a-4bf4-a8fe-c5e38c2c8f83/'
        links = [{'url': asset + name, 'label': name, 'section': ''} for name in (
            'HA-25-73-EN.pdf', 'NXAIR-H-Catalog.pdf', 'NXAIR-Profile.pdf', 'Environmental-Product-Declaration-SIVACON-S8_original.pdf',
            'SIMARIS-control-EN-online.pdf', 'Energy-and-data-successfully-put-on-track.pdf')] + [
            {'url': 'https://example.com/a.pdf', 'label': 'x', 'section': ''}]
        rows = self.adapter.candidates({'links': links}, page)
        self.assertEqual([(r['url'].rsplit('/', 1)[-1], r['rank'], r['role']) for r in rows],
                         [('HA-25-73-EN.pdf', 1, 'attachment'), ('NXAIR-H-Catalog.pdf', 1, 'attachment'),
                          ('Energy-and-data-successfully-put-on-track.pdf', 2, 'attachment')])

    def test_identity_from_title_and_fallback_for_other_pdfs(self):
        page = 'https://www.siemens.com/en-us/products/energy-systems/nxair/'
        identity = self.adapter.identity({'title': 'NXAIR: Air-insulated medium-voltage switchgear | Siemens', 'tables': []}, page)
        self.assertEqual((identity['name'], identity['kind']), ('NXAIR', 'family_or_directory'))
        self.assertIsNone(self.adapter.document_tables(b'%PDF-1.4 no technical data', 'pdf', 'https://example.com/a.pdf'))
        self.assertIsNone(self.adapter.document_tables(b'PK', 'docx', page))

    def test_siemens_energy_rows_are_not_siemens_rows(self):
        from fetchspec.coverage import instance_adapters
        self.assertEqual(instance_adapters({'instances': ['Siemens Energy 变压器', 'Hyosung']}), ['siemens-energy'])
        self.assertEqual(instance_adapters({'instances': ['西门子能源', 'ABB']}), ['siemens-energy'])
        self.assertEqual(instance_adapters({'instances': ['ABB MNS', 'Siemens SIVACON']}), ['siemens'])
        self.assertEqual(instance_adapters({'instances': ['Siemens NXAIR', 'Siemens Energy 8DA']}), ['siemens', 'siemens-energy'])

    def test_repository_seeds(self):
        mine = [s for s in seeds.load() if s['company_id'] == 'siemens']
        self.assertEqual(sorted(t for s in mine for t in s['targets']), ['P.busway.spec', 'P.lv-switchgear.spec', 'P.mv-switchgear.spec'])
        self.assertTrue(all(self.adapter.page_allowed(s['url']) for s in mine))


if __name__ == '__main__':
    unittest.main()
