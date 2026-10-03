import copy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from fetchspec.catalog_batch2 import decode, epyc_rows, load_sources, merge_catalog, flex_board_quote, plain
from fetchspec.product_catalog import Document


class Batch2Tests(unittest.TestCase):
    def test_epyc_explicit_lines_align_model_fields_and_shared_cores(self):
        node=next(Document('<table><tr><th>Model</th><th>Cores</th><th>TDP</th></tr><tr><td>9755<br>9745</td><td>128 cores</td><td>500W<br>400W</td></tr></table>').root.walk('table'))
        rows=list(epyc_rows(node))
        self.assertEqual([r[1] for r in rows],[['9755','128 cores','500W'],['9745','128 cores','400W']])
        bad=next(Document('<table><tr><th>Model</th><th>TDP</th></tr><tr><td>9755<br>9745</td><td>500W<br>400W<br>300W</td></tr></table>').root.walk('table'))
        with self.assertRaisesRegex(ValueError,'ambiguous'):list(epyc_rows(bad))

    def test_wire_digest_survives_bounded_gzip_and_version_pin(self):
        body=gzip.compress(b'<form><p>Official product</p></form>')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'blobs').mkdir();(root/'blobs/a').write_bytes(body)
            url='https://a.test/product';sha=hashlib.sha256(body).hexdigest();dest=root/'review/amd';dest.mkdir(parents=True)
            (dest/(hashlib.sha256(url.encode()).hexdigest()+'.json')).write_text(json.dumps({'source_url':url,'sha256':sha,'snapshot_path':'blobs/a'}))
            cfg={'sources':[{'company_id':'amd','url':url,'sha256':sha}]}
            item=load_sources(root,'amd',cfg)[url]
            self.assertEqual(item['text'],'Official product');self.assertEqual(item['source']['sha256'],sha)
            (root/'blobs/a').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'version changed'):load_sources(root,'amd',cfg)
        with self.assertRaisesRegex(ValueError,'expanded HTML'):decode(gzip.compress(b'x'*(16*1024*1024+1)))

    def test_supplement_preserves_existing_and_only_replaces_explicit_identity(self):
        baseline={'amd':{'products':[{'id':'old','name':'old'},{'id':'update','name':'before'}],'sources':[{'source_url':'https://a.test/old','sha256':'a'}]}}
        before=copy.deepcopy(baseline)
        result=merge_catalog('amd',[{'id':'new','name':'new'},{'id':'update','name':'after'}],[],baseline,[])
        self.assertEqual(baseline,before)
        self.assertEqual({p['id']:p['name'] for p in result['products']},{'old':'old','new':'new','update':'after'})
        self.assertEqual(result['sources'],baseline['amd']['sources'])
        with self.assertRaisesRegex(ValueError,'duplicate'):merge_catalog('amd',[{'id':'new'},{'id':'new'}],[],baseline,[])

    def test_flex_variant_evidence_is_not_shared_by_substring(self):
        doc=Document('<h3>Intel® Data Center GPU Flex 140</h3><p>75W, half-height PCIe package</p><p>PCIe Intel Data Center GPU Flex 170 for AI</p><p>PCIe Intel Data Center GPU Flex 170V for VDI</p>').root
        item={'doc':doc,'text':plain(doc)}
        self.assertEqual(flex_board_quote(item,'170V'),'PCIe Intel Data Center GPU Flex 170V for VDI')
        self.assertEqual(flex_board_quote(item,'170'),'PCIe Intel Data Center GPU Flex 170 for AI')
        self.assertIn('Flex 140 75W',flex_board_quote(item,'140'))
