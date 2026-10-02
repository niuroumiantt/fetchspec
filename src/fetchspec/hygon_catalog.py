"""Hygon's official catalog uses a static JSON literal inside its linked JS bundle.

Only decode JSON.parse string literals; never execute downloaded JavaScript.
CPU rows are explicit models. The DCU marketing page supplies no model rows.
"""
import ast
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urljoin

from .extraction import cell
from .inventory import atomic_bytes, atomic_json, load_profile, utc_now
from .network import ProductFetcher
from .product_catalog import Document


def product_data(script):
    for match in re.finditer(r"JSON\.parse\(('(?:\\.|[^'\\])*')\)", script):
        try:
            data=json.loads(ast.literal_eval(match.group(1)))
        except (ValueError,SyntaxError,json.JSONDecodeError):
            continue
        if isinstance(data,dict) and set(data)=={'category1','category2'}:
            if all(isinstance(rows,list) and rows and all(isinstance(r,dict) and isinstance(r.get('model'),str) and 'cores' in r for r in rows) for rows in data.values()):
                return data
    raise ValueError('official model JSON missing; candidate only')


def collect_hygon(root,fetcher=None):
    root=Path(root);fetcher=fetcher or ProductFetcher(load_profile('hygon'));sources=[]
    def fetch(url,kind):
        body,meta=fetcher.get(url);sha=hashlib.sha256(body).hexdigest();path=Path('blobs')/sha[:2]/sha
        atomic_bytes(root/path,body)
        source={'source_url':meta.get('final_url',url),'requested_url':url,'sha256':sha,'snapshot_path':str(path),
                'observed_at':utc_now(),'format':kind,'kind':kind,'language':'zh','http':meta}
        if sources: source['discovered_from'] = sources[-1]['source_url']
        sources.append(source);return body.decode('utf-8','replace'),source
    try:
        page,page_source=fetch('https://www.hygon.cn/product/productlist?category=category2','html')
        scripts=[n.attrs.get('src','') for n in Document(page).root.walk('script')]
        index=next((s for s in scripts if re.fullmatch(r'/js/index\.[a-f0-9]+\.js',s)),None)
        if not index:raise ValueError('official entry script missing')
        script,_=fetch(urljoin(page_source['source_url'],index),'javascript')
        # Reviewed current application chunk, discovered from the linked entry file.
        chunk=re.search(r'430:"([a-f0-9]+)"',script)
        if not chunk:raise ValueError('official application chunk changed; review required')
        data_script,source=fetch(urljoin(page_source['source_url'],'/js/430.'+chunk[1]+'.js'),'javascript')
        data=product_data(data_script);products=[]
        ref={'url':source['source_url'],'sha256':source['sha256']}
        for category,rows in data.items():
            for record in rows:
                name=record['model'];pid='hygon-'+hashlib.sha256(re.sub(r'\s+','',name).encode()).hexdigest()[:20]
                products.append({'id':pid,'name':name,'company_id':'hygon','parent_id':None,'kind':'named_product',
                    'category':'海光CPU','taxonomy':[{'slug':'cpu','name':'海光CPU'}],'listing':'active','availability':'not_verified',
                    'official_component_category':category,'product_url':page_source['source_url'],
                    'source_url':source['source_url'],'source_sha256':source['sha256'],'observed_at':source['observed_at'],
                    'extraction_status':'native_tables_extracted','tables':[{'index':1,'section':'官方型号参数（静态 JSON 原字段）',
                        'rows':[[cell(k),cell(str(v))] for k,v in record.items()],'notes':'原字段与原值；不推断架构代际。',
                        'method':'official_static_json_literal','source_refs':[ref]}],
                    'compute':{'category':'cpu','form':'chip','architecture':'','evidence_quote':name,'source_refs':[ref]},
                    'official_pages':[{'url':page_source['source_url'],'sha256':page_source['sha256']}],'attachments':[]})
        if len({p['id'] for p in products})!=len(products):raise ValueError('duplicate official model; review required')
        return {'schema_version':1,'company_id':'hygon','generated_at':utc_now(),'products':products,'sources':sources,
                'coverage':{'complete':False,'limitations':['当前官网静态组件中明示的 CPU 型号；未证明全系列穷尽。','DCU 无逐型号官方架构证据，不套用 CPU 或软件生态信息。']},
                'acquisition_report':{'errors':[],'complete':False}}
    finally:
        receipts=getattr(fetcher,'policy_receipts',[])
        if receipts:
            sha=hashlib.sha256(json.dumps(receipts,sort_keys=True).encode()).hexdigest()
            atomic_json(root/'acquisition/hygon/policies'/(sha+'.json'),receipts)
