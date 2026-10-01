import json
import unittest

from fetchspec import seeds
from fetchspec.adapters import adapter_for
from fetchspec.adapters.delta import section_rows, spec_section

BASE = 'https://www.deltaww.com/en-US/products/'
CDU = BASE + 'data-center-cooling/liquid-to-liquid-coolant-distribution-unit-1500kw'
SHELF = BASE + 'orv3-server-power/orv3-33kw-power-system'
DOC = 'https://docs.google.com/document/d/e/2PACX-1vTsfY91JdZG5q8uFZpoDKyJ1yFPUIc_VmYQtDsD_6jPp/pub'
CDU_SECTION = ('<p>&nbsp;</p><ol><li><strong>Model</strong></li><li>GoCool-1500</li>'
               '<li><strong>Nominal Cooling Capacity:&nbsp;</strong></li><li>1500 kW @6&deg;C approach, 1500 LPM</li>'
               '<li><strong>Primary Side</strong></li><li>Coolant Type: Water</li><li>Operation Pressure Drop: 114 kPa</li>'
               '<li>Front: 1200 mm (47.2&#39;&#39;); Rear: 800 mm</li><li>Noise Level: &lt; 76 dBA (at 1 m)</li></ol>')


def page(title, section_content, inline=False):
    """Shape observed 2026-10-01: Next.js server-component payload chunks pushed from <script> tags."""
    if inline:
        sections = [{'title': 'Product Specifications', 'content': section_content}]
        payload = json.dumps({'sections': sections}, separators=(',', ':'))  # compact, as served
    else:
        text_chunk = section_content.encode('utf-8')
        payload = ('0:{"x":1}\n2a:{"sections":[{"title":"Product Specifications","content":"$1c"}]}'
                   + '<p>intro</p>1c:T%x,' % len(text_chunk) + section_content + '2b:{"after":true}')
    scripts = ''.join('<script>self.__next_f.push(%s)</script>' % json.dumps([1, part])
                      for part in (payload[:40], payload[40:]))
    return ('<html><head><title>Products - %s - Delta</title></head><body><h1>%s</h1>%s</body></html>'
            % (title, title, scripts)).encode('utf-8')


class DeltaAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = adapter_for('delta')

    def test_scope_includes_only_product_pages_and_published_spec_documents(self):
        for url in (CDU, SHELF, DOC, DOC + '?embedded=true'):
            self.assertTrue(self.adapter.page_allowed(url), url)
        for url in (BASE[:-1].replace('/products', '/company') + '/about', CDU + '?x=1',
                    'https://docs.google.com/document/d/abc/edit', 'https://docs.google.com/spreadsheets/d/e/2PACX-1vTsfY91JdZG5q8uFZpoDKyJ1y/pub',
                    DOC + '?output=pdf', 'https://www.deltaww.com/api/products'):
            self.assertFalse(self.adapter.page_allowed(url), url)

    def test_text_chunk_reference_is_resolved_by_byte_length(self):
        body = page('LTL CDU, GoCool-1500', CDU_SECTION.replace('Water', 'Water – 純水'))
        self.assertEqual(spec_section(body), CDU_SECTION.replace('Water', 'Water – 純水'))
        self.assertEqual(spec_section(page('X', CDU_SECTION, inline=True)), CDU_SECTION)

    def test_rows_split_only_at_the_vendors_colon(self):
        rows = [[(c['text'], c['header']) for c in r] for r in section_rows(CDU_SECTION)]
        self.assertEqual(rows, [
            [('Model', True)], [('GoCool-1500', False)], [('Nominal Cooling Capacity', True)],
            [('1500 kW @6°C approach, 1500 LPM', False)], [('Primary Side', True)],
            [('Coolant Type', False), ('Water', False)], [('Operation Pressure Drop', False), ('114 kPa', False)],
            [('Front', False), ("1200 mm (47.2''); Rear: 800 mm", False)],
            [('Noise Level', False), ('< 76 dBA (at 1 m)', False)]])

    def test_inline_section_becomes_the_specification_table(self):
        parsed = self.adapter.parse(page('LTL CDU, GoCool-1500', CDU_SECTION), CDU)
        [table] = parsed['tables']
        self.assertEqual((table['method'], table['is_specification']), ('delta_spec_section_rows', True))
        identity = self.adapter.identity(parsed, CDU)
        self.assertEqual((identity['name'], identity['kind']), ('LTL CDU, GoCool-1500', 'named_product'))
        self.assertEqual(self.adapter.candidates(parsed, CDU), [])

    def test_embedded_spec_document_is_followed_and_its_tables_are_specifications(self):
        parsed = self.adapter.parse(page('ORV3 33kW Power System', '<iframe src="%s?embedded=true"></iframe>' % DOC), SHELF)
        self.assertEqual(parsed['tables'], [])
        self.assertEqual([(c['url'], c['role']) for c in self.adapter.candidates(parsed, SHELF)], [(DOC + '?embedded=true', 'specification')])
        self.assertEqual(self.adapter.identity(parsed, SHELF)['kind'], 'named_product')
        doc = self.adapter.parse(b'<html><body><table><tr><td></td><td>5.5 kW PSU</td></tr><tr><td>Efficiency</td>'
                                 b'<td>&gt;97.5 @100% Load</td></tr></table></body></html>', DOC + '?embedded=true')
        self.assertTrue(doc['tables'][0]['is_specification'])
        self.assertEqual(doc['tables'][0]['rows'][1][1]['text'], '>97.5 @100% Load')
        self.assertIsNone(self.adapter.identity(doc, DOC), 'a document is never a product of its own')

    def test_repository_seeds(self):
        mine = [s for s in seeds.load() if s['company_id'] == 'delta']
        self.assertEqual(sorted(t for s in mine for t in s['targets']),
                         ['P.bbu.spec', 'P.cdu.spec', 'P.power-shelf.operation', 'P.power-shelf.spec', 'P.psu.operation', 'P.psu.spec'])
        self.assertTrue(all(self.adapter.page_allowed(s['url']) for s in mine))


if __name__ == '__main__':
    unittest.main()
