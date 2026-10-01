"""Micron product catalog: sitemap list → current part specifications → schema-1 catalog with honest coverage."""
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

from fetchspec.micron_catalog import FAMILY_BRIEFS, SITEMAP, Catalog, brief_rows, classify, directory_id, part_id, taxonomy_path, title_case

BASE = 'https://www.micron.com'
RDIMM = '/products/memory/dram-modules/rdimm'
PART = BASE + RDIMM + '/part-catalog/part-detail/mtc40f2046s1rc64bh1'
GAP = BASE + RDIMM + '/part-catalog/part-detail/mtc20f1045s1rc64bd2'
GONE = BASE + RDIMM + '/part-catalog/part-detail/mtc10f1084s1rc64bd1'
IT_PART = BASE + '/products/memory/dram-components/ddr-sdram/part-catalog/part-detail/mt46v16m16cy-5b-it-m'
TWICE = BASE + '/products/memory/data-center-memory/part-catalog/part-detail/mtc40f2046s1rc64bh1'
OBSOLETE = BASE + '/products/obsolete/obsolete-rdimm/part-catalog/part-detail/mt18htf6472dy-53eb2'


def api(path, part):
    return ('/content/micron/us/en' + path + '/part-catalog/part-detail/_jcr_content.products.json/getproductinfo/-/-/-/en_US/-/' + part)


def part_html(title, path, part):
    return ('<html><head><title>' + title + ' part detail | Micron Technology Inc.</title></head><body>'
            '<div data-apiresource="' + api(path, part) + '"></div></body></html>').encode()


def component(rows, status='Production'):
    details = [{'id': 'density', 'name': 'Density', 'value': rows}, {'id': 'production_status', 'name': 'Part Status Code', 'value': status}]
    return json.dumps({'response-code': '200', 'part-title': 'x', 'details': details if rows else []}).encode()


SITEMAP_XML = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(
    f'<url><loc>{u}</loc><lastmod>2026-09-30</lastmod></url>' for u in [
        BASE + '/products', BASE + '/products/memory', BASE + '/products/memory/dram-modules', BASE + RDIMM,
        BASE + RDIMM + '/part-catalog', PART, PART, GAP, GONE, IT_PART, TWICE, OBSOLETE,
        BASE + '/products/obsolete/obsolete-rdimm/part-catalog/part-detail/spd-data/mt18htf6472dy-53eb2',
        BASE + '/about/company']) + '</urlset>').encode()


class Fetcher:
    """Serves fixed bytes; records conditional headers; 404 for GONE's component."""
    def __init__(self):
        self.calls, self.headers = [], []
        self.pages = {
            SITEMAP: SITEMAP_XML,
            BASE + '/products': b'<html><head><title>Products | Micron Technology Inc.</title></head></html>',
            BASE + '/products/memory': b'<html><head><title>Memory | Micron Technology Inc.</title></head><h1>Memory transforms</h1></html>',
            BASE + '/products/memory/dram-modules': b'<html><head><title>DRAM modules | Micron Technology Inc.</title></head></html>',
            BASE + RDIMM: b'<html><head><title>RDIMM | Micron Technology Inc.</title></head></html>',
            PART: part_html('MTC40F2046S1RC64BH1', RDIMM, 'mtc40f2046s1rc64bh1'),
            TWICE: part_html('MTC40F2046S1RC64BH1', '/products/memory/data-center-memory', 'mtc40f2046s1rc64bh1'),
            BASE + api(RDIMM, 'mtc40f2046s1rc64bh1'): component('64GB'),
            BASE + api('/products/memory/data-center-memory', 'mtc40f2046s1rc64bh1'): component('64GB'),
            GAP: part_html('MTC20F1045S1RC64BD2', RDIMM, 'mtc20f1045s1rc64bd2'),
            BASE + api(RDIMM, 'mtc20f1045s1rc64bd2'): component(''),
            GONE: part_html('MTC10F1084S1RC64BD1', RDIMM, 'mtc10f1084s1rc64bd1'),
            IT_PART: part_html('MT46V16M16CY-5B-IT-M', '/products/memory/dram-components/ddr-sdram', 'mt46v16m16cy-5b-it-m'),
            BASE + api('/products/memory/dram-components/ddr-sdram', 'mt46v16m16cy-5b-it-m'): component('256Mb', 'End of Life'),
        }

    def get(self, url, *, headers=None, cap=None):
        self.calls.append(url)
        self.headers.append(headers or {})
        if headers and headers.get('If-None-Match') == '"same"':
            raise HTTPError(url, 304, 'Not Modified', {}, None)
        if url not in self.pages:
            raise HTTPError(url, 404, 'Not Found', {}, None)
        ctype = 'application/xml' if url == SITEMAP else 'application/json' if 'products.json' in url else 'text/html'
        return self.pages[url], {'status': 200, 'final_url': url, 'content_type': ctype, 'etag': '"same"', 'last_modified': None}


class MicronCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fetcher = Fetcher()
        self.catalog = Catalog(Path(self.tmp.name), fetcher=self.fetcher, profile={'max_xml_bytes': 1 << 20})
        self.addCleanup(self.catalog.db.close)

    def test_sitemap_roles(self):
        self.assertEqual(classify(PART), ('part', 'mtc40f2046s1rc64bh1'))
        self.assertEqual(classify(OBSOLETE), ('obsolete', 'mt18htf6472dy-53eb2'))
        dotted = BASE + '/products/multichip-packages/nand-based-mcp/part-catalog/part-detail/mt29gz5a5bpgga-53it.87j'
        self.assertEqual(classify(dotted), ('part', 'mt29gz5a5bpgga-53it.87j'), 'multichip part numbers carry a dot')
        self.assertEqual(classify(dotted.replace('/multichip-packages/nand-based-mcp/', '/obsolete/obsolete-nand-mcp-catalog/'))[0], 'obsolete')
        self.assertEqual(classify(BASE + RDIMM)[0], 'directory')
        self.assertEqual(classify(BASE + RDIMM + '/part-catalog')[0], 'part_catalog_index')
        self.assertEqual(classify(BASE + '/products/obsolete/x/part-catalog/part-detail/spd-data/y')[0], 'spd')
        self.assertEqual(classify(BASE + '/about/company')[0], 'other')
        self.assertEqual(taxonomy_path(urlsplit_path(PART)), ['memory', 'dram-modules', 'rdimm'])
        self.assertEqual(part_id('mtc40f2046s1rc64bh1'), 'micron-52d542752ea687682b78', 'same id as the delivered DRAM part')

    def test_catalog_lists_everything_and_keeps_the_two_denominators_apart(self):
        self.catalog.collect()
        bundle = self.catalog.export()
        products = {p['id']: p for p in bundle['products']}
        part = products[part_id('mtc40f2046s1rc64bh1')]
        self.assertEqual((part['name'], part['kind'], part['listing'], part['official_status']),
                         ('MTC40F2046S1RC64BH1', 'named_product', 'active', 'Production'))
        self.assertEqual([t['name'] for t in part['taxonomy']], ['Memory', 'DRAM modules', 'RDIMM'], 'titles, not h1 slogans')
        self.assertEqual(part['parent_id'], directory_id(RDIMM))
        self.assertEqual([r[0]['text'] for r in part['tables'][0]['rows']], ['Density', 'Part Status Code'])
        self.assertEqual(len(part['listings']), 2, 'listed under two families: one product, both listings')
        gap = products[part_id('mtc20f1045s1rc64bd2')]
        self.assertEqual((gap['extraction_status'], gap['gap_reason']), ('vendor_specification_gap', 'specification_component_returned_no_rows'))
        self.assertEqual(products[part_id('mtc10f1084s1rc64bd1')]['extraction_status'], 'specification_unavailable')
        it = products[part_id('mt46v16m16cy-5b-it-m')]
        self.assertEqual((it['extraction_status'], it['official_status']), ('native_tables_extracted', 'End of Life'))
        obsolete = products[part_id('mt18htf6472dy-53eb2')]
        self.assertEqual((obsolete['listing'], obsolete['tables'], obsolete['source_url']), ('obsolete', [], SITEMAP))
        self.assertNotIn(OBSOLETE, self.fetcher.calls, 'obsolete parts are listed, not fetched')
        c = bundle['coverage']
        self.assertFalse(c['complete'])
        self.assertEqual((c['named_products_current'], c['named_products_current_with_specifications']), (4, 2))
        self.assertEqual((c['entities'], c['entities_with_tables']), (len(products), 2))
        self.assertEqual((c['vendor_specification_gaps'], c['named_products_obsolete_listed'], c['directory_entities']), (1, 1, 4))
        self.assertEqual(c['sitemap']['by_role']['spd'], 1)
        sources = {(s['sha256'], s['source_url']) for s in bundle['sources']}
        for p in products.values():
            self.assertIn((p['source_sha256'], p['source_url']), sources, p['name'])
            for t in p['tables']:
                for ref in t['source_refs']:
                    self.assertIn((ref['sha256'], ref['url']), sources)
            self.assertTrue(p['parent_id'] is None or p['parent_id'] in products)
        for s in bundle['sources']:
            self.assertTrue((Path(self.tmp.name) / s['snapshot_path']).is_file())

    def test_rerun_is_resumable_and_refresh_is_conditional(self):
        self.catalog.collect()
        first = len(self.fetcher.calls)
        self.catalog.collect()
        again = self.fetcher.calls[first:]
        self.assertEqual(again[0], SITEMAP, 'the list is rechecked on every run')
        self.assertEqual(self.fetcher.headers[first].get('If-None-Match'), '"same"', 'conditionally')
        self.assertEqual(set(again[1:]), {BASE + api(RDIMM, 'mtc10f1084s1rc64bd1')}, 'only the failed page is retried')
        self.catalog.collect(refresh=True)
        self.assertTrue(all(h.get('If-None-Match') == '"same"' for h in self.fetcher.headers[first + 1:] if h))
        self.assertEqual(self.catalog.export()['coverage']['named_products_current_with_specifications'], 2,
                         '304 keeps the stored snapshot')


# Shape of the 6600 ION product brief, Table 4, as pdftotext -layout prints it (2026-10-01).
BRIEF = """Micron 6600 ION SSD key specifications
  SSD capacity13                                                        30.72TB        61.44TB        122.88TB       245.76TB
  Form factors                     U.2 (15mm)                           \uf0fc         \uf0fc         \uf0fc         \uf0fc
  Performance14                    Sequential read (MB/s)               14,000         14,000         14,000         13,700
                                   Read latency (µs)                                   100                           108
  Power Consumption & Use          Maximum                                             25W                           30W
  Endurance by Workload (DWPD)15   100% 128KB sequential
                                                                                       1.0
                                   writes
  Table 4: Micron 6600 ION SSD specifications overview
"""


class TitleCaseTests(unittest.TestCase):
    def test_untitled_segments_keep_the_vendor_spelling_of_product_lines(self):
        self.assertEqual(title_case('obsolete-lpddr4'), 'Obsolete LPDDR4')
        self.assertEqual(title_case('obsolete-ddr-sdram'), 'Obsolete DDR SDRAM')
        self.assertEqual(title_case('obsolete-emmc'), 'Obsolete e.MMC')
        self.assertEqual(title_case('4150at-ssd'), '4150AT SSD')
        self.assertEqual(title_case('obsolete-universal-flash-storage'), 'Obsolete Universal Flash Storage')


class ProductBriefTests(unittest.TestCase):
    def test_brief_rows_keep_printed_values_in_order(self):
        rows, raw = brief_rows(BRIEF, 'key specifications', 'Table 4')
        pairs = [(r[0]['text'], r[1]['text']) for r in rows]
        self.assertEqual(pairs, [('SSD capacity13', '30.72TB | 61.44TB | 122.88TB | 245.76TB'),
                                 ('Form factors · U.2 (15mm)', '✓ | ✓ | ✓ | ✓'),
                                 ('Performance14 · Sequential read (MB/s)', '14,000 | 14,000 | 14,000 | 13,700'),
                                 ('Read latency (µs)', '100 | 108'),
                                 ('Power Consumption & Use · Maximum', '25W | 30W'),
                                 ('100% 128KB sequential writes', '1.0')])
        self.assertIn('Endurance by Workload', raw, 'the printed block travels with the rows')

    def test_part_numbers_decode_by_the_briefs_own_scheme(self):
        brief = FAMILY_BRIEFS['6600-ion']
        match = brief['part'].match('MTFDLAL122T8QHF-1BQ1DFCYY')
        self.assertEqual((brief['form_factor'][match.group(1)], brief['capacity'][match.group(2)]), ('U.2 (15mm)', '122.88TB'))
        self.assertNotIn(brief['part'].match('MTFDLBN122T8QHF-1BQ1DFCYY').group(1), brief['form_factor'], 'BN is not in the brief')


def urlsplit_path(url):
    from urllib.parse import urlsplit
    return urlsplit(url).path


if __name__ == '__main__':
    unittest.main()
