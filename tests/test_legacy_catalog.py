import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from fetchspec.legacy_catalog import export


def fixture(root):
    directory = root/'ledger/companies/supermicro'
    directory.mkdir(parents=True)
    db = sqlite3.connect(directory/'crawl.sqlite')
    db.executescript('''
      CREATE TABLE requests(id TEXT PRIMARY KEY,url TEXT,method TEXT,latest_sha TEXT);
      CREATE TABLE pages(request TEXT,sha TEXT,path TEXT,title TEXT,observed_at TEXT);
      CREATE TABLE observations(observed_at TEXT,sha TEXT,request TEXT);
      CREATE TABLE blobs(sha TEXT,kind TEXT,bytes INTEGER,path TEXT);
      CREATE TABLE edges(parent TEXT,child TEXT,label TEXT,first_seen TEXT);
    ''')
    urls = ['https://www.supermicro.com/en/products/system/3u/6039/sys-6039p-txrt.php',
            'https://www.supermicro.com/en/products/system/2u/6029/sys-6029uz-tr4_.php',
            'https://www.supermicro.com/en/products/system/2u/6129/ssg-6129p-acr12n4l.php']
    for i,(url,title) in enumerate(zip(urls,['6039P-TXRT','SYS-6029UZ-TR4+','SSG-6129P-ACR12N4L'])):
        body=('<title>'+title+'</title><h1>'+title+'</h1><h2>Specifications</h2><table><tr><th>Memory</th><td>TEST_VALUE '+str(i)+'</td></tr></table>').encode()
        sha=hashlib.sha256(body).hexdigest(); path='ledger/companies/supermicro/snapshots/'+sha+'.html'
        (root/path).parent.mkdir(parents=True,exist_ok=True); (root/path).write_bytes(body)
        db.execute('INSERT INTO requests VALUES(?,?,?,?)',(str(i),url,'GET',sha))
        db.execute('INSERT INTO pages VALUES(?,?,?,?,?)',(str(i),sha,path,title,'2026-09-23T17:55:00+00:00'))
    for i,(url,label,parent) in enumerate([
        ('https://www.supermicro.com/products/powersupply/80PLUS/80PLUS_PWS-1K05A-1R.pdf','Test Report','0'),
        ('https://www.supermicro.com/manuals/other/unassigned.pdf','Manual',None)]):
        sha=('a' if i==0 else 'b')*64
        db.execute('INSERT INTO blobs VALUES(?,?,?,?)',(sha,'pdf',1000,'missing-pdf-intentionally-not-read.pdf'))
        db.execute('INSERT INTO requests VALUES(?,?,?,?)',('doc'+str(i),url,'GET',sha))
        if parent:
            db.execute('INSERT INTO edges VALUES(?,?,?,?)',(parent,'doc'+str(i),label,'2026-09-23T17:55:00+00:00'))
    db.execute('INSERT INTO observations VALUES(?,?,?)',('2026-09-23T17:55:06+00:00',None,None))
    db.commit();db.close()
    return directory/'crawl.sqlite'


class LegacyCatalogTests(unittest.TestCase):
    def test_offline_hash_verified_models_native_cells_and_document_links(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'legacy'; out=Path(temp)/'output'; ledger=fixture(root)
            before=ledger.read_bytes()
            with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('offline only')):
                report=export(root,out)
            self.assertEqual(ledger.read_bytes(),before)
            self.assertEqual((report['named_products'],report['specification_tables'],report['indexed_documents'],report['unassigned_documents']),(3,3,2,1))
            manifest=json.loads((out/'manifest.json').read_text())
            payload=json.loads((out/manifest['batches'][0]['path']).read_text())
            products={p['name']:p for p in payload['products']}
            self.assertEqual(set(products),{'SYS-6039P-TXRT','SYS-6029UZ-TR4+','SSG-6129P-ACR12N4L'})
            old=products['SYS-6039P-TXRT']
            self.assertEqual(old['observed_at'],'2026-09-23T17:55:00+00:00')
            self.assertEqual(old['tables'][0]['rows'][0][1]['text'],'TEST_VALUE 0')
            self.assertEqual(old['attachments'][0]['label'],'Test Report')
            self.assertNotIn('sha256',old['attachments'][0])
            with tarfile.open(report['bundle']) as tar:
                self.assertFalse(any(n.endswith('.pdf') for n in tar.getnames()))
            self.assertEqual(report['pdf_bytes_transferred'],0)

    def test_corrupt_html_fails_before_bundle_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'legacy'; out=Path(temp)/'out';fixture(root)
            next((root/'ledger/companies/supermicro/snapshots').glob('*.html')).write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'mismatch'):
                export(root,out)
            self.assertFalse(out.exists())

    @unittest.skipUnless(os.environ.get('FETCHSPEC_LEGACY_INRESEARCH_ROOT'),'set FETCHSPEC_LEGACY_INRESEARCH_ROOT for supplement receiver integration')
    def test_real_bundle_receiver_preserves_current_and_exposes_all_materials(self):
        if not os.environ.get('FETCHSPEC_LEGACY_TEST_PROCESS'):
            # Other integrations deliberately import the deployed main authority.
            # Isolate this candidate receiver so their Python module cache cannot
            # silently select the wrong implementation (or weaken target guards).
            code="import sys,unittest;sys.path.insert(0,'tests');suite=unittest.defaultTestLoader.loadTestsFromName('test_legacy_catalog.LegacyCatalogTests.test_real_bundle_receiver_preserves_current_and_exposes_all_materials');r=unittest.TextTestRunner().run(suite);raise SystemExit(not r.wasSuccessful())"
            result=subprocess.run([sys.executable,'-c',code],env={**os.environ,'FETCHSPEC_LEGACY_TEST_PROCESS':'1'},capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            return
        sys.path.insert(0,str(Path(os.environ['FETCHSPEC_LEGACY_INRESEARCH_ROOT'])/'src'))
        from inresearch.workflow import product_catalog as receiver, catalog_materials, product_navigation
        from inresearch.workflow.catalog_bundle import import_bundle
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{},clear=True):
            root=Path(temp)/'legacy';out=Path(temp)/'out';site=Path(temp)/'site'; fixture(root)
            report=export(root,out)
            manifest=json.loads((out/'manifest.json').read_text())
            base=json.loads((out/manifest['batches'][0]['path']).read_text())
            base.pop('catalog_mode');base['material_index']=[]
            base['products']=[next(p for p in base['products'] if p['name']=='SYS-6039P-TXRT')]
            base['products'][0]['observed_at']='2026-10-03T00:00:00+00:00'
            base['products'][0]['tables'][0]['rows'][0][1]['text']='NEWER_TEST_VALUE'
            base['generated_at']='2026-10-03T00:00:00+00:00'
            (site/'framework').mkdir(parents=True)
            (site/'framework/tco_targets.json').write_text(json.dumps({'targets':[]}))
            receiver.receive(site,base,'supermicro')
            for iteration in range(2):
                with Path(report['bundle']).open('rb') as stream:
                    receipt=import_bundle(site,stream,'supermicro')
                self.assertTrue(receipt['ok'])
                self.assertEqual(receipt['catalog_summary']['entities'],3)
                self.assertEqual(receipt['indexed_materials'],2)
                self.assertEqual(receipt['added_products'],2 if iteration==0 else 0)
            value=receiver.snapshot(site,'supermicro')
            self.assertEqual(next(p for p in value['products'] if p['name']=='SYS-6039P-TXRT')['tables'][0]['rows'][0][1]['text'],'NEWER_TEST_VALUE')
            materials=catalog_materials.query(site,'supermicro')
            self.assertEqual((materials['total'],materials['unassigned']),(2,1))
            self.assertEqual(catalog_materials.query(site,'supermicro','Test Report')['matched'],1)
            self.assertEqual(product_navigation.company_line(next(p for p in value['products'] if p['name'].startswith('SSG-')),'supermicro'),'storage')
            db=sqlite3.connect(receiver.database(site,'supermicro'))
            self.assertEqual(db.execute('SELECT count(*) FROM versions').fetchone()[0],3)
            db.close()


if __name__=='__main__':
    unittest.main()
