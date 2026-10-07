import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fetchspec.adapters import adapter_for
from fetchspec.legacy_catalog import identity
from fetchspec.ownership import audit, conflict, support_context
from fetchspec.products import ProductStore


class OwnershipTests(unittest.TestCase):
    def test_all_supported_storage_brands_are_not_supermicro_products(self):
        adapter = adapter_for('supermicro')
        for brand in ('kioxia', 'samsung', 'intel', 'hgst', 'micron', 'unknown-vendor'):
            for locale, suffix in (('en', ''), ('zh-cn', ''), ('zh_tw', '.cfm'), ('', '.cfm')):
                url = 'https://www.supermicro.com/' + (locale + '/' if locale else '') + 'products/storage/pci-e/' + brand + suffix
                with self.subTest(url=url):
                    page = adapter.parse(('<title>'+brand+' NVMe | Supermicro</title><h2>Supermicro Servers Support PCI-E SSD Solutions</h2>').encode(), url)
                    self.assertIsNone(adapter.identity(page, url))
                    self.assertIsNone(identity(page, url, adapter))
                    self.assertIsNotNone(support_context('supermicro', url))

    def test_own_server_components_and_own_nvme_directory_remain(self):
        adapter = adapter_for('supermicro')
        url = 'https://www.supermicro.com/en/products/system/storage/ssg-121e-nes24r'
        page = adapter.parse(b'<h1>SSG-121E-NES24R</h1><h2>Specifications</h2><table><tr><th>Drive</th><td>Kioxia SSD; Samsung DDR5; NVIDIA GPU</td></tr></table>', url)
        product = {**identity(page, url, adapter), 'source_url': url, 'tables': page['tables']}
        self.assertEqual(product['name'], 'SSG-121E-NES24R')
        self.assertIsNone(conflict(product, 'supermicro'))
        self.assertEqual(audit([product], 'supermicro')['issues'], [])
        self.assertIsNone(support_context('supermicro', 'https://www.supermicro.com/en/products/nvme'))
        self.assertIsNotNone(support_context('supermicro', 'https://www.supermicro.com/en/products/nvme/vroc'))

    def test_other_companies_review_names_without_scanning_component_cells(self):
        for company in ('intel', 'amd', 'nvidia', 'micron', 'vertiv', 'delta', 'asteralabs', 'siemens', 'siemens-energy', 'biren', 'loongson'):
            with self.subTest(company=company):
                self.assertEqual(audit([{'id':'p', 'name':'Samsung PM1743'}], company)['review_required'], 1)
        self.assertEqual(audit([{'name':'Samsung PM1743'}], 'samsung')['issues'], [])
        self.assertEqual(audit([{'name':'Siemens Energy SGT-800'}], 'siemens-energy')['issues'], [])
        self.assertEqual(audit([{'name':'Siemens Energy SGT-800'}], 'siemens')['review_required'], 1)
        self.assertEqual(audit([{'name':'NVIDIA H100', 'tables':[{'text':'Intel CPU Samsung DDR5'}]}], 'nvidia')['issues'], [])

    def test_store_rejects_new_misattribution_and_filters_legacy_export_without_erasing_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = b'<h1>Samsung NVMe SSDs</h1>'
            sha = hashlib.sha256(body).hexdigest()
            (root/'source.html').write_bytes(body)
            url = 'https://www.supermicro.com/en/products/storage/pci-e/samsung'
            p = {'id':'supermicro-test', 'name':'Samsung NVMe', 'source_url':url, 'source_sha256':sha,
                 'observed_at':'2026-09-23T00:00:00+00:00', 'kind':'family_or_directory', 'tables':[]}
            data = {'company_id':'supermicro', 'products':[p], 'sources':[{'sha256':sha, 'source_url':url,
                    'observed_at':p['observed_at'], 'snapshot_path':'source.html'}]}
            with ProductStore(root/'store') as store:
                with self.assertRaisesRegex(ValueError, 'ownership conflict'):
                    store.ingest_catalog(data, root)
                self.assertEqual(store.stats()['products'], 0)
                # Simulate an already imported archive under the previous policy.
                store.db.execute('INSERT INTO product_versions VALUES(?,?,?,?,?)', ('supermicro',p['id'],'old',p['observed_at'],json.dumps(p)))
                store.db.execute('INSERT INTO products VALUES(?,?,?,?,?)', ('supermicro',p['id'],'old',p['observed_at'],json.dumps(p)))
                child = copy.deepcopy(p)
                child.update(id='supermicro-child', name='SYS-222H-TN', parent_id=p['id'], source_url='https://www.supermicro.com/en/products/system/sys-222h-tn')
                store.db.execute('INSERT INTO product_versions VALUES(?,?,?,?,?)', ('supermicro',child['id'],'old',child['observed_at'],json.dumps(child)))
                store.db.execute('INSERT INTO products VALUES(?,?,?,?,?)', ('supermicro',child['id'],'old',child['observed_at'],json.dumps(child)))
                store.db.commit()
                result = store.export_catalog('supermicro')
                self.assertEqual([x['id'] for x in result['products']], [child['id']])
                self.assertIsNone(result['products'][0]['parent_id'])
                self.assertEqual(result['ownership_audit']['conflicts'], 1)
                self.assertEqual(store.stats()['product_versions'], 2)
                self.assertEqual(store.stats()['products'], 2)


if __name__ == '__main__':
    unittest.main()
