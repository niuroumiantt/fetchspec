import copy
import unittest
from fetchspec.adapters import adapter_for
from fetchspec.compute_catalog import expand_skus
from fetchspec.extraction import cell

class ComputeCatalogTests(unittest.TestCase):
    def test_candidate_shell_or_navigation_is_not_product_evidence(self):
        adapter=adapter_for('metax');url='https://www.metax-tech.com/prod.html?cid=107&id=21'
        for body in [b'<h1>Loading...</h1>', '<nav>旗舰级PCIe板卡形态通用GPU产品</nav>'.encode()]:
            with self.assertRaisesRegex(ValueError,'candidate only'):adapter.parse(body,url)
        self.assertFalse(adapter.page_allowed('https://www.metax-tech.com/'))

    def test_raw_spec_and_architecture_keep_original_scope(self):
        a=adapter_for('loongson');u='https://www.loongson.cn/product/show?id=41'
        body='''<h1>龙芯3C6000</h1><p>龙芯 3C6000 系列处理器采用龙芯第四代微处理器架构；支持LoongArch™指令系统</p><div class="parameter parameter2"><span><h4>物理核数</h4><p>16（S），32（D），64（Q）</p></span></div>'''.encode()
        p=a.parse(body,u);i=a.identity(p,u)
        self.assertEqual(i['kind'],'family_or_directory');self.assertEqual(i['compute']['form'],'series')
        self.assertEqual(p['tables'][0]['rows'][0][1]['text'],'16（S），32（D），64（Q）')
        self.assertEqual(i['compute']['source_refs'][0]['url'],u)

    def test_ampere_children_only_get_own_row_with_notes(self):
        table={'rows':[[cell('Processor Model'),cell('Usage Power*(W)')],[cell('AmpereOne A192-32X'),cell('283')],[cell('AmpereOne A128-34X'),cell('275')]],'notes':'SPECrate estimate; not TDP'}
        p={'id':'parent','name':'AmpereOne','tables':[table],'compute':{'category':'cpu','form':'series'}}
        data=expand_skus({'company_id':'ampere-computing','products':[p]})
        self.assertEqual(len(data['products']),3)
        child=data['products'][1]
        self.assertEqual(child['tables'][0]['rows'][1][1]['text'],'283')
        self.assertEqual(len(child['tables'][0]['rows']),2)
        self.assertEqual(child['tables'][0]['notes'],table['notes']);self.assertEqual(child['parent_id'],'parent')
        self.assertEqual(child['compute']['form'],'chip');self.assertEqual(p['compute']['form'],'series')

    def test_phytium_model_columns_keep_shared_cells_without_summing(self):
        rows=[[cell('子型号'),cell('飞腾S5000C-64'),cell('飞腾S5000C-32')],
              [cell('核心'),cell('FTC862',colspan=2)], [cell('核数'),cell('64核'),cell('32核')]]
        p={'id':'series','name':'S5000C','tables':[{'rows':rows}],'compute':{'category':'cpu','form':'series'}}
        data=expand_skus({'company_id':'phytium','products':[p]})
        self.assertEqual(len(data['products']),3)
        for child,n in zip(data['products'][1:],['64核','32核']):
            values={r[0]['text']:r[1]['text'] for r in child['tables'][0]['rows']}
            self.assertEqual(values['核心'],'FTC862');self.assertEqual(values['核数'],n)
        p['tables'][0]['rows'][1][0]['rowspan']=2
        self.assertEqual(len(expand_skus({'company_id':'phytium','products':[p]})['products']),1)

    def test_hygon_decodes_data_without_running_javascript(self):
        from fetchspec.hygon_catalog import product_data
        import json
        raw={'category1':[{'model':'海光7185','cores':32}],'category2':[{'model':'海光7380','cores':32}]}
        script='throw new Error("must never run");JSON.parse('+repr(json.dumps(raw))+')'
        self.assertEqual(product_data(script),raw)
        with self.assertRaises(ValueError):product_data('JSON.parse(fetch("https://example.test"))')
