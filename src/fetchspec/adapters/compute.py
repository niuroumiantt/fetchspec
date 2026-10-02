"""Exact reviewed product pages; a URL or a navigation mention alone proves nothing."""
import hashlib
import json
import re
from pathlib import Path

from .base import ProductAdapter
from ..extraction import cell, html_page
from ..product_catalog import Document

CONFIG = Path(__file__).resolve().parents[3] / 'config/compute_catalog.json'


def definitions():
    return json.loads(CONFIG.read_text())


def normalized(text):
    return ' '.join(text.split())


class ComputeProductAdapter(ProductAdapter):
    def __init__(self, known_catalog=None):
        super().__init__(known_catalog)
        self.entries = {e['url']: e for e in definitions()['products'] if e['company_id'] == self.company_id}

    def page_allowed(self, url):
        return url in self.entries

    def parse(self, body, url):
        page = html_page(body, url)
        root = Document(body.decode('utf-8', 'replace')).root
        # Do not accept script payloads or global navigation as model evidence.
        def text(node):
            if isinstance(node,str): return node
            if node.tag in {'script','style','nav','header','footer'}: return ''
            return ' '.join(text(c) for c in node.children)
        plain = normalized(text(root))
        entry = self.entries.get(url)
        if not entry or normalized(entry['evidence_quote']) not in plain:
            raise ValueError('reviewed product identity evidence missing; candidate only')
        if entry.get('architecture_quote') and normalized(entry['architecture_quote']) not in plain:
            raise ValueError('model architecture evidence no longer matches official page')
        rows = []
        for label, pattern in entry.get('spec_patterns', []):
            match = re.search(pattern, plain)
            if match:
                rows.append([cell(label),cell(match.group(1))])
        if entry.get('parser') == 'loongson_parameters':
            for block in root.walk():
                if 'parameter' not in block.attrs.get('class','').split(): continue
                for span in block.walk('span'):
                    label=next((n.text() for n in span.walk('h4')), '')
                    value=next((n.text() for n in span.walk('p')), '')
                    if label and value: rows.append([cell(label),cell(value)])
        if rows:
            page['tables'] = [{'index':1,'section':'产品参数（官方原文）','rows':rows,'notes':'保留页面原值和条件；未知字段不补值。','is_specification':True,'method':'official_text_fields'}]
        else:
            page['tables']=[t for t in page['tables'] if t['is_specification']]
        # Classification is evidence metadata, not replacement vendor navigation.
        compute={k:entry.get(k,'') for k in ('category','form','architecture','evidence_quote','architecture_quote')}
        compute['source_refs']=[{'url':url,'sha256':hashlib.sha256(body).hexdigest()}]
        page['reviewed_identity']={'id':self.company_id+'-'+hashlib.sha256(url.encode()).hexdigest()[:20],
            'name':entry['name'],'kind':'family_or_directory' if entry['form']=='series' else 'named_product',
            'parent_id':None,'taxonomy':entry.get('taxonomy',[]),'category':' / '.join(t['name'] for t in entry.get('taxonomy',[])),
            'listing':'directory' if entry['form']=='series' else 'active','compute':compute}
        return page

    def identity(self,page,url):
        return page.get('reviewed_identity')

    def candidates(self,page,url,is_directory=False):
        # No blanket product-path regex. Add and review a page before collecting it.
        return []
