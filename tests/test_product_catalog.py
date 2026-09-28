import unittest
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from fetchspec.product_catalog import parse_page, entity_kind, page_allowed, section_products, sitemap_product_category
from fetchspec.product_catalog import sync_product_sitemap, website_page_identity, source_receipt, sitemap_entry_for_page
from fetchspec.product_catalog import pdf_spec_tables_from_text, pdf_matrix_products
from fetchspec.product_catalog import comparison_model_identity, product_identifier, frontier_failure_state, previous_page_identities
from urllib.error import HTTPError


class ProductCatalogTests(unittest.TestCase):
    def test_previous_catalog_not_stale_product_map_anchors_page_identity(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / 'catalog.json').write_text(json.dumps({'products': [
                {'id': 'nvidia-active', 'name': 'RTX A1000', 'kind': 'family_or_directory',
                 'source_url': 'https://www.nvidia.cn/products/workstations/rtx-a1000/'},
                {'id': 'nvidia-model', 'name': 'RTX A1000 model', 'kind': 'named_product',
                 'source_url': 'https://www.nvidia.cn/products/workstations/rtx-a1000/'},
            ]}))
            exact, paths = previous_page_identities(base)
            self.assertEqual(exact, {
                'https://www.nvidia.cn/products/workstations/rtx-a1000/': 'nvidia-active'})
            self.assertEqual(paths['/products/workstations/rtx-a1000'], 'nvidia-active')

    def test_frontier_failure_states_separate_vendor_removal_and_policy(self):
        missing = HTTPError('https://www.nvidia.com/old', 404, 'Not Found', {}, None)
        self.assertEqual(frontier_failure_state(missing), 'unavailable')
        self.assertEqual(frontier_failure_state(
            ValueError('redirect or URL outside HTTPS host allowlist')), 'policy_blocked')
        self.assertEqual(frontier_failure_state(TimeoutError('timed out')), 'failed')

    def test_embedded_comparison_model_has_distinct_scoped_identity(self):
        official = 'NVIDIA RTX PRO 4000 Blackwell'
        desktop, desktop_scope = comparison_model_identity(
            official, 'https://www.nvidia.com/en-us/products/workstations/professional-desktop-gpus/')
        embedded, embedded_scope = comparison_model_identity(
            official, 'https://www.nvidia.com/en-us/products/workstations/rtx-embedded/')
        self.assertEqual((desktop, desktop_scope), (official, None))
        self.assertEqual((embedded, embedded_scope),
                         ('NVIDIA RTX PRO 4000 Blackwell (Embedded GPU)', 'embedded_gpu'))
        self.assertNotEqual(product_identifier(desktop), product_identifier(embedded))

    def test_native_pdf_spec_parser_preserves_rows_and_does_not_guess_unheaded_text(self):
        text = '''Marketing copy with 800 GB/s bandwidth\n\nTechnical Specifications\n\n GPU Architecture                       NVIDIA Blackwell Architecture\n CUDA Cores                              10,496\n GPU Memory                              32 GB GDDR7\n Memory Bandwidth                        800 GB/s\n\nReady to Get Started'''
        tables = pdf_spec_tables_from_text(text)
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]['section'], 'Technical Specifications')
        self.assertEqual([row[0]['text'] for row in tables[0]['rows']],
                         ['GPU Architecture', 'CUDA Cores', 'GPU Memory', 'Memory Bandwidth'])
        self.assertEqual(tables[0]['rows'][-1][1]['text'], '800 GB/s')
        self.assertEqual(pdf_spec_tables_from_text('GPU Architecture       NVIDIA Blackwell\nCUDA Cores  10000'), [])

    def test_native_pdf_spec_parser_isolates_right_hand_specification_panel(self):
        left_width = 118
        lines = [
            'Extending InfiniBand Performance to Remote',
            ' ' * left_width + 'Specifications',
            'Infrastructures and the Edge',
            ' ' * left_width + 'Performance    400Gb/s bidirectional',
            f'{"The NVIDIA MetroX system seamlessly extends the reach.":<{left_width}}               throughput',
            f'{"and native remote direct-memory access communications.":<{left_width}}Connectors     4x QSFP112 for data',
            f'{"over long distances.":<{left_width}}               2x QSFP112 for InfiniBand',
            '',
            f'{"Optimized for High Density and Scalability":<{left_width}}Management     4x RJ45: 2x 1GbE ports',
            f'{"Marketing copy continues in the left pane.":<{left_width}}ports          and 2x 10GbE ports',
            f'{"More unrelated prose.":<{left_width}}Software       NVDA-OS-XC',
            'x' * (left_width + 9) + 'overflow from the left pane',
        ]
        text = '\n'.join(lines)
        tables = pdf_spec_tables_from_text(text)
        self.assertEqual(len(tables), 1)
        self.assertEqual([(row[0]['text'], row[1]['text']) for row in tables[0]['rows']], [
            ('Performance', '400Gb/s bidirectional throughput'),
            ('Connectors', '4x QSFP112 for data 2x QSFP112 for InfiniBand'),
            ('Management ports', '4x RJ45: 2x 1GbE ports and 2x 10GbE ports'),
            ('Software', 'NVDA-OS-XC'),
        ])

    def test_native_pdf_spec_parser_finds_inline_right_panel_and_stacked_values(self):
        left_width = 92
        text = '\n'.join([
            f'{"Marketing paragraph in the left column.":<{left_width}}Product Specifications',
            f'{"More marketing copy.":<{left_width}}Supported Network Protocols',
            ' ' * left_width + '> Ethernet',
            ' ' * left_width + '> InfiniBand',
            f'{"Unrelated prose.":<{left_width}}Total Bandwidth',
            ' ' * left_width + '> 800 Gb/s',
            '',
            f'{"More prose.":<{left_width}}Host Interface',
            ' ' * left_width + '> PCIe Gen6: up to 48 lanes',
        ])
        tables = pdf_spec_tables_from_text(text)
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]['section'], 'Product Specifications')
        self.assertEqual([(row[0]['text'], row[1]['text']) for row in tables[0]['rows']], [
            ('Supported Network Protocols', 'Ethernet; InfiniBand'),
            ('Total Bandwidth', '800 Gb/s'),
            ('Host Interface', 'PCIe Gen6: up to 48 lanes'),
        ])

    def test_native_pdf_spec_parser_preserves_bullet_portfolio(self):
        text = '\n'.join([
            'Product overview                              Portfolio',
            ' ' * 46 + '> 1 or 2 ports with up to 400 Gb/s connectivity',
            ' ' * 46 + '> 32 GB on-board DDR5 memory',
            ' ' * 46 + '> 1 GbE out-of-band',
            ' ' * 46 + '> management port',
            ' ' * 46 + '> Integrated BMC',
        ])
        table = pdf_spec_tables_from_text(text)[0]
        self.assertEqual([row[1]['text'] for row in table['rows']], [
            '1 or 2 ports with up to 400 Gb/s connectivity',
            '32 GB on-board DDR5 memory',
            '1 GbE out-of-band management port',
            'Integrated BMC',
        ])

    def test_native_pdf_matrix_uses_column_gutters_and_splits_models(self):
        text = '''Technical Specifications*

 Switch Model                                                    SN6800-LD                                       SN6810-LD                               SN6600-LD

 Optical form factor                                      512 MMC-12 800 Gb/s                            128 MMC-12 800 Gb/s                           64 OSFP 800 GbE
                                                           co-packaged optics                             co-packaged optics                               liquid-DC
 Switching capacity (Tb/s)                                        409.6 Tb/s                                      102.4 Tb/s                              102.4 Tb/s
                                                                [4x 102.4 Tb/s]
 CPU                                                         16-core x86, AMD                                8-core x86, AMD                           8-core x86, AMD
 Cooling specifications                                          Liquid                                          Liquid                                     Liquid
                                                        connector: 10x UQD8 v2                           connector: 4x UQD8 v2                      connector: 2x UQD8 v2
*Hardware capabilities.'''
        table = pdf_spec_tables_from_text(text)[0]
        self.assertEqual([cell['text'] for cell in table['rows'][0]],
                         ['Switch Model', 'SN6800-LD', 'SN6810-LD', 'SN6600-LD'])
        self.assertEqual([cell['text'] for cell in table['rows'][1]], [
            'Optical form factor', '512 MMC-12 800 Gb/s co-packaged optics',
            '128 MMC-12 800 Gb/s co-packaged optics', '64 OSFP 800 GbE liquid-DC'])
        self.assertEqual(table['rows'][3][1]['text'], '16-core x86, AMD')
        evidence = {'id': 'doc', 'name': 'SN6000 Datasheet', 'category': 'Networking',
            'categories': ['Networking'], 'source_url': 'https://resources.nvidia.com/sn6000',
            'source_sha256': 'a' * 64, 'observed_at': '2026-01-01T00:00:00Z',
            'tables': [{**table, 'source_refs': [{'url': 'https://example/spec.pdf', 'sha256': 'b' * 64}]}],
            'attachments': [], 'official_pages': [], 'website_sitemap': {'matched': False}}
        parent = {'id': 'series', 'name': 'NVIDIA Spectrum-6 SN6000 Series',
            'category': 'Networking', 'categories': ['Networking'], 'source_url': 'https://nvidia.com/sn6000',
            'official_resources': [{'url': evidence['source_url']}]}
        children = pdf_matrix_products([evidence, parent])
        self.assertEqual([child['name'] for child in children],
                         ['NVIDIA SN6800-LD', 'NVIDIA SN6810-LD', 'NVIDIA SN6600-LD'])
        self.assertTrue(all(child['parent_id'] == 'series' for child in children))
        self.assertEqual(children[0]['tables'][0]['rows'][2][1]['text'], '16-core x86, AMD')

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

    def test_empty_or_invalid_html_cell_spans_default_to_one(self):
        data = parse_page(b'<h1>Specifications</h1><table><tr><th colspan="">Metric</th><td rowspan="n/a">Value</td></tr></table>',
                          'https://networking-docs.nvidia.com/connectx5vpiocp2hw/specifications')
        cells = data['tables'][0]['rows'][0]
        self.assertEqual([(cell['colspan'], cell['rowspan']) for cell in cells], [(1, 1), (1, 1)])

    def test_article_header_does_not_hide_networking_manual_specifications(self):
        body = b'''<header data-component="header" class="header"><h2>Site Navigation</h2></header>
          <main><article><header><h1>Specifications</h1></header>
          <h2>MCX545A-ECAN Specifications</h2><table><tr><th>Physical</th><th>Value</th></tr>
          <tr><td>Connector</td><td>Single QSFP28</td></tr></table></article></main>'''
        data = parse_page(body, 'https://networking-docs.nvidia.com/connectx5vpiocp2hw/specifications')
        self.assertEqual(data['heading'], 'Specifications')
        self.assertEqual(len(data['tables']), 1)
        self.assertIn('MCX545A-ECAN Specifications', data['tables'][0]['section'])
        self.assertEqual(data['tables'][0]['rows'][1][1]['text'], 'Single QSFP28')

    def test_networking_manual_models_are_imported_with_parent_and_source_receipt(self):
        import hashlib
        from fetchspec.product_catalog import networking_doc_products
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            url = 'https://networking-docs.nvidia.com/connectx5vpiocp2hw/specifications'
            body = b'''<main><article><header><h1>Specifications</h1></header>
              <h2>MCX545A-ECAN Specifications</h2><table><tr><th>Physical</th><th>Value</th></tr>
              <tr><td>Connector</td><td>Single QSFP28</td></tr></table>
              <h2>MCX545B-ECAN Specifications</h2><table><tr><th>Physical</th><th>Value</th></tr>
              <tr><td>Connector</td><td>Single QSFP28</td></tr></table></article></main>'''
            sha = hashlib.sha256(body).hexdigest()
            rel = f'ledger/companies/nvidia/snapshots/{sha[:2]}/{sha}.html'
            snapshot = root / rel
            snapshot.parent.mkdir(parents=True)
            snapshot.write_bytes(body)
            ledger = root / 'ledger/companies/nvidia/crawl.sqlite'
            ledger.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(ledger) as db:
                db.executescript('CREATE TABLE requests(id TEXT PRIMARY KEY,url TEXT); CREATE TABLE pages(request TEXT,sha TEXT,path TEXT,title TEXT,breadcrumbs TEXT,observed_at TEXT);')
                db.execute('INSERT INTO requests VALUES(?,?)', ('r1', url))
                db.execute('INSERT INTO pages VALUES(?,?,?,?,?,?)', ('r1', sha, rel,
                    'Specifications | NVIDIA ConnectX-5 InfiniBand/Ethernet Adapter Cards User Manual',
                    json.dumps(['Networking', 'Adapters']), '2026-09-28T00:00:00+00:00'))
            products, sources = networking_doc_products(ledger, root)
            self.assertEqual(len(products), 3)
            parent = next(p for p in products if p['kind'] == 'family_or_directory')
            child = next(p for p in products if p['kind'] == 'named_product')
            self.assertEqual(child['name'], 'NVIDIA ConnectX-5 MCX545A-ECAN')
            self.assertEqual(child['parent_id'], parent['id'])
            self.assertEqual(len(child['tables'][0]['rows']), 2)
            self.assertEqual(child['official_pages'][0]['sha256'], sha)
            self.assertEqual(child['tables'][0]['source_refs'][0]['url'], url)
            self.assertEqual(sources[0]['source_url'], url)

    def test_scope_and_classification_do_not_turn_family_into_shipping_product(self):
        self.assertEqual(entity_kind('NVIDIA HGX Platform', 'https://www.nvidia.com/en-us/data-center/hgx/'), 'family_or_directory')
        self.assertEqual(entity_kind('NVIDIA GB300 NVL72', ''), 'named_product')
        self.assertFalse(page_allowed('https://www.nvidia.com/de-de/data-center/h200/'))
        self.assertFalse(page_allowed('https://evil.example/en-us/h200/'))
        self.assertFalse(page_allowed('https://www.nvidia.com/en-us/about-nvidia/news/'))
        self.assertTrue(page_allowed('https://www.nvidia.cn/networking/products/data-processing-unit/'))
        self.assertTrue(page_allowed('https://resources.nvidia.com/en-us-accelerated-networking-resource-library/bluefield-4-dpu-datasheet'))
        self.assertFalse(page_allowed('https://resources.nvidia.com/en-us-accelerated-networking-resource-library/gated/bluefield-4-dpu-datasheet'))
        self.assertFalse(page_allowed('https://resources.nvidia.com/_pfcdn/assets/secret.pdf'))
        self.assertTrue(page_allowed('https://developer.nvidia.com/riva'))
        self.assertTrue(page_allowed('https://networking-docs.nvidia.com/software/lts-releases'))
        self.assertFalse(page_allowed('https://developer.nvidia.com/blog/unrelated/'))
        self.assertFalse(page_allowed('https://www.nvidia.com/fr-fr/geforce/'))
        self.assertEqual(sitemap_product_category('https://www.nvidia.cn/networking/products/data-processing-unit/'), 'Networking')
        self.assertEqual(sitemap_product_category('https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5090/'), 'Gaming and Creating')
        self.assertIsNone(sitemap_product_category('https://www.nvidia.cn/news/geforce-launch/'))
        self.assertEqual(website_page_identity('https://www.nvidia.com/en-us/geforce/rtx-5090/'),
                         website_page_identity('https://www.nvidia.cn/geforce/rtx-5090/'))

    def test_resource_landing_url_drops_tracking_query_but_direct_file_keeps_it(self):
        from fetchspec.product_catalog import normalized
        self.assertEqual(normalized('https://resources.nvidia.com/en-us-accelerated-networking-resource-library/bluefield-4-dpu-datasheet?xs=123', 'https://www.nvidia.com/'),
                         'https://resources.nvidia.com/en-us-accelerated-networking-resource-library/bluefield-4-dpu-datasheet')
        self.assertEqual(normalized('https://resources.nvidia.com/_pfcdn/assets/a.pdf?token=abc', 'https://www.nvidia.com/'),
                         'https://resources.nvidia.com/_pfcdn/assets/a.pdf?token=abc')

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

    def test_identical_models_share_identity_across_english_chinese_and_datasheet_labels(self):
        from fetchspec.product_catalog import product_identifier
        self.assertEqual(product_identifier('GeForce RTX 5090'), product_identifier('NVIDIA RTX 5090'))
        self.assertEqual(product_identifier('NVIDIA H200 GPU'), product_identifier('H200 Datasheet'))
        self.assertEqual(product_identifier('NVIDIA A10 Tensor Core GPU'),
                         product_identifier('NVIDIA A10 Tensor 核心 GPU'))
        self.assertEqual(product_identifier('NVIDIA Jetson AGX Orin 开发者套件'),
                         product_identifier('NVIDIA Jetson AGX Orin Developer Kit'))
        self.assertNotEqual(product_identifier('NVIDIA RTX PRO 6000 Blackwell Workstation Edition'),
                            product_identifier('NVIDIA RTX PRO 6000 Blackwell Server Edition'))

    def test_resource_viewer_iframe_is_retained_as_official_file_source(self):
        page = parse_page(b'<h1>NVIDIA BlueField-4 DPU</h1><iframe src="https://dam-cdn.nvd.orangelogic.com/AssetLink/abc.pdf" title="BlueField-4 DPU Datasheet"></iframe>',
                          'https://resources.nvidia.com/en-us-accelerated-networking-resource-library/bluefield-4-dpu-datasheet')
        self.assertIn({'url': 'https://dam-cdn.nvd.orangelogic.com/AssetLink/abc.pdf',
                       'label': 'BlueField-4 DPU Datasheet', 'section': 'NVIDIA BlueField-4 DPU',
                       'role': 'embedded_official_document'}, page['links'])

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
            db.execute('INSERT INTO frontier VALUES(?,?,?,?,?,?,?)', (rows[0]['url'], 1, '', '', '', 'failed', 'redirect or URL outside HTTPS host allowlist'))
            result = sync_product_sitemap(root, db)
            self.assertEqual(result['candidate_urls'], 1)
            self.assertEqual(result['new'], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM frontier').fetchone()[0], 1)
            self.assertIn('Networking', db.execute('SELECT category FROM frontier').fetchone()[0])
            self.assertEqual(db.execute('SELECT state FROM frontier').fetchone()[0], 'pending')

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


    def test_official_comparison_columns_become_model_records(self):
        from fetchspec.product_catalog import export, product_identifier, model_source_rank
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / 'product-catalog/nvidia'
            base.mkdir(parents=True)
            inventory = root / 'ledger/companies/nvidia/inventory'
            inventory.mkdir(parents=True)
            (inventory / 'latest-attempt.json').write_text(json.dumps({'status': 'sitemaps_complete',
                'run_id': 'fixture', 'finished_at': '2026-09-28T00:00:00+00:00',
                'unique_url_candidates': 1, 'sitemaps': []}))
            db = sqlite3.connect(base / 'discovery.sqlite3')
            db.row_factory = sqlite3.Row
            db.executescript('''CREATE TABLE pages(url TEXT PRIMARY KEY,sha TEXT,observed_at TEXT,payload TEXT);
              CREATE TABLE product_map(id TEXT PRIMARY KEY,name TEXT,parent_id TEXT,source_sha256 TEXT,source_url TEXT,kind TEXT,change_status TEXT,observed_at TEXT);
              CREATE TABLE product_map_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,product_id TEXT,observed_at TEXT,status TEXT,source_sha256 TEXT,payload TEXT);
              CREATE TABLE frontier(url TEXT PRIMARY KEY,depth INTEGER,parent TEXT,category TEXT,label TEXT,state TEXT,error TEXT);
              CREATE TABLE memberships(url TEXT,parent TEXT,category TEXT,label TEXT);
              CREATE TABLE product_sitemap_urls(url TEXT PRIMARY KEY,category TEXT,lastmod TEXT,role_data TEXT,state TEXT,observed_at TEXT);''')
            url = 'https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5070-family/'
            sha = 'a' * 64
            cell = lambda text: {'text': text, 'colspan': 1, 'rowspan': 1, 'header': False}
            rows = [[cell(''), cell(''), cell('GeForce RTX 5070 Ti'), cell('GeForce RTX 5070')],
                    [cell('GPU Engine Specs:'), cell('NVIDIA CUDA Cores'), cell('8960'), cell('6144')],
                    [cell('Memory Specs:'), cell('Standard Memory Config'), cell('16 GB GDDR7'), cell('12 GB GDDR7')]]
            table = {'index': 1, 'section': 'GeForce RTX 5070 Family', 'rows': rows,
                     'is_specification': True, 'notes': 'Official footnote', 'method': 'html_table'}
            compare_rows = [[cell(''), cell('GeForce RTX 5070 Ti'), cell('GeForce RTX 5070')],
                [cell('GPU Engine Specs:'), cell(''), cell('')],
                [cell('NVIDIA CUDA Cores'), cell('8960'), cell('6144')],
                [cell('Standard Memory Config'), cell('16 GB GDDR7'), cell('12 GB GDDR7')]]
            compare = {'index': 2, 'section': 'Compare 50 Series Specs', 'rows': compare_rows,
                       'is_specification': True, 'notes': 'Official footnote', 'method': 'html_table'}
            page = {'heading': 'GeForce RTX 5070 Family', 'title': 'Family', 'canonical': url,
                    'links': [], 'tables': [table, compare], 'text': '', 'source_url': url,
                    'requested_url': url, 'sha256': sha, 'snapshot_path': 'blobs/aa/a.html',
                    'observed_at': '2026-09-28T00:00:00+00:00', 'category': 'Gaming and Creating',
                    'parent_url': '', 'depth': 1, 'kind': 'family_or_directory',
                    'http': {'status': 200, 'final_url': url, 'content_type': 'text/html'}}
            db.execute('INSERT INTO pages VALUES(?,?,?,?)', (url, sha, page['observed_at'], json.dumps(page)))
            coverage = export(db, base)
            products = json.loads((base / 'catalog.json').read_text())['products']
            self.assertEqual(coverage['entity_counts']['named_product'], 2)
            self.assertGreater(model_source_rank(url), model_source_rank('https://www.nvidia.com/en-us/geforce/graphics-cards/compare/'))
            for model, cuda, memory in [('NVIDIA GeForce RTX 5070 Ti', '8960', '16 GB GDDR7'),
                                        ('NVIDIA GeForce RTX 5070', '6144', '12 GB GDDR7')]:
                product = next(p for p in products if p['name'] == model)
                self.assertEqual(product['id'], product_identifier(model))
                values = {row[0]['text']: row[1]['text'] for row in product['tables'][0]['rows']}
                self.assertEqual(values.get('NVIDIA CUDA Cores'), cuda, repr(values))
                self.assertEqual(values.get('Standard Memory Config'), memory, repr(values))
                self.assertIn('Official footnote', product['tables'][0]['notes'])
                self.assertEqual(len(product['tables']), 1)


if __name__ == '__main__':
    unittest.main()
