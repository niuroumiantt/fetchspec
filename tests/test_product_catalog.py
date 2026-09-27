import unittest
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from fetchspec.product_catalog import parse_page, entity_kind, page_allowed, section_products, sitemap_product_category
from fetchspec.product_catalog import sync_product_sitemap, website_page_identity, source_receipt, sitemap_entry_for_page


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
        self.assertTrue(page_allowed('https://www.nvidia.cn/networking/products/data-processing-unit/'))
        self.assertFalse(page_allowed('https://www.nvidia.com/fr-fr/geforce/'))
        self.assertEqual(sitemap_product_category('https://www.nvidia.cn/networking/products/data-processing-unit/'), 'Networking')
        self.assertEqual(sitemap_product_category('https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5090/'), 'Gaming and Creating')
        self.assertIsNone(sitemap_product_category('https://www.nvidia.cn/news/geforce-launch/'))
        self.assertEqual(website_page_identity('https://www.nvidia.com/en-us/geforce/rtx-5090/'),
                         website_page_identity('https://www.nvidia.cn/geforce/rtx-5090/'))

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

    def test_platform_sections_become_distinct_products_with_resource_entry_points(self):
        page = {'links': [
            {'section': 'NVIDIA BlueField-4 DPU', 'label': 'Explore BlueField-4 DPUs', 'url': 'https://resources.nvidia.com/en-us/bluefield-4-datasheet'},
            {'section': 'NVIDIA BlueField-4 STX Storage Processor', 'label': 'Explore BlueField-4 STX Storage Processors', 'url': 'https://resources.nvidia.com/en-us/stx-datasheet'},
            {'section': 'NVIDIA BlueField-3 DPU', 'label': 'Explore BlueField-3 DPUs', 'url': 'https://resources.nvidia.com/en-us/bluefield-3-datasheet'},
            {'section': 'NVIDIA Vera CPU Accelerates AI-Native Storage in BlueField-4 STX', 'label': 'Read the Blog', 'url': 'https://www.nvidia.com/en-us/bluefield-blog'},
        ]}
        products = section_products(page)
        self.assertEqual([p['name'] for p in products], [
            'NVIDIA BlueField-4 DPU', 'NVIDIA BlueField-4 STX Storage Processor', 'NVIDIA BlueField-3 DPU'])
        self.assertTrue(all(len(p['resources']) == 1 for p in products))

    def test_product_sitemap_seeds_only_official_product_paths(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory = root / 'ledger/companies/nvidia/inventory'
            inventory.mkdir(parents=True)
            manifest = inventory / 'urls.jsonl'
            rows = [
                {'url': 'https://www.nvidia.cn/networking/products/ethernet/', 'sources': [{'role': 'china_zh-cn', 'lastmod_claim': '2026-09-26'}]},
                {'url': 'https://www.nvidia.com/en-us/blog/geforce-launch/', 'sources': [{'role': 'en_us', 'lastmod_claim': '2026-09-26'}]},
            ]
            manifest.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            (inventory / 'latest-attempt.json').write_text(json.dumps({'status': 'sitemaps_complete',
                'run_id': 'fixture', 'url_manifest': str(manifest.relative_to(root)), 'unique_url_candidates': 2,
                'sitemaps': []}))
            db = sqlite3.connect(':memory:')
            db.row_factory = sqlite3.Row
            db.executescript('CREATE TABLE frontier(url TEXT PRIMARY KEY,depth INTEGER,parent TEXT,category TEXT,label TEXT,state TEXT DEFAULT "pending",error TEXT); CREATE TABLE pages(url TEXT PRIMARY KEY,payload TEXT);')
            result = sync_product_sitemap(root, db)
            self.assertEqual(result['candidate_urls'], 1)
            self.assertEqual(result['new'], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM frontier').fetchone()[0], 1)
            self.assertIn('Networking', db.execute('SELECT category FROM frontier').fetchone()[0])

    def test_delivery_source_receipt_omits_duplicate_html_and_links(self):
        receipt = source_receipt({'source_url': 'https://www.nvidia.com/en-us/data-center/h200/',
            'requested_url': 'https://www.nvidia.com/en-us/data-center/h200/', 'sha256': 'a'*64,
            'snapshot_path': 'blobs/aa/a.html', 'observed_at': 'now', 'heading': 'H200', 'text': 'x'*100000,
            'links': [{'url': 'https://www.nvidia.com/file.pdf'}], 'http': {'status': 200, 'final_url': 'https://www.nvidia.com/en-us/data-center/h200/', 'content_type': 'text/html'}})
        self.assertEqual(receipt['sha256'], 'a'*64)
        self.assertNotIn('text', receipt)
        self.assertNotIn('links', receipt)

    def test_sitemap_evidence_matches_requested_url_after_canonical_redirect(self):
        entry = {'url': 'https://www.nvidia.cn/geforce/rtx-5090/'}
        page = {'requested_url': entry['url'], 'source_url': 'https://www.nvidia.com/en-us/geforce/rtx-5090/'}
        self.assertIs(sitemap_entry_for_page(page, {entry['url']: entry}), entry)


if __name__ == '__main__':
    unittest.main()
