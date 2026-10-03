"""Reproducible, reviewed catalog supplement; immutable bytes stay outside Git.

Pins gate every source version. A changed source needs review, not silent reparse.
Existing products are unioned by stable identity; publication is a separate step.
"""
import argparse
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import urljoin

from .extraction import cell, html_page
from .inventory import atomic_bytes, atomic_json, utc_now
from .product_catalog import Document, Node
from .adapters.supermicro import SupermicroProductAdapter
from .inventory import load_profile
from .products import ProductStore, semantic
from .network import ProductFetcher

CONFIG = Path(__file__).resolve().parents[2] / 'config/catalog_batch2.json'


def decode(body):
    # Preserve wire bytes/SHA. Bound decompression before feeding the HTML parser.
    if body.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
            body = stream.read(16 * 1024 * 1024 + 1)
    if len(body) > 16 * 1024 * 1024:
        raise ValueError('expanded HTML exceeds limit')
    return body


def prose(root):
    if isinstance(root, str):
        return root
    if root.tag in {'script', 'style', 'noscript', 'nav', 'footer', 'svg'}:
        return ''
    value = ''.join(prose(c) for c in root.children)
    return '\n'+value+'\n' if root.tag in {'div','p','br','li','h1','h2','h3','h4','td','th','tr'} else value


def plain(root):
    return ' '.join(prose(root).split())


def collect_sources(root, company, config):
    """Fetch only reviewed URLs; changed bytes are archived but require a new review."""
    fetcher = ProductFetcher(load_profile(company))
    try:
        for entry in config['sources']:
            if entry['company_id'] != company:
                continue
            url = entry['url']; key = hashlib.sha256(url.encode()).hexdigest()
            receipt = root / 'review' / company / (key + '.json')
            if receipt.exists():
                old = json.loads(receipt.read_text())
                if old.get('sha256') == entry['sha256'] and (root / old['snapshot_path']).exists():
                    continue  # load_sources independently verifies cached bytes.
            wire, meta = fetcher.get(url)
            sha = hashlib.sha256(wire).hexdigest(); path = 'blobs/' + sha[:2] + '/' + sha
            atomic_bytes(root / path, wire)
            observation = {'requested_url': url, 'source_url': meta.get('final_url', url), 'sha256': sha,
                           'snapshot_path': path, 'observed_at': utc_now(), 'http': meta}
            atomic_json(root / 'review' / company / 'observations' / (key + '-' + sha + '.json'), observation)
            if sha != entry['sha256']:
                raise ValueError('new source version archived; review and update pin before extraction')
            atomic_json(receipt, observation)
    finally:
        data = fetcher.policy_receipts
        key = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        atomic_json(root / 'review' / company / 'policies' / (key + '.json'), data)


def load_sources(root, company, config):
    result = {}
    for entry in config['sources']:
        if entry['company_id'] != company:
            continue
        receipt = root / 'review' / company / (hashlib.sha256(entry['url'].encode()).hexdigest() + '.json')
        source = json.loads(receipt.read_text())
        path = (root / source['snapshot_path']).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError('source outside archive')
        wire = path.read_bytes()
        if hashlib.sha256(wire).hexdigest() != entry['sha256'] or source['sha256'] != entry['sha256']:
            raise ValueError('source version changed; review required')
        body = decode(wire)
        doc = Document(body.decode('utf-8', 'replace')).root
        source.update(format='html', kind='html', language='zh' if company in {'zhaoxin','biren','moore-threads'} else 'en')
        result[entry['url']] = {'source': source, 'body': body, 'doc': doc, 'text': plain(doc), 'page': html_page(body, source['source_url'])}
    return result


def ref(source):
    return {'url': source['source_url'], 'sha256': source['sha256']}


def identity(company, key):
    return company + '-' + hashlib.sha256(key.encode()).hexdigest()[:20]


def require(text, quote):
    if not quote or ' '.join(quote.split()) not in text:
        raise ValueError('official evidence not found: ' + quote[:100])
    return quote


def native(table, source, rows=None):
    return {**copy.deepcopy(table), 'rows': copy.deepcopy(rows if rows is not None else table['rows']), 'source_refs': [ref(source)]}


def make(company, name, item, category, form, *, kind='named_product', key=None, url=None, tables=None, quote=None, architecture='', architecture_quote=''):
    s = item['source']; quote = quote or name
    require(item['text'], quote)
    if architecture:
        require(item['text'], architecture_quote)
    label = {'amd': 'AMD EPYC' if category=='cpu' else 'AMD Instinct', 'intel': 'Intel Xeon' if category=='cpu' else 'Intel Data Center GPU', 'zhaoxin': '通用处理器', 'supermicro': '服务器 / Server', 'moore-threads': 'GPU', 'biren': 'GPU'}.get(company,category)
    return {'id': identity(company, key or url or name), 'name': name, 'company_id': company,
            'parent_id': None, 'kind': kind, 'category': label, 'taxonomy': [], 'category_basis': 'reviewed_product_scope',
            'listing': 'directory' if kind == 'family_or_directory' else 'active', 'availability': 'not_verified',
            'product_url': url or s['source_url'], 'source_url': s['source_url'], 'source_sha256': s['sha256'],
            'observed_at': s['observed_at'], 'tables': tables or [],
            'extraction_status': 'native_tables_extracted' if tables else 'review_required_no_native_specification',
            'compute': {'category': category if category in {'cpu','gpu','accelerator'} else 'excluded', 'form': form,
                        'architecture': architecture, 'architecture_quote': architecture_quote, 'evidence_quote': quote, 'source_refs': [ref(s)]},
            'official_pages': [ref(s)], 'attachments': []}


def epyc_rows(table_node):
    rows = list(table_node.walk('tr'))
    header = [n.text() for n in rows[0].children if isinstance(n, Node) and n.tag in {'td','th'}]
    for row in rows[1:]:
        columns = [[x.strip() for x in n.raw_text().splitlines() if x.strip()] for n in row.children if isinstance(n, Node) and n.tag in {'td','th'}]
        if len(columns) != len(header) or not columns or any(not re.fullmatch(r'9\d{3}[FP]?', x) for x in columns[0]):
            raise ValueError('EPYC model layout changed')
        count = len(columns[0])
        if any(len(c) not in {1, count} for c in columns):
            raise ValueError('ambiguous EPYC per-model field alignment')
        for index in range(count):
            yield header, [c[0] if len(c) == 1 else c[index] for c in columns]


def amd(items):
    result = []
    gpu = next(v for v in items.values() if v['source']['source_url'].endswith('/reference/gpu-specs.html'))
    table = gpu['page']['tables'][0]
    if table['rows'][0][0]['text'] != 'Name' or len(table['rows']) != 17:
        raise ValueError('Instinct model table changed')
    micro = next(v for v in items.values() if v['source']['source_url'].endswith('/reference/gpu-arch/mi350.html'))
    form_quote = next(n.text() for n in micro['doc'].walk('p') if 'both in the OAM form factor' in n.text())
    for row in table['rows'][1:]:
        name, arch = row[0]['text'], row[1]['text']
        category = 'accelerator' if name == 'MI300A' else 'gpu'
        p = make('amd','AMD Instinct '+name,gpu,category,'unknown',quote=name,architecture=arch,architecture_quote=arch,
                 tables=[native(table,gpu['source'],[table['rows'][0],row])])
        if name in {'MI350X','MI355X','MI350P'}:
            p['compute'].update(form='board' if name=='MI350P' else 'module',evidence_quote=require(micro['text'],form_quote))
            p['compute']['source_refs'].append(ref(micro['source']))
            t=micro['page']['tables'][0]; col=next(i for i,c in enumerate(t['rows'][0]) if c['text'].startswith(name+' '))
            extra=native(t,micro['source'],[[r[0],r[col]] for r in t['rows']])
            extra['source_table_index']=extra['index'];extra['index']=len(p['tables'])+1
            p['tables'].append(extra)
        if name == 'MI300A':
            p['compute']['classification_note']='APU：此表仅给 GPU 部分；CPU 核心与封装形态未据本表推定。'
        result.append(p)
    cpu = next(v for v in items.values() if '/detail/1219/' in v['source']['source_url'])
    for header,row in epyc_rows(next(cpu['doc'].walk('table'))):
        t={'index':1,'section':'AMD EPYC 9005 official launch model table','rows':[[cell(x,header=True) for x in header],[cell(x) for x in row]],
           'method':'html_explicit_line_aligned_model_row','notes':'官方发布时点原表；保留历史价格和脚注，非当前报价或在售证明。\n'+cpu['page']['tables'][0]['notes'],'source_refs':[ref(cpu['source'])]}
        result.append(make('amd','AMD EPYC '+row[0],cpu,'cpu','chip',quote=row[0],tables=[t],architecture=row[2],architecture_quote=row[2]))
    return result


def flex_board_quote(item, model):
    """Match a complete variant name, never Flex 170 inside Flex 170V."""
    paragraphs=[n.text() for n in item['doc'].walk('p') if 'PCIe' in n.text()]
    if model=='140':
        paragraph=next(p for p in paragraphs if '75W, half-height PCIe package' in p)
        return require(item['text'],'Intel® Data Center GPU Flex 140 '+paragraph)
    paragraph=next(p for p in paragraphs if re.search(r'Flex '+re.escape(model)+r'\b',p))
    return require(item['text'],paragraph)


def intel(items):
    result = {}
    for item in items.values():
        url=item['source']['source_url']
        if not any(x in url for x in ['/xeon/entry.html','/xeon/6-p-core-series.html','/data-center-gpu/max-series.html','/data-center-gpu/flex-series.html']):continue
        table=item['page']['tables'][0]
        for row in table['rows'][1:]:
            name=row[0]['text']; link=next(l for l in item['page']['links'] if l['label']==name and '/sku/' in l['url'])
            target=urljoin(url,link['url']); category='cpu' if 'Xeon' in name else 'gpu'
            result[target]=make('intel',name,item,category,'chip' if category=='cpu' else 'unknown',url=target,tables=[native(table,item['source'],[table['rows'][0],row])])
    for item in items.values():
        url=item['source']['source_url']
        if '/sku/' not in url:continue
        name=item['page']['heading'];category='cpu' if 'Xeon' in name else 'gpu'
        p=make('intel',name,item,category,'chip' if category=='cpu' else 'unknown',url=url,tables=[native(t,item['source']) for t in item['page']['tables']])
        for t in p['tables']:
            for row in t['rows']:
                if row[0]['text']=='Microarchitecture':p['compute'].update(architecture=row[1]['text'],architecture_quote=require(item['text'],row[1]['text']))
        # Never infer silicon model from an ARK board/model name or code name.
        variant=re.search(r'/intel-data-center-gpu-flex-(140|170v?)/',url)
        if variant:
            directory=next(v for v in items.values() if v['source']['source_url'].endswith('/flex-series.html'))
            quote=flex_board_quote(directory,variant[1].upper())
            p['compute'].update(form='board',evidence_quote=quote,source_refs=[ref(item['source']),ref(directory['source'])])
        result[url]=p
    return list(result.values())


def zhaoxin(items):
    item=next(v for v in items.values() if v['source']['requested_url'].endswith('/pro.aspx?nid=3'));out=[]
    for node in item['doc'].walk('div'):
        if not node.attrs.get('class','').startswith('data_col'):continue
        line=next((c for c in node.children if isinstance(c,Node)),None)
        fields=[c.text() for c in line.children if isinstance(c,Node)] if line else []
        if len(fields)!=6 or '系列处理器' not in fields[0]:continue
        name=fields[0];is_kh=name.startswith('开胜')
        headers=['型号','架构代号',*(['发布日期','工艺'] if is_kh else ['工艺','发布日期']),'最高工作频率','内核数']
        target=urljoin(item['source']['source_url'],next(node.walk('a')).attrs['href'])
        t={'index':1,'section':'官方 CPU 系列目录原字段','rows':[[cell(x,header=True) for x in headers],[cell(x) for x in fields]],'method':'official_div_grid','notes':'系列最高值/多种配置；空白工艺保留为空，不拆成具体 SKU。','source_refs':[ref(item['source'])]}
        p=make('zhaoxin',name,item,'cpu','series',kind='family_or_directory',url=target,tables=[t],architecture=fields[1],architecture_quote=fields[1])
        out.append(p)
    if len(out)!=12:raise ValueError('Zhaoxin reviewed CPU series layout changed')
    return out


def supermicro(items):
    adapter=SupermicroProductAdapter(load_profile('supermicro'));result=[]
    for item in items.values():
        url=item['source']['source_url'];page=adapter.parse(item['body'],url);ident=adapter.identity(page,url)
        tables=[native(t,item['source']) for t in page['tables'] if t['section']!='Optional Parts List']
        p=make('supermicro',ident['name'],item,'servers','system',url=url,key=ident['name'].casefold(),tables=tables)
        result.append(p)
    return result


def moore(items):
    out=[]
    s4=next(v for v in items.values() if '/product_specifications/' in v['source']['source_url'])
    s5=next(v for v in items.values() if '/product/S5000' in v['source']['source_url'])
    arch=next(v for v in items.values() if '/architecture/pinghu' in v['source']['source_url'])
    q4='MTT S4000 是基于摩尔线程曲院 GPU 架构打造的全功能元计算卡'
    p4=make('moore-threads','MTT S4000',s4,'gpu','board',key='https://docs.mthreads.com/s4000/s4000-doc-online/',quote=q4,
            architecture='曲院 GPU',architecture_quote=q4,tables=[native(t,s4['source']) for t in s4['page']['tables']])
    q5=next(n.text() for n in s5['doc'].walk('p') if n.text().startswith('MTT S5000 是一款'))
    precision='提供从 FP8 到 FP64 的全精度算力支持。';require(s5['text'],precision)
    form='OAM 计算模组';require(s5['text'],form)
    t={'index':1,'section':'官方产品正文规格（非完整硬件参数表）','rows':[[cell('精度支持'),cell(precision)],[cell('产品形态'),cell(form)]],
       'method':'reviewed_literal_fields','notes':'对标基准与多机测试吞吐不充当单卡硬件规格；完整数值参数表待补。','source_refs':[ref(s5['source'])]}
    p5=make('moore-threads','MTT S5000',s5,'gpu','module',key=s5['source']['source_url'],quote=q5,architecture='平湖',architecture_quote='凭借先进的“平湖”架构',tables=[t])
    for p,name,item,quote,architecture,aq in [
        (p4,'QY102AA-800',s4,'芯片 图形芯片 QY102AA-800','',''),
        (p5,'PH100',arch,'PH100 作为旗舰智算卡 MTT S5000 的核心','平湖','基于"平湖"架构的全新一代 PH100 芯片')]:
        chip=make('moore-threads',name,item,'gpu','chip',quote=quote,architecture=architecture,architecture_quote=aq)
        p['compute']['chip_links']=[{'product_id':chip['id'],'evidence_quote':require(item['text'],quote),'source_refs':[ref(item['source'])]}]
        out.extend([p,chip])
    return out


def biren(items):
    item=next(v for v in items.values() if '/csr-article/' in v['source']['source_url']);q='首款国产高端通用GPU芯片BR100系列已正式发布'
    return [make('biren','BR100系列',item,'gpu','series',kind='family_or_directory',quote=q)]


def merge_catalog(company, products, sources, baseline, limitations):
    old=baseline.get(company,{})
    before={p['id']:copy.deepcopy(p) for p in old.get('products',[])}
    # Existing identifiers and names survive unless explicitly replaced.
    if len({p['id'] for p in products})!=len(products):raise ValueError('duplicate supplement identity')
    for raw in products:
        p=copy.deepcopy(raw); old_product=before.get(p['id'])
        if old_product:
            for field in ('taxonomy','category','category_basis','parent_id','listing','part_number','official_status'):
                if field in old_product:p[field]=copy.deepcopy(old_product[field])
            for field in ('official_pages','attachments'):
                union={x['url']:x for x in old_product.get(field,[])}
                union.update({x['url']:x for x in p.get(field,[])})
                p[field]=list(union.values())
        before[p['id']]=p
    refs={(s['sha256'],s['source_url']):s for s in old.get('sources',[])}
    refs.update({(s['sha256'],s['source_url']):s for s in sources})
    return {'schema_version':1,'company_id':company,'generated_at':utc_now(),'products':list(before.values()),'sources':list(refs.values()),
            'coverage':{'complete':False,'limitations':limitations+['仅覆盖本轮审阅的官方型号/系列；历史值与空白保留，不证明当前在售或厂商全量。']}}


def run(root,company,baseline):
    config=json.loads(CONFIG.read_text());items=load_sources(root,company,config)
    parsers={'amd':amd,'intel':intel,'zhaoxin':zhaoxin,'supermicro':supermicro,'moore-threads':moore,'biren':biren}
    if company=='sk-hynix':
        products=[]
        for e in config.get('sk_hynix_products',[]):
            item=items[e['url']];rows=[[cell(s['label']),cell(require(item['text'],s['quote']))] for s in e['specs']]
            table={'index':1,'section':'官方发布正文中的产品规格','rows':rows,'notes':'新闻稿原文；不是完整数据表或研究采用。','method':'reviewed_literal_fields','source_refs':[ref(item['source'])]}
            products.append(make(company,e['name'],item,e['category'],'series' if e['kind']=='family_or_directory' else 'unknown',kind=e['kind'],quote=e['quote'],tables=[table] if rows else []))
    else:products=parsers[company](items)
    if company=='biren':
        for old in baseline.get(company,{}).get('products',[]):
            item=items.get(old['source_url'])
            if item and ('OAM 模组' in item['text'] or 'OAM V1.1 风冷模组' in item['text']):
                p=copy.deepcopy(old);require(item['text'],p['compute']['evidence_quote']);p['compute']['form']='module';products.append(p)
    for p in products:
        indexes=[t['index'] for t in p['tables']]
        if len(indexes)!=len(set(indexes)):raise ValueError('duplicate product table index')
    payload=merge_catalog(company,products,[v['source'] for v in items.values()],baseline,config.get('limitations',{}).get(company,[]))
    with ProductStore(root) as store:
        prior={p['id']:p for p in store.export_catalog(company)['products']}
        for p in payload['products']:
            if p['id'] in prior:
                old=prior[p['id']]
                p['observed_at']=old['observed_at'] if semantic(p)==semantic(old) else utc_now()
        store.ingest_catalog(payload,root)
    atomic_json(root/'exports'/company/'catalog.json',payload)
    return payload


def main():
    p=argparse.ArgumentParser(__doc__);p.add_argument('--root',type=Path,required=True);p.add_argument('--company',required=True);p.add_argument('--baseline',type=Path,required=True);p.add_argument('--collect',action='store_true')
    a=p.parse_args()
    if a.collect:collect_sources(a.root.expanduser(),a.company,json.loads(CONFIG.read_text()))
    v=run(a.root.expanduser(),a.company,json.loads(a.baseline.expanduser().read_text()))
    print(json.dumps({'company':a.company,'products':len(v['products']),'named':sum(x['kind']=='named_product' for x in v['products']),'with_specs':sum(bool(x['tables']) for x in v['products'])}));return 0

if __name__=='__main__':raise SystemExit(main())
