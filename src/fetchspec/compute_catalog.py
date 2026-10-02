"""Bounded compute collection using the existing acquisition and ProductStore ledgers."""
import argparse
import copy
import hashlib
import json
from pathlib import Path

from .acquisition import collect
from .adapters.compute import definitions
from .inventory import atomic_json
from .products import ProductStore


def expand_skus(payload):
    """Only explicit model rows become children; never clone all family values to a SKU."""
    for parent in list(payload['products']):
        if payload['company_id'] != 'ampere-computing' or parent['name'] != 'AmpereOne': continue
        for table in parent['tables']:
            rows=table['rows']
            if not rows or rows[0][0]['text'] != 'Processor Model': continue
            for row in rows[1:]:
                name=row[0]['text']
                if not name.startswith('AmpereOne') or len(row)!=len(rows[0]): continue
                child=copy.deepcopy(parent)
                child.update(id='ampere-computing-'+hashlib.sha256(name.encode()).hexdigest()[:20],name=name,
                             parent_id=parent['id'],kind='named_product',listing='active',part_number=name)
                child['compute'].update(form='chip',evidence_quote=name)
                child['tables']=[{**table,'rows':[rows[0],row]}]
                payload['products'].append(child)
    if payload['company_id'] == 'phytium':
        for parent in list(payload['products']):
            for table in parent['tables']:
                # Expand only explicit colspan values; complex rowspan layouts stay series.
                if any(c.get('rowspan',1)!=1 for row in table['rows'] for c in row): continue
                grid=[[c['text'] for c in row for _ in range(c.get('colspan',1))] for row in table['rows']]
                header=next((row for row in grid if row and row[0]=='子型号'),None)
                if not header: continue
                for col,name in enumerate(header[1:],1):
                    if not name.startswith('飞腾'): continue
                    child=copy.deepcopy(parent)
                    child.update(id='phytium-'+hashlib.sha256(name.encode()).hexdigest()[:20],name=name,
                        parent_id=parent['id'],kind='named_product',listing='active',part_number=name)
                    child['compute'].update(form='chip',evidence_quote=name)
                    from .extraction import cell
                    child['tables']=[{**table,'rows':[[cell(row[0]),cell(row[col])] for row in grid if len(row)>col and row[0]!='类型']}]
                    payload['products'].append(child)
    return payload


def run(root,company,*,reparse=False,fetcher=None):
    entries=[e for e in definitions()['products'] if e['company_id']==company]
    if not entries and company != 'hygon': raise ValueError('company has only candidate entrypoints; no reviewed products')
    with ProductStore(root) as store:
        if company == 'hygon':
            if reparse: raise ValueError('Hygon static component requires a fresh verified link chain')
            from .hygon_catalog import collect_hygon
            payload=collect_hygon(root,fetcher)
        else:
            payload=collect(root,company,[e['url'] for e in entries],max_pages=len(entries),
                            known_catalog=store.export_catalog(company),reparse=reparse,fetcher=fetcher)
        expand_skus(payload)
        payload['coverage']['limitations']=payload['coverage'].get('limitations',[])+['仅覆盖已审阅型号页；候选入口不算产品，全公司目录未穷尽。',
            *[e['gap'] for e in entries if e.get('gap')]]
        store.ingest_catalog(payload,root)
        # Export this audited run, including new SKU children and evidence receipts.
        dest=Path(root)/'exports'/company/'catalog.json'
        atomic_json(dest,payload)
        return {'company':company,'products':len(payload['products']),
                'named_products':sum(p['kind']=='named_product' for p in payload['products']),
                'with_specs':sum(bool(p['tables']) for p in payload['products']),
                'output':str(dest),'errors':payload['acquisition_report']['errors']}


def main():
    p=argparse.ArgumentParser(__doc__)
    p.add_argument('--root',type=Path,default=Path.home()/'.local/share/fetchspec/compute-catalog-20261002')
    p.add_argument('--company',required=True);p.add_argument('--reparse',action='store_true')
    args=p.parse_args();result=run(args.root,args.company,reparse=args.reparse)
    print(json.dumps(result,ensure_ascii=False));return int(bool(result['errors']))

if __name__=='__main__': raise SystemExit(main())
