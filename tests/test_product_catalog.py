import unittest
from fetchspec.product_catalog import parse_page, entity_kind, page_allowed


class ProductCatalogTests(unittest.TestCase):
    def test_native_specification_preserves_variants_spans_notes_and_excludes_navigation(self):
        html = b'''<header><a href="/en-us/unrelated/">H100</a></header><h1>NVIDIA H200 GPU</h1>
          <h2>Specifications</h2><table><tr><td></td><td>SXM</td><td>NVL</td></tr>
          <tr><td>Memory</td><td colspan="2">141 GB</td></tr></table>
          <p>* Peak values; sparse performance.</p><h2>Resources</h2><a href="/spec.pdf">Datasheet</a>'''
        data = parse_page(html, 'https://www.nvidia.com/en-us/data-center/h200/')
        self.assertEqual(data['heading'], 'NVIDIA H200 GPU')
        self.assertEqual(len(data['links']), 1)
        table = data['tables'][0]
        self.assertTrue(table['is_specification'])
        self.assertEqual(table['rows'][1][1]['colspan'], 2)
        self.assertIn('sparse', table['notes'])

    def test_scope_and_classification_do_not_turn_family_into_shipping_product(self):
        self.assertEqual(entity_kind('NVIDIA HGX Platform', 'https://www.nvidia.com/en-us/data-center/hgx/'), 'family_or_directory')
        self.assertEqual(entity_kind('NVIDIA GB300 NVL72', ''), 'named_product')
        self.assertFalse(page_allowed('https://www.nvidia.com/de-de/data-center/h200/'))
        self.assertFalse(page_allowed('https://evil.example/en-us/h200/'))
        self.assertFalse(page_allowed('https://www.nvidia.com/en-us/about-nvidia/news/'))

    def test_spec_grid_preserves_superscript_and_ignores_marketing_grid(self):
        html = b'''<h1>DGX Station</h1><h2>Overview</h2><div class="nv-flexbox"><div class="nv-text">Marketing</div><div class="nv-text">Claim</div></div>
        <h2>Specifications</h2><h3>DGX Station Specifications</h3><div class="nv-flexbox"><div class="nv-text">FP4</div><div class="nv-text">20 | 15<sup>3</sup> PFLOPS</div></div><p>3. Without sparsity.</p>'''
        result = parse_page(html, 'https://www.nvidia.com/en-us/products/workstations/dgx-station/')
        self.assertEqual(len(result['tables']), 1)
        self.assertEqual(result['tables'][0]['rows'][0][1]['text'], '20 | 15^{3} PFLOPS')
        self.assertIn('Without sparsity', result['tables'][0]['notes'])

    def test_inline_markup_does_not_split_model_names(self):
        data = parse_page(b'<h1><span>G</span>eForce RTX 5090</h1>', 'https://www.nvidia.com/en-us/geforce/')
        self.assertEqual(data['heading'], 'GeForce RTX 5090')

    def test_model_navigation_is_evidence_even_when_body_is_dynamic(self):
        data = parse_page(b'<nav><a href="/en-us/geforce/graphics-cards/50-series/rtx-5090/">RTX 5090</a><a href="/en-us/news/">News</a></nav><h1>GeForce RTX 50 Series</h1>', 'https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/')
        self.assertEqual(len(data['links']), 1)
        self.assertEqual(data['links'][0]['role'], 'model_navigation')
        self.assertEqual(entity_kind('GeForce RTX 4070 Family', ''), 'family_or_directory')


if __name__ == '__main__':
    unittest.main()
