import hashlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
import zipfile

from fetchspec.acquisition import collect
from fetchspec.adapters import adapter_for
from fetchspec.extraction import extract_document, html_page

BASE = 'https://www.supermicro.com/en/products/system/sys-222h-tn'
PDF = 'https://www.supermicro.com/manuals/sys-222h-spec.pdf'
HTML = b'<h1>SYS-222H-TN</h1><h2>Technical Specifications</h2><table><tr><th>Memory</th><td>Up to 4 TB*</td></tr><tr><th>Power</th><td>1200 W</td></tr></table><p>* configuration dependent</p>'


class Fetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.prepared = 0

    def prepare_robots(self):
        self.prepared += 1
        return {}

    def get(self, url, *, headers=None):
        self.calls.append((url, headers or {}))
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            return value
        return value, {'status': 200, 'content_type': 'text/html', 'final_url': url, 'etag': 'v1'}


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_html_wins_preserves_notes_and_rerun_needs_no_request(self):
        body = HTML + ('<a href="'+PDF+'">Datasheet</a>').encode()
        first = collect(self.root, 'supermicro', [BASE], fetcher=Fetcher({BASE: body}))
        product = first['products'][0]
        self.assertEqual(product['name'], 'SYS-222H-TN')
        self.assertEqual(product['tables'][0]['notes'], '* configuration dependent')
        self.assertEqual(product['tables'][0]['rows'][0][1]['text'], 'Up to 4 TB*')
        self.assertEqual(len(first['sources']), 1)
        second_fetcher = Fetcher({})
        second = collect(self.root, 'supermicro', [BASE], fetcher=second_fetcher)
        self.assertEqual(first['products'], second['products'])
        self.assertEqual(second_fetcher.calls, [])
        self.assertEqual(second_fetcher.prepared, 0)

    def test_reparse_uses_verified_snapshot_without_any_http(self):
        first = collect(self.root, 'supermicro', [BASE], fetcher=Fetcher({BASE: HTML}))
        fetcher = Fetcher({})
        second = collect(self.root, 'supermicro', [BASE], fetcher=fetcher, reparse=True)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(fetcher.prepared, 0)
        self.assertEqual(second['acquisition_report']['reparsed'], 1)
        self.assertEqual(first['products'], second['products'])
        self.assertEqual(first['sources'], second['sources'])

    def test_304_validates_snapshot_and_preserves_one_version(self):
        collect(self.root, 'supermicro', [BASE], fetcher=Fetcher({BASE: HTML}))
        fetcher = Fetcher({BASE: HTTPError(BASE, 304, 'Not Modified', {}, None)})
        result = collect(self.root, 'supermicro', [BASE], fetcher=fetcher, refresh=True)
        self.assertEqual(fetcher.calls[0][1]['If-None-Match'], 'v1')
        self.assertEqual(result['acquisition_report']['unchanged'], 1)
        self.assertEqual(len(result['sources']), 1)
        blob = self.root / result['sources'][0]['snapshot_path']
        blob.write_bytes(b'corrupt')
        failed = collect(self.root, 'supermicro', [BASE], fetcher=fetcher, refresh=True)
        self.assertIn('SHA mismatch', failed['acquisition_report']['errors'][0]['error'])

    def test_same_url_new_version_retains_bytes_and_supersedes(self):
        first = collect(self.root, 'supermicro', [BASE], fetcher=Fetcher({BASE: HTML}))
        changed = HTML.replace(b'1200 W', b'1500 W')
        result = collect(self.root, 'supermicro', [BASE], fetcher=Fetcher({BASE: changed}), refresh=True)
        self.assertEqual(len(result['sources']), 2)
        self.assertEqual(len(list((self.root/'blobs').glob('*/*'))), 2)
        current = next(s for s in result['sources'] if s['sha256'] == hashlib.sha256(changed).hexdigest())
        self.assertEqual(current['supersedes_sha256'], first['sources'][0]['sha256'])
        self.assertEqual(result['products'][0]['tables'][0]['rows'][1][1]['text'], '1500 W')

    def test_same_bytes_another_url_keeps_source_observation_and_one_blob(self):
        other = BASE + '/specifications'
        collect(self.root, 'supermicro', [BASE], fetcher=Fetcher({BASE: HTML}))
        result = collect(self.root, 'supermicro', [BASE, other], fetcher=Fetcher({other: HTML}))
        self.assertEqual(len(result['sources']), 2)
        self.assertEqual(len(list((self.root/'blobs').glob('*/*'))), 1)
        self.assertEqual(len(result['products']), 1)

    def test_html_spec_link_wins_over_pdf(self):
        spec = BASE + '/specifications'
        body = f'<h1>SYS-222H-TN</h1><a href="{spec}">Specifications</a><a href="{PDF}">Datasheet PDF</a>'.encode()
        fetcher = Fetcher({BASE: body, spec: HTML})
        result = collect(self.root, 'supermicro', [BASE], fetcher=fetcher)
        self.assertEqual([r[0] for r in fetcher.calls], [BASE, spec])
        self.assertTrue(result['products'][0]['tables'])
        self.assertEqual(next(r for r in result['frontier'] if r['url']==PDF)['state'], 'deferred_better_native_source')

    def test_pdf_fallback_records_native_method(self):
        body = f'<h1>SYS-222H-TN</h1><a href="{PDF}">Datasheet PDF</a>'.encode()
        fetcher = Fetcher({BASE: body, PDF: (b'%PDF-1.7\n%%EOF', {'content_type': 'application/pdf', 'status': 200, 'final_url': PDF})})
        table = html_page(HTML, BASE)['tables'][0]
        with patch('fetchspec.extraction.pdf_spec_tables', return_value=[table]):
            result = collect(self.root, 'supermicro', [BASE], fetcher=fetcher)
        self.assertEqual(len(result['sources']), 2)
        self.assertEqual(result['products'][0]['tables'][0]['source_refs'][0]['url'], PDF)
        self.assertEqual(result['products'][0]['extraction_status'], 'native_tables_extracted')

    def test_directory_budget_resumes_and_scope_does_not_leak(self):
        directory = 'https://www.supermicro.com/en/products/gpu'
        unrelated = 'https://www.supermicro.com/en/products/system/sys-999-test'
        body = f'<h1>GPU Servers</h1><a href="{BASE}">SYS-222H-TN</a><a href="/en/pressreleases/latest">News</a>'.encode()
        first = collect(self.root, 'supermicro', [directory], fetcher=Fetcher({directory: body}), max_pages=1)
        self.assertEqual(first['acquisition_report']['pending'], 1)
        unrelated_result = collect(self.root, 'supermicro', [unrelated], fetcher=Fetcher({unrelated: HTML.replace(b'SYS-222H-TN', b'SYS-999-TEST')}))
        self.assertEqual(len(unrelated_result['products']), 1)
        resumed = collect(self.root, 'supermicro', [directory], fetcher=Fetcher({BASE: HTML}))
        self.assertEqual(len(resumed['products']), 2)
        self.assertEqual(resumed['acquisition_report']['pending'], 0)
        child = next(p for p in resumed['products'] if p['kind']=='named_product')
        self.assertEqual(child['parent_id'], next(p['id'] for p in resumed['products'] if p['kind']=='family_or_directory'))

    def test_language_rejects_foreign_seed_and_filtered_link(self):
        adapter = adapter_for('supermicro')
        self.assertIsNone(adapter.normalize('https://www.supermicro.com/ja-jp/products/gpu'))
        self.assertIsNotNone(adapter.normalize('https://www.supermicro.com/zh-cn/products/gpu'))
        with self.assertRaises(ValueError):
            collect(self.root, 'supermicro', ['https://www.supermicro.com/de-de/products/gpu'], fetcher=Fetcher({}))

    def test_transient_error_resumes_without_losing_queue(self):
        with patch('fetchspec.acquisition.time.sleep'):
            first = collect(self.root, 'supermicro', [BASE], fetcher=Fetcher({BASE: URLError('Connection reset')}))
        self.assertEqual(first['frontier'][0]['state'], 'retryable_error')
        second = collect(self.root, 'supermicro', [BASE], fetcher=Fetcher({BASE: HTML}))
        self.assertEqual(second['frontier'][0]['state'], 'done')
        db = sqlite3.connect(self.root/'acquisition/supermicro/sources.sqlite3')
        self.assertEqual(db.execute('SELECT count(*) FROM errors').fetchone()[0], 1)
        db.close()

    def test_nvidia_reuses_existing_model_identity(self):
        url = 'https://www.nvidia.com/en-us/data-center/h100/'
        known = {'products': [{'id': 'nvidia-original-model', 'name': 'NVIDIA H100', 'kind': 'named_product', 'parent_id': 'existing-parent', 'product_url': url, 'source_url': url}]}
        body = HTML.replace(b'SYS-222H-TN', b'NVIDIA H100')
        result = collect(self.root, 'nvidia', [url], fetcher=Fetcher({url: body}), known_catalog=known)
        self.assertEqual(result['products'][0]['id'], 'nvidia-original-model')
        self.assertEqual(result['products'][0]['parent_id'], 'existing-parent')

    def test_explicit_product_seed_promotes_inferred_specification_owner(self):
        parent = 'https://www.supermicro.com/en/products/system/sys-111-parent'
        parent_body = f'<h1>SYS-111-PARENT</h1><a href="{BASE}">Specifications</a>'.encode()
        first = collect(self.root, 'supermicro', [parent], fetcher=Fetcher({parent: parent_body, BASE: HTML}))
        self.assertEqual(len(first['products']), 1)
        original_parent = first['products'][0]
        fetcher = Fetcher({})
        promoted = collect(self.root, 'supermicro', [BASE], fetcher=fetcher)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(len(promoted['products']), 1)
        self.assertEqual(promoted['products'][0]['name'], 'SYS-222H-TN')
        self.assertNotEqual(promoted['products'][0]['id'], original_parent['id'])
        self.assertIsNone(promoted['products'][0]['parent_id'])
        db = sqlite3.connect(self.root/'acquisition/supermicro/sources.sqlite3')
        import json
        self.assertEqual(json.loads(db.execute('SELECT payload FROM products WHERE id=?', (original_parent['id'],)).fetchone()[0]), original_parent)
        self.assertEqual(db.execute('SELECT product_id FROM source_products WHERE url=?', (BASE,)).fetchall(), [(promoted['products'][0]['id'],)])
        self.assertEqual(db.execute('SELECT count(*) FROM source_owner_history').fetchone()[0], 1)
        db.close()

    def test_nested_layout_tables_do_not_duplicate_values(self):
        body = b'<h1>SYS-222H-TN</h1><table class="spec-table-1"><tr><td><table><tr><td colspan="2">Processor</td></tr><tr><td>CPU</td><td>Dual Xeon</td></tr></table><table><tr><td colspan="2">GPU</td></tr><tr><td>Count</td><td>8</td></tr></table></td></tr></table>'
        page = adapter_for('supermicro').parse(body, BASE)
        self.assertEqual([t['section'] for t in page['tables']], ['Processor', 'GPU'])
        self.assertEqual(sum(c['text']=='Dual Xeon' for t in page['tables'] for r in t['rows'] for c in r), 1)

    def test_known_pdf_table_survives_html_refresh_with_evidence(self):
        url = 'https://www.nvidia.com/en-us/data-center/h100/'
        pdf_url = 'https://images.nvidia.com/h100.pdf'
        old_html, old_pdf = b'<h1>NVIDIA H100</h1>', b'%PDF-1.7 original %%EOF'
        sources = []
        for source_url, body, kind in [(url, old_html, 'html'), (pdf_url, old_pdf, 'pdf')]:
            sha = hashlib.sha256(body).hexdigest()
            rel = f'blobs/{sha[:2]}/{sha}'
            path = self.root/rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            sources.append({'source_url': source_url, 'sha256': sha, 'snapshot_path': rel,
                            'observed_at': '2026-09-29T00:00:00Z', 'format': kind})
        old_table = {'section': 'PDF Specifications', 'rows': [[{'text': 'GPU memory'}, {'text': '80 GB'}]],
                     'extraction_method': 'pdftotext_layout', 'source_refs': [{'url': pdf_url, 'sha256': sources[1]['sha256']}]}
        known = {'products': [{'id': 'nvidia-preserved', 'name': 'NVIDIA H100', 'kind': 'named_product',
                              'product_url': url, 'source_url': url, 'source_sha256': sources[0]['sha256'],
                              'observed_at': sources[0]['observed_at'], 'tables': [old_table]}], 'sources': sources}
        result = collect(self.root, 'nvidia', [url], fetcher=Fetcher({url: HTML.replace(b'SYS-222H-TN', b'NVIDIA H100')}), known_catalog=known)
        self.assertIn(old_table, result['products'][0]['tables'])
        self.assertEqual(len(result['products'][0]['tables']), 2)
        self.assertIn(sources[1], result['sources'])

    def test_ambiguous_nvidia_comparison_page_never_overwrites_first_model(self):
        url = 'https://www.nvidia.com/en-us/data-center/h100/'
        known = {'products': [{'id': str(i), 'name': name, 'kind': 'named_product', 'source_url': url}
                               for i, name in enumerate(['NVIDIA H100 PCIe', 'NVIDIA H100 SXM'])]}
        result = collect(self.root, 'nvidia', [url], fetcher=Fetcher({url: HTML.replace(b'SYS-222H-TN', b'NVIDIA H100')}), known_catalog=known)
        self.assertEqual(result['products'], [])
        self.assertIn('ambiguous', result['acquisition_report']['errors'][0]['error'])

    def test_docx_native_cells_and_legacy_office_pending(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Memory 4 TB*</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')
        result = extract_document(buf.getvalue(), 'docx')
        value = result['tables'][0]['rows'][0][0]
        self.assertEqual(value['text'], 'Memory 4 TB*')
        self.assertEqual(value['colspan'], 2)
        legacy = extract_document(b'legacy Office', 'xls')
        self.assertEqual(legacy['status'], 'review_required_no_native_specification')
        self.assertFalse(legacy['tables'])

    def test_xlsx_preserves_coordinates_formula_and_merged_ranges(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as archive:
            archive.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Official Power (W)" sheetId="1" r:id="rId1"/></sheets></workbook>')
            archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
            archive.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Power</t></is></c><c r="C1"><f>600*2</f><v>1200</v></c></row></sheetData><mergeCells><mergeCell ref="A1:B1"/></mergeCells></worksheet>')
        table = extract_document(buf.getvalue(), 'xlsx')['tables'][0]
        self.assertEqual(table['section'], 'Official Power (W)')
        self.assertTrue(table['requires_review'])
        self.assertEqual(table['merged_ranges'], ['A1:B1'])
        self.assertEqual(table['rows'][0][1]['coordinate'], 'C1')
        self.assertEqual(table['rows'][0][1]['formula'], '600*2')
        self.assertEqual(table['rows'][0][1]['text'], '1200')

    def test_pptx_native_table_preserves_slide_title_and_cell(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as archive:
            archive.writestr('ppt/presentation.xml', '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>')
            archive.writestr('ppt/slides/slide1.xml', '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>GPU Specifications</a:t></a:r></a:p></p:txBody></p:sp><a:tbl><a:tr><a:tc gridSpan="2"><a:txBody><a:p><a:r><a:t>Memory: 80 GB</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl></p:sld>')
        result = extract_document(buf.getvalue(), 'pptx')
        table = result['tables'][0]
        self.assertEqual(table['section'], 'GPU Specifications')
        self.assertEqual(table['slide_number'], 1)
        self.assertEqual(table['rows'][0][0]['text'], 'Memory: 80 GB')
        self.assertEqual(table['rows'][0][0]['colspan'], 2)
        self.assertTrue(result['uncertainty'])


if __name__ == '__main__':
    unittest.main()
