"""Separate a source publisher from a product manufacturer.

Only reviewed support-page paths are excluded automatically. Brand names in a
product title raise review candidates; brands in specification cells do not.
Keep this dependency-free policy identical to InResearch's catalog_ownership.
"""
import argparse
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

VERSION = '2026-10-07.1'
BRANDS = {
    'kioxia': ('Kioxia', '铠侠', '鎧俠'),
    'samsung': ('Samsung', '三星'),
    'micron': ('Micron', '美光'),
    'intel': ('Intel', '英特尔'),
    'amd': ('AMD',),
    'sk-hynix': ('SK hynix', 'SK海力士'),
    'hgst': ('HGST',),
    'toshiba': ('Toshiba', '东芝'),
    'western-digital': ('Western Digital',),
    'solidigm': ('Solidigm',),
    'seagate': ('Seagate', '希捷'),
    'nvidia': ('NVIDIA', 'Mellanox', '英伟达'),
    'supermicro': ('Supermicro', 'Super Micro', '超微'),
    'asteralabs': ('Astera Labs', 'Astera', '阿斯特拉'),
    'delta': ('Delta', '台达'),
    'vertiv': ('Vertiv', 'Liebert', '维谛'),
    'siemens-energy': ('Siemens Energy', '西门子能源'),
    'siemens': ('Siemens', '西门子'),
    'ampere-computing': ('Ampere',),
    'biren': ('壁仞', '壁砺', 'Biren'),
    'cambricon': ('寒武纪', 'Cambricon'),
    'enflame': ('燧原', 'Enflame'),
    'huawei-ascend': ('Ascend', '昇腾'),
    'huawei-kunpeng': ('Kunpeng', '鲲鹏'),
    'hygon': ('Hygon', '海光'),
    'iluvatar-corex': ('Iluvatar', '天数智芯'),
    'loongson': ('Loongson', '龙芯'),
    'metax': ('MetaX', '沐曦'),
    'moore-threads': ('Moore Threads', '摩尔线程'),
    'phytium': ('Phytium', '飞腾'),
    'zhaoxin': ('Zhaoxin', '兆芯'),
    'dell': ('Dell',),
    'hpe': ('HPE', 'Hewlett Packard Enterprise'),
    'lenovo': ('Lenovo', '联想'),
    'gigabyte': ('Gigabyte',),
}


def support_context(company, url):
    """Reviewed third-party storage catalogs, including legacy/localized paths."""
    if company != 'supermicro':
        return None
    parts = urlsplit(url or '')
    host = parts.hostname or ''
    if host != 'supermicro.com' and not host.endswith('.supermicro.com'):
        return None
    path = unquote(parts.path).casefold()
    path = re.sub(r'^/(?:en|zh[-_](?:cn|tw)|ja(?:-jp)?|de|fr|es)(?=/)', '', path)
    path = re.sub(r'\.(?:cfm|php|html?)$', '', path).rstrip('/')
    if (re.match(r'^/products/storage/pci-e(?:/|$)', path)
            or path == '/products/nvme/vroc'):
        return {'status': 'conflict', 'reason': 'third_party_storage_support_page',
                'source_url': url, 'basis': 'reviewed_support_path', 'policy_version': VERSION}
    return None


def conflict(product, company):
    declared = product.get('company_id')
    if declared and declared != company:
        return {'status': 'conflict', 'reason': 'product_company_mismatch',
                'declared_company_id': declared, 'policy_version': VERSION}
    # A primary support table is not a manufacturer catalog, even if a related
    # product URL has been guessed or attached. Auxiliary links stay untouched.
    for key in ('source_url', 'product_url'):
        issue = support_context(company, product.get(key, ''))
        if issue:
            return issue
    return None


def review(product, company):
    issue = conflict(product, company)
    if issue:
        return issue
    name = product.get('name') or ''
    # Longest match avoids treating Siemens Energy as Siemens. Related
    # divisions keep their common brand; no ownership inferred from cell text.
    matches = [(len(alias), owner) for owner, aliases in BRANDS.items() for alias in aliases
               if re.match(r'^\s*' + re.escape(alias) + r'(?![A-Za-z0-9])', name, re.I)]
    longest = max((length for length, _ in matches), default=0)
    owners = [owner for length, owner in matches if length == longest]
    if company in owners or (company == 'hygon-dcu' and 'hygon' in owners):
        return None
    if owners:
        return {'status': 'review_required', 'reason': 'foreign_brand_in_product_name',
                'possible_manufacturer_company_ids': sorted(set(owners)),
                'basis': 'product_name_only_not_manufacturer_confirmation', 'policy_version': VERSION}
    return None


def audit(products, company):
    issues = [{**issue, 'product_id': p.get('id'), 'name': p.get('name'),
               'source_url': p.get('source_url'), 'source_sha256': p.get('source_sha256')}
              for p in products if (issue := review(p, company))]
    return {'company_id': company, 'policy_version': VERSION, 'checked_entities': len(products),
            'conflicts': sum(i['status'] == 'conflict' for i in issues),
            'review_required': sum(i['status'] == 'review_required' for i in issues),
            'issues': issues,
            'scope': 'Known support paths and explicit leading brands; unflagged entries are not proof of manufacturer ownership.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only ownership audit of exported catalogs or recorded batches.')
    parser.add_argument('--input', type=Path, action='append', required=True)
    args = parser.parse_args(argv)
    reports = []
    for path in args.input:
        value = json.loads(path.read_text())
        catalogs = value.get('companies', [value])
        for catalog in catalogs:
            report = audit(catalog['products'], catalog['company_id'])
            reports.append({**report, 'input': str(path)})
    if not reports or not sum(r['checked_entities'] for r in reports):
        raise ValueError('no catalog entities checked')
    print(json.dumps({'ok': True, 'reports': reports}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
