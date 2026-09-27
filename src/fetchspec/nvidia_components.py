"""Extract literal vendor component data without executing downloaded JavaScript."""
import argparse
import hashlib
import fcntl
import json
from pathlib import Path
import re
import sqlite3
from .inventory import InventoryFetcher, atomic_bytes, utc_now
from .product_catalog import export, identifier, entity_kind


def literal_object(text, start):
    decoder = json.JSONDecoder()
    tokens, depth, i = [], 0, start
    while i < len(text):
        char = text[i]
        if char.isspace():
            i += 1
            continue
        if char == '"':
            value, used = decoder.raw_decode(text[i:])
            tokens.append(json.dumps(value))
            i += used
        elif text[i:i+2] in ('!0', '!1') and text[i+2:i+3] in (',', '}'):
            tokens.append('true' if text[i:i+2] == '!0' else 'false')
            i += 2
        elif char in '{}[]:,':
            tokens.append(char)
            if char in '{[': depth += 1
            elif char in '}]':
                depth -= 1
                if depth == 0: return json.loads(''.join(tokens))
            i += 1
        else:
            match = re.match(r'-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[A-Za-z_$][A-Za-z0-9_$]*', text[i:])
            if not match: raise ValueError('non-literal component data at offset %d: %r' % (i, text[i:i+60]))
            token = match.group()
            i += len(token)
            if text[i:].lstrip().startswith(':'): tokens.append(json.dumps(token))
            elif token in {'true', 'false', 'null'} or re.fullmatch(r'-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?', token): tokens.append(token)
            else: raise ValueError('non-literal component expression')
    raise ValueError('incomplete component object')


def extract(text, selected):
    for match in re.finditer(r'\{"RTX \d{4}"\s*:\s*\{', text):
        data = literal_object(text, match.start())
        if not all(name in data and isinstance(data[name], dict) for name in selected): continue
        for name in selected:
            if not all(isinstance(k, str) and type(v) in (str, int, float) for k, v in data[name].items()):
                raise ValueError('unsupported specification value')
        return {name: data[name] for name in selected}
    raise ValueError('official component literal not found')


def run(root, *, refresh=False):
    root = Path(root).expanduser()
    base = root / 'product-catalog/nvidia'
    with (base / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _run(root, base, refresh=refresh)


def _run(root, base, *, refresh=False):
    db = sqlite3.connect(base / 'discovery.sqlite3')
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE IF NOT EXISTS component_sources(sha TEXT PRIMARY KEY,payload TEXT);
      CREATE TABLE IF NOT EXISTS component_products(id TEXT PRIMARY KEY,payload TEXT);''')
    candidates = []
    existing_models = set()
    for saved in db.execute('SELECT payload FROM pages'):
        page = json.loads(saved[0])
        if entity_kind(page['heading'], page['source_url']) == 'named_product' and any(t['is_specification'] for t in page['tables']):
            existing_models.add(re.sub(r'^NVIDIA\s+', '', page['heading']).casefold())
        if not page['source_url'].endswith('/geforce/graphics-cards/50-series/'): continue
        body = (root / page['snapshot_path']).read_bytes()
        if hashlib.sha256(body).hexdigest() != page['sha256']: raise ValueError('page snapshot hash mismatch')
        html = body.decode()
        urls = re.findall(r'src="(https://images\.nvidia\.com/aem-dam/en-zz/Solutions/raw-html/dynamic-specs-table-50-series/v[\d.]+\.js)"', html)
        columns = re.search(r'staticColumns:\s*(\[[^\]]+\])', html)
        if urls and columns: candidates.append((page, urls[0], json.loads(columns.group(1))))
    if not candidates: raise ValueError('no observed official component declaration')
    fetcher = InventoryFetcher({'allowed_hosts': ['images.nvidia.com', 'images.nvidia.cn'],
        'delay_seconds': 1, 'timeout_seconds': 30, 'max_xml_bytes': 5 * 1024 * 1024})
    fetcher.prepare_robots()
    seen = set()
    for page, url, columns in candidates:
        if url in seen: continue
        seen.add(url)
        source = next((json.loads(r[0]) for r in db.execute('SELECT payload FROM component_sources')
                       if json.loads(r[0])['requested_url'] == url), None)
        if source and not refresh:
            body = (root / source['snapshot_path']).read_bytes()
            if hashlib.sha256(body).hexdigest() != source['sha256']: raise ValueError('component snapshot hash mismatch')
        else:
            body, meta = fetcher.get(url)
            sha = hashlib.sha256(body).hexdigest()
            blob = root / 'blobs' / sha[:2] / (sha + '.js')
            if not blob.exists(): atomic_bytes(blob, body)
            source = {'source_url': meta['final_url'], 'requested_url': url, 'sha256': sha,
                'snapshot_path': str(blob.relative_to(root)), 'observed_at': utc_now(),
                'parent_url': page['source_url'], 'category': page['category'], 'http': meta,
                'method': 'official_component_literal_no_execution'}
        data = extract(body.decode(), columns)
        db.execute('INSERT OR IGNORE INTO component_sources VALUES(?,?)', (source['sha256'], json.dumps(source)))
        for name, fields in data.items():
            if ('GeForce ' + name).casefold() in existing_models:
                continue
            key = identifier('nvidia:geforce:' + name)
            rows = [[{'text': str(v), 'colspan': 1, 'rowspan': 1, 'header': False} for v in (field, value)] for field, value in fields.items()]
            product = {'id': key, 'name': 'NVIDIA GeForce ' + name, 'category': page['category'],
                'kind': 'named_product', 'availability': 'not_verified',
                'identity_status': 'official_comparison_column', 'source_url': source['source_url'],
                'source_sha256': source['sha256'], 'product_url': page['source_url'],
                'observed_at': source['observed_at'], 'attachments': [{'url': page['source_url'], 'label': '官方产品页（动态规格所在页面）'}],
                'extraction_status': 'partial_official_component_fields',
                'tables': [{'index': 1, 'section': name + ' — 官方动态规格组件', 'rows': rows,
                    'method': source['method'], 'is_specification': True,
                    'notes': '原厂组件中的参数字面值，未执行下载脚本。仅覆盖组件列出的字段，完整规格及性能条件仍待核查；不能据此确认在售或跨型号性能等价。'}]}
            db.execute('INSERT OR REPLACE INTO component_products VALUES(?,?)', (key, json.dumps(product, ensure_ascii=False)))
        db.commit()
        print(json.dumps({'component': url, 'products': len(data), 'fields': sum(map(len, data.values()))}))
    result = export(db, base)
    db.close()
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', default='~/.local/share/fetchspec')
    ap.add_argument('--refresh', action='store_true', help='Observe component again; retain immutable prior snapshots')
    args = ap.parse_args()
    print(json.dumps(run(args.out, refresh=args.refresh), ensure_ascii=False))


if __name__ == '__main__':
    main()
