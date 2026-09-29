import copy
import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fetchspec.products import ProductStore


class ProductStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.archive = self.base / 'archive'
        self.archive.mkdir()
        self.store = ProductStore(self.base / 'new')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def catalog(self, body=b'<h1>GPU</h1>', at='2026-09-01T00:00:00+00:00', url='https://www.nvidia.com/en-us/test/'):
        sha = hashlib.sha256(body).hexdigest()
        path = self.archive / (sha + '.html')
        path.write_bytes(body)
        return {'company_id': 'nvidia', 'sources': [{'source_url': url, 'sha256': sha, 'snapshot_path': path.name, 'observed_at': at}],
                'products': [{'id': 'nvidia-gpu', 'name': 'GPU', 'kind': 'named_product', 'parent_id': None,
                              'source_url': url, 'source_sha256': sha, 'observed_at': at,
                              'tables': [{'index': 3, 'section': 'Specifications', 'notes': 'Maximum at 25°C',
                                          'rows': [[{'text': 'Power', 'rowspan': 2, 'colspan': 1, 'header': True},
                                                    {'text': '700 W', 'rowspan': 1, 'colspan': 1, 'header': False}]]}]}]}

    def test_repeat_preserves_byte_table_and_cell_counts(self):
        payload = self.catalog()
        self.assertEqual(self.store.ingest_catalog(payload, self.archive)['new_products'], 1)
        first = self.store.stats()
        self.assertEqual(self.store.ingest_catalog(payload, self.archive)['unchanged_products'], 1)
        self.assertEqual(self.store.stats(), first)
        self.assertEqual(self.store.export_catalog('nvidia')['products'], payload['products'])
        self.assertEqual(first['spec_cells'], 2)

    def test_filename_and_url_change_same_bytes_preserves_observation(self):
        old = self.catalog()
        self.store.ingest_catalog(old, self.archive)
        new = self.catalog(url='https://www.nvidia.cn/test/', at='2026-09-02T00:00:00+00:00')
        self.store.ingest_catalog(new, self.archive)
        self.assertEqual(self.store.stats()['blobs'], 1)
        self.assertEqual(self.store.stats()['sources'], 2)
        self.assertEqual(self.store.stats()['product_versions'], 2)

    def test_auxiliary_table_change_is_product_change(self):
        old = self.catalog()
        self.store.ingest_catalog(old, self.archive)
        revised = copy.deepcopy(old)
        revised['products'][0]['observed_at'] = '2026-09-02T00:00:00+00:00'
        revised['products'][0]['tables'][0]['notes'] = 'Typical at 25°C'
        self.assertEqual(self.store.ingest_catalog(revised, self.archive)['changed_products'], 1)
        self.assertEqual(self.store.stats()['product_versions'], 2)
        self.assertEqual(self.store.stats()['spec_tables'], 2)

    def test_same_url_new_bytes_and_stale_batch_never_rolls_back(self):
        old = self.catalog()
        self.store.ingest_catalog(old, self.archive)
        new = self.catalog(b'<h1>GPU v2</h1>', '2026-09-02T00:00:00+00:00')
        self.store.ingest_catalog(new, self.archive)
        self.assertEqual(self.store.ingest_catalog(old, self.archive)['stale_products'], 1)
        self.assertEqual(self.store.stats()['blobs'], 2)
        self.assertEqual(self.store.export_catalog('nvidia')['products'], new['products'])

    def test_new_model_partial_catalog_retains_existing_product(self):
        old = self.catalog()
        self.store.ingest_catalog(old, self.archive)
        new = self.catalog(at='2026-09-02T00:00:00+00:00')
        new['products'][0].update(id='nvidia-next', parent_id='nvidia-gpu')
        self.store.ingest_catalog(new, self.archive)
        self.assertEqual(self.store.stats()['products'], 2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM relations').fetchone()[0], 1)

    def test_corrupt_source_rejected_before_projection_mutation(self):
        payload = self.catalog()
        (self.archive / payload['sources'][0]['snapshot_path']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'SHA mismatch'):
            self.store.ingest_catalog(payload, self.archive)
        self.assertEqual(self.store.stats()['products'], 0)
        self.assertEqual(self.store.stats()['blobs'], 0)

    def test_conflicting_same_timestamp_rolls_back_entire_batch(self):
        payload = self.catalog()
        self.store.ingest_catalog(payload, self.archive)
        conflicting = copy.deepcopy(payload)
        conflicting['products'][0]['name'] = 'different identity'
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            self.store.ingest_catalog(conflicting, self.archive)
        self.assertEqual(self.store.stats()['product_versions'], 1)
        self.assertEqual(self.store.export_catalog('nvidia')['products'], payload['products'])

    def test_path_escape_and_missing_parent_rejected(self):
        payload = self.catalog()
        payload['sources'][0]['snapshot_path'] = '../not-archive.html'
        with self.assertRaisesRegex(ValueError, 'outside archive'):
            self.store.ingest_catalog(payload, self.archive)
        payload = self.catalog()
        payload['products'][0]['parent_id'] = 'invented'
        with self.assertRaisesRegex(ValueError, 'unknown parent'):
            self.store.ingest_catalog(payload, self.archive)

    def test_csv_keeps_original_cell_spans_notes_and_escapes_formula(self):
        payload = self.catalog()
        payload['products'][0]['tables'][0]['rows'][0][1]['text'] = '=HYPERLINK("url")'
        self.store.ingest_catalog(payload, self.archive)
        report = self.store.export_csv(self.base / 'csv')
        self.assertEqual(report['cells'], 2)
        with (self.base / 'csv/spec_cells.csv').open(encoding='utf-8-sig') as stream:
            cells = list(csv.DictReader(stream))
        self.assertEqual(cells[0]['rowspan'], '2')
        self.assertEqual(cells[0]['notes'], 'Maximum at 25°C')
        self.assertTrue(cells[1]['original_text'].startswith("'="))
        self.assertEqual(self.store.export_catalog('nvidia')['products'][0]['tables'][0]['rows'][0][1]['text'], '=HYPERLINK("url")')


    def test_comparison_mapping_is_versioned_and_does_not_change_original(self):
        payload = self.catalog()
        self.store.ingest_catalog(payload, self.archive)
        before = self.store.export_catalog('nvidia')['products']
        result = self.store.map_field('nvidia','nvidia-gpu',1,1,2,'gpu.tdp.max','W','maximum at 25C','test reviewer')
        self.assertEqual(result['original_text'], '700 W')
        self.assertEqual(before,self.store.export_catalog('nvidia')['products'])
        with self.assertRaisesRegex(ValueError, 'existing current'):
            self.store.map_field('nvidia','nvidia-gpu',9,1,2,'gpu.tdp.max','W','','test')
        revised = self.catalog(b'<h1>revised</h1>', '2026-09-02T00:00:00+00:00')
        revised['products'][0]['tables'][0]['rows'][0][1]['text'] = '800 W'
        self.store.ingest_catalog(revised,self.archive)
        self.store.export_csv(self.base / 'csv')
        with (self.base / 'csv/comparisons.csv').open(encoding='utf-8-sig') as f:
            records = list(csv.DictReader(f))
        self.assertEqual(records[0]['original_text'], '700 W')
        self.assertEqual(records[0]['version_status'], 'historical')

    def test_invalid_json_shape_and_parent_cycle_fail_before_commit(self):
        for invalid in ([], {'company_id':'nvidia','products':[None],'sources':[]}):
            with self.assertRaises(ValueError): self.store.ingest_catalog(invalid,self.archive)
        payload=self.catalog(); payload['products'][0]['parent_id']='nvidia-gpu'
        with self.assertRaisesRegex(ValueError,'cyclic'):
            self.store.ingest_catalog(payload,self.archive)
        self.assertEqual(self.store.stats()['products'],0)


if __name__ == '__main__':
    unittest.main()
