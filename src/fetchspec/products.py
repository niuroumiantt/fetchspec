"""Product evidence store. Raw vendor values are immutable candidate observations.

The current product projection, historical product payloads, byte identities,
source observations and original table cells have separate identities. Importing
an incomplete catalog never declares that a missing product has disappeared.
"""
import csv
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from urllib.parse import urlsplit

from .inventory import utc_now


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def semantic(value):
    if isinstance(value, dict):
        return {k: semantic(v) for k, v in value.items()
                if k not in {'observed_at', 'generated_at', 'map_change_status', 'checked_at'}}
    return [semantic(v) for v in value] if isinstance(value, list) else value


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def safe_csv(value):
    text = str(value) if value is not None else ''
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else text


def source_language(url):
    p = urlsplit(url)
    return 'zh' if (p.hostname or '').endswith('.cn') or re.search(r'/(?:zh(?:-cn|-tw|-hans)?|chinese)/', p.path, re.I) else 'en'


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, ValueError) as exc:
        raise ValueError('an ISO observed_at timestamp is required') from exc
    if parsed.tzinfo is None:
        raise ValueError('observed_at must have a timezone')
    return parsed


def evidence_refs(product):
    """Exact provenance, including supplemental tables; never match by title."""
    result = {(product['source_url'], product['source_sha256'])}
    for source in product.get('official_pages', []):
        if source.get('sha256') and source.get('url'):
            result.add((source['url'], source['sha256']))
    for table in product.get('tables', []):
        for source in table.get('source_refs', []):
            if source.get('sha256') and source.get('url'):
                result.add((source['url'], source['sha256']))
    for source in product.get('attachments', []):
        if source.get('sha256') and source.get('url'):
            result.add((source['url'], source['sha256']))
    return sorted(result)


class ProductStore:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        (self.root / 'products').mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'products/catalog.sqlite3', timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS blobs(sha256 TEXT PRIMARY KEY, path TEXT NOT NULL, bytes INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(company_id TEXT, url TEXT, sha256 TEXT REFERENCES blobs,
          observed_at TEXT, format TEXT NOT NULL, language TEXT NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(company_id,url,sha256,observed_at));
        CREATE TABLE IF NOT EXISTS product_versions(company_id TEXT, product_id TEXT, version TEXT,
          observed_at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(company_id,product_id,version));
        CREATE TABLE IF NOT EXISTS products(company_id TEXT, product_id TEXT, version TEXT,
          observed_at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(company_id,product_id),
          FOREIGN KEY(company_id,product_id,version) REFERENCES product_versions);
        CREATE TABLE IF NOT EXISTS relations(company_id TEXT, product_id TEXT, version TEXT, relation TEXT,
          target_id TEXT, PRIMARY KEY(company_id,product_id,version,relation,target_id),
          FOREIGN KEY(company_id,product_id,version) REFERENCES product_versions);
        CREATE TABLE IF NOT EXISTS spec_tables(company_id TEXT, product_id TEXT, version TEXT, table_index INTEGER,
          payload TEXT NOT NULL, PRIMARY KEY(company_id,product_id,version,table_index),
          FOREIGN KEY(company_id,product_id,version) REFERENCES product_versions);
        CREATE TABLE IF NOT EXISTS spec_cells(company_id TEXT, product_id TEXT, version TEXT,
          table_index INTEGER, row_index INTEGER, cell_index INTEGER, text TEXT, payload TEXT NOT NULL,
          PRIMARY KEY(company_id,product_id,version,table_index,row_index,cell_index),
          FOREIGN KEY(company_id,product_id,version,table_index) REFERENCES spec_tables);
        CREATE TABLE IF NOT EXISTS changes(id INTEGER PRIMARY KEY, company_id TEXT, product_id TEXT,
          old_version TEXT, new_version TEXT, observed_at TEXT NOT NULL, reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bindings(snapshot_id TEXT, target_id TEXT, company_id TEXT, product_id TEXT,
          part_id TEXT, reason TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(snapshot_id,target_id,company_id,product_id),
          FOREIGN KEY(company_id,product_id) REFERENCES products);
        CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY, company_id TEXT, imported_at TEXT, receipt TEXT);
        CREATE TABLE IF NOT EXISTS catalog_snapshots(id TEXT PRIMARY KEY, company_id TEXT, metadata TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS historical_evidence(origin TEXT, table_name TEXT, record_hash TEXT,
          payload TEXT NOT NULL, PRIMARY KEY(origin,table_name,record_hash));
        CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY, operation TEXT, context TEXT, error TEXT, observed_at TEXT);
        CREATE TABLE IF NOT EXISTS comparisons(company_id TEXT, product_id TEXT, version TEXT,
          table_index INTEGER,row_index INTEGER,cell_index INTEGER,field TEXT,unit TEXT,condition TEXT,reviewer TEXT,
          PRIMARY KEY(company_id,product_id,version,table_index,row_index,cell_index,field));
        ''')

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _place(self, source, sha):
        """Content address has no extension, so equal bytes occupy one file."""
        target = self.root / 'blobs' / sha[:2] / sha
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if file_sha(target) != sha:
                raise ValueError(f'corrupt existing blob: {sha}')
            return target
        fd, name = tempfile.mkstemp(prefix='.incoming-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as stream, source.open('rb') as inp:
                shutil.copyfileobj(inp, stream)
                stream.flush()
                os.fsync(stream.fileno())
            if file_sha(name) != sha:
                raise ValueError(f'source changed while copying: {sha}')
            try:
                os.link(name, target)  # exclusive publish; never overwrite existing evidence
            except FileExistsError:
                if file_sha(target) != sha:
                    raise ValueError(f'corrupt concurrent blob: {sha}')
        finally:
            Path(name).unlink(missing_ok=True)
        return target

    def ingest_catalog(self, payload, archive_root):
        """Verify the complete batch before committing. Missing products are retained."""
        if not isinstance(payload, dict):
            raise ValueError('catalog must be an object')
        company = payload.get('company_id')
        products, sources = payload.get('products'), payload.get('sources')
        if not isinstance(company, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', company):
            raise ValueError('invalid company_id')
        if not isinstance(products, list) or not isinstance(sources, list):
            raise ValueError('catalog must contain products and sources arrays')
        if any(not isinstance(p, dict) for p in products) or any(not isinstance(s, dict) for s in sources):
            raise ValueError('products and sources must contain objects')
        ids = [p.get('id') for p in products]
        if any(not isinstance(p, str) or not p for p in ids) or len(set(ids)) != len(ids):
            raise ValueError('product IDs must be nonempty and unique')
        archive = Path(archive_root).resolve()
        verified, source_keys = {}, set()
        for source in sources:
            sha, url = source.get('sha256'), source.get('source_url')
            if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
                raise ValueError('invalid source SHA256')
            if not isinstance(url, str) or urlsplit(url).scheme != 'https' or not urlsplit(url).hostname:
                raise ValueError('source URL must be HTTPS')
            timestamp(source.get('observed_at'))
            if not isinstance(source.get('snapshot_path'), str):
                raise ValueError('snapshot_path must be a string')
            path = (archive / source['snapshot_path']).resolve()
            if not path.is_relative_to(archive) or not path.is_file():
                raise ValueError('snapshot is missing or outside archive root')
            if path not in verified:
                if file_sha(path) != sha:
                    raise ValueError(f'snapshot SHA mismatch: {sha}')
                verified[path] = sha
            elif verified[path] != sha:
                raise ValueError('one path cannot identify multiple byte versions')
            source_keys.add((url, sha))
        existing = {r[0] for r in self.db.execute('SELECT product_id FROM products WHERE company_id=?', (company,))}
        for product in products:
            timestamp(product.get('observed_at'))
            if not isinstance(product.get('name'), str) or not isinstance(product.get('tables', []), list):
                raise ValueError('invalid product name/tables')
            if product.get('parent_id') and product['parent_id'] not in existing | set(ids):
                raise ValueError(f'unknown parent: {product["parent_id"]}')
            if (product.get('source_url'), product.get('source_sha256')) not in source_keys:
                raise ValueError(f'product lacks verified primary source: {product["id"]}')
            for table in product.get('tables', []):
                if not isinstance(table, dict) or not isinstance(table.get('rows'), list):
                    raise ValueError('table rows must be an array')
                for row in table['rows']:
                    if not isinstance(row, list) or any(not isinstance(c, dict) or not isinstance(c.get('text'), str) for c in row):
                        raise ValueError('table cells must preserve text objects')
        parents = {r[0]: json.loads(r[1]).get('parent_id') for r in self.db.execute('SELECT product_id,payload FROM products WHERE company_id=?', (company,))}
        parents.update({p['id']: p.get('parent_id') for p in products})
        for pid in ids:
            seen, current = set(), pid
            while current is not None:
                if current in seen:
                    raise ValueError('cyclic product parent relation')
                seen.add(current)
                current = parents.get(current)
        batch_id = fingerprint(payload)
        receipt = {'import_id': batch_id, 'company_id': company, 'products_in_batch': len(products),
                   'sources_in_batch': len(sources), 'new_products': 0, 'changed_products': 0,
                   'unchanged_products': 0, 'stale_products': 0}
        # Complete bytes may outlive a failed SQL transaction; retry verifies/reuses them.
        placed = {sha: self._place(path, sha) for path, sha in verified.items()}
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for sha, path in placed.items():
                self.db.execute('INSERT OR IGNORE INTO blobs VALUES(?,?,?)',
                                (sha, str(path.relative_to(self.root)), path.stat().st_size))
            for source in sources:
                ext = source.get('format') or Path(source['snapshot_path']).suffix.lstrip('.') or 'html'
                self.db.execute('INSERT OR IGNORE INTO sources VALUES(?,?,?,?,?,?,?)',
                    (company, source['source_url'], source['sha256'], source['observed_at'], ext,
                     source.get('language') or source_language(source['source_url']), encoded(source)))
            for product in products:
                pid, observed = product['id'], product['observed_at']
                version = fingerprint(semantic(product))
                old = self.db.execute('SELECT * FROM products WHERE company_id=? AND product_id=?', (company, pid)).fetchone()
                raw = encoded(product)
                self.db.execute('INSERT OR IGNORE INTO product_versions VALUES(?,?,?,?,?)', (company, pid, version, observed, raw))
                if product.get('parent_id'):
                    self.db.execute('INSERT OR IGNORE INTO relations VALUES(?,?,?,?,?)', (company, pid, version, 'parent', product['parent_id']))
                for ti, table in enumerate(product.get('tables', []), 1):
                    self.db.execute('INSERT OR IGNORE INTO spec_tables VALUES(?,?,?,?,?)', (company, pid, version, ti, encoded(table)))
                    for ri, row in enumerate(table['rows'], 1):
                        for ci, cell in enumerate(row, 1):
                            self.db.execute('INSERT OR IGNORE INTO spec_cells VALUES(?,?,?,?,?,?,?,?)',
                                            (company, pid, version, ti, ri, ci, cell['text'], encoded(cell)))
                if old and timestamp(observed) < timestamp(old['observed_at']):
                    receipt['stale_products'] += 1
                    continue
                if old and version == old['version']:
                    receipt['unchanged_products'] += 1
                    self.db.execute('UPDATE products SET observed_at=?,payload=? WHERE company_id=? AND product_id=?', (observed, raw, company, pid))
                    continue
                if old and timestamp(observed) == timestamp(old['observed_at']) and version != old['version']:
                    raise ValueError(f'conflicting product versions at same observed_at: {pid}')
                self.db.execute('''INSERT INTO products VALUES(?,?,?,?,?) ON CONFLICT(company_id,product_id)
                    DO UPDATE SET version=excluded.version,observed_at=excluded.observed_at,payload=excluded.payload''',
                    (company, pid, version, observed, raw))
                receipt['changed_products' if old else 'new_products'] += 1
                self.db.execute('INSERT INTO changes(company_id,product_id,old_version,new_version,observed_at,reason) VALUES(?,?,?,?,?,?)',
                    (company, pid, old['version'] if old else None, version, observed, 'source or product/specification payload changed'))
            self.db.execute('INSERT OR IGNORE INTO imports VALUES(?,?,?,?)', (batch_id, company, utc_now(), encoded(receipt)))
            self.db.execute('INSERT OR IGNORE INTO catalog_snapshots VALUES(?,?,?)', (batch_id, company, encoded({k:v for k,v in payload.items() if k not in {'products','sources'}})))
        return receipt

    def export_catalog(self, company_id):
        products = [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM products WHERE company_id=? ORDER BY product_id', (company_id,))]
        sources = []
        for row in self.db.execute('SELECT s.*,b.path FROM sources s JOIN blobs b USING(sha256) WHERE company_id=? ORDER BY url,observed_at,sha256', (company_id,)):
            source = json.loads(row['payload'])
            source.update(snapshot_path=row['path'], format=row['format'], language=row['language'])
            sources.append(source)
        return {'schema_version': 1, 'company_id': company_id, 'generated_at': utc_now(),
                'products': products, 'sources': sources, 'authority': 'candidate_only',
                'coverage': {'complete': False, 'entity_counts': dict(Counter(p.get('kind') for p in products)),
                             'with_spec_tables': sum(bool(p.get('tables')) for p in products),
                             'limitations': ['Current union of observed products; partial missing entries retained; no shipping/completeness claim.']},
                'product_map': {'entries': len(products), 'policy': 'Partial absence never retires a product.'}}

    def bind(self, snapshot, company_id, product_ids, target_ids, reason):
        from .targets import validate_target_ids
        targets = validate_target_ids(snapshot, target_ids)
        if not product_ids or not reason.strip():
            raise ValueError('explicit product IDs and binding reason are required')
        with self.db:
            for pid in product_ids:
                if not self.db.execute('SELECT 1 FROM products WHERE company_id=? AND product_id=?', (company_id, pid)).fetchone():
                    raise ValueError(f'unknown product: {pid}')
                for target in targets:
                    self.db.execute('INSERT OR IGNORE INTO bindings VALUES(?,?,?,?,?,?,?)',
                        (snapshot['snapshot_id'], target['id'], company_id, pid, target.get('part_id'), reason, utc_now()))
        return {'products': len(set(product_ids)), 'targets': [t['id'] for t in targets], 'snapshot_id': snapshot['snapshot_id']}

    def delivery_items(self, snapshot, company_id, product_ids):
        from .targets import validate_target_ids
        if not product_ids:
            raise ValueError('choose specific products before delivery')
        catalog = self.export_catalog(company_id)
        products = {p['id']: p for p in catalog['products']}
        sources = {}
        for source in catalog['sources']:
            # Rechecking identical bytes records a new observation, not a new delivery.
            sources.setdefault((source['source_url'], source['sha256']), source)
        items = []
        for pid in sorted(set(product_ids)):
            if pid not in products:
                raise ValueError(f'unknown product: {pid}')
            target_ids = [r[0] for r in self.db.execute('SELECT target_id FROM bindings WHERE snapshot_id=? AND company_id=? AND product_id=? ORDER BY target_id', (snapshot['snapshot_id'], company_id, pid))]
            validate_target_ids(snapshot, target_ids)
            product = products[pid]
            for url, sha in evidence_refs(product):
                source = sources.get((url, sha))
                if not source:
                    raise ValueError(f'unverified supplemental evidence for {pid}: {sha}')
                sequence = [r[0] for r in self.db.execute('SELECT sha256 FROM sources WHERE company_id=? AND url=? GROUP BY sha256 ORDER BY min(observed_at),sha256', (company_id, url))]
                index = sequence.index(sha)
                relation = {'type': 'new_version', 'supersedes_sha256': sequence[index - 1]} if index else {'type': 'original'}
                item = {'blob_path': str(self.root / source['snapshot_path']), 'sha256': sha, 'format': source['format'],
                        'source_item_id': company_id + ':' + hashlib.sha256(url.encode()).hexdigest()[:24],
                        'source': {'publisher': company_id, 'url': url, 'language': source['language'],
                                   'categories': [c for c in (product.get('categories') or [product.get('category')]) if isinstance(c, str) and c.strip()] or ['Unclassified'],
                                   'original_filename': Path(urlsplit(url).path).name},
                        'retrieved_at': source['observed_at'], 'target_ids': target_ids, 'product_ids': [pid],
                        'version_relation': relation,
                        'product_evidence': [{'product_id': pid, 'name': product['name'], 'parent_id': product.get('parent_id'),
                           'product_url': product.get('product_url'), 'target_ids': target_ids,
                           'part_ids': sorted({t['part_id'] for t in validate_target_ids(snapshot, target_ids) if t.get('part_id')}),
                           'version': fingerprint(semantic(product)), 'specification_tables': product.get('tables', [])}]}
                items.append(item)
        return items

    def map_field(self, company_id, product_id, table_index, row_index, cell_index, field, unit, condition, reviewer):
        """Attach a reviewed comparison label to an exact original cell; never convert it."""
        if not re.fullmatch(r'[a-z][a-z0-9_.-]*', field) or not reviewer.strip():
            raise ValueError('comparison field key and reviewer are required')
        with self.db:
            row = self.db.execute("""SELECT c.* FROM spec_cells c JOIN products p
                ON p.company_id=c.company_id AND p.product_id=c.product_id AND p.version=c.version
                WHERE c.company_id=? AND c.product_id=? AND table_index=? AND row_index=? AND cell_index=?""",
                (company_id, product_id, table_index, row_index, cell_index)).fetchone()
            if not row:
                raise ValueError('comparison must reference an existing current original cell')
            self.db.execute('INSERT OR REPLACE INTO comparisons VALUES(?,?,?,?,?,?,?,?,?,?)',
                (company_id, product_id, row['version'], table_index, row_index, cell_index, field, unit, condition, reviewer))
        return {'field': field, 'original_text': row['text'], 'unit': unit, 'condition': condition,
                'product_version': row['version'], 'authority': 'reviewed_mapping_candidate_only'}

    def export_csv(self, directory, company_id=None):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        rows = self.db.execute('SELECT * FROM products WHERE (? IS NULL OR company_id=?) ORDER BY company_id,product_id', (company_id, company_id)).fetchall()
        def write(name, fields, values):
            fd, temporary = tempfile.mkstemp(prefix='.csv-', dir=directory)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8-sig', newline='') as stream:
                    writer = csv.writer(stream)
                    writer.writerow(fields)
                    for row in values:
                        writer.writerow([safe_csv(v) for v in row])
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, directory / name)
            finally:
                Path(temporary).unlink(missing_ok=True)
        product_rows, cells = [], []
        for row in rows:
            p = json.loads(row['payload'])
            product_rows.append([row['company_id'], p['id'], p['name'], p.get('kind'), p.get('parent_id'), row['version'], p['source_url'], p['source_sha256'], p.get('availability'), len(p.get('tables', []))])
            bindings = [dict(b) for b in self.db.execute('SELECT target_id,part_id,snapshot_id FROM bindings WHERE company_id=? AND product_id=? ORDER BY snapshot_id,target_id', (row['company_id'], p['id']))]
            for ti, table in enumerate(p.get('tables', []), 1):
                for ri, source_row in enumerate(table['rows'], 1):
                    for ci, cell in enumerate(source_row, 1):
                        cells.append([row['company_id'], p['id'], p['name'], row['version'], ti, table.get('section'), ri, ci, cell['text'], cell.get('rowspan', 1), cell.get('colspan', 1), cell.get('header', False), table.get('notes', ''), table.get('extraction_method', table.get('method', 'unknown')), encoded(table.get('source_refs') or [{'url': p['source_url'], 'sha256': p['source_sha256']}]), encoded(bindings)])
        write('products.csv', ['company_id','product_id','name','kind','parent_id','version','source_url','source_sha256','availability','table_count'], product_rows)
        write('spec_cells.csv', ['company_id','product_id','name','version','table_index','section','row_index','cell_index','original_text','rowspan','colspan','header','notes','method','source_refs','target_bindings'], cells)
        mappings = self.db.execute('''SELECT m.company_id,m.product_id,m.version,m.table_index,m.row_index,m.cell_index,
            m.field,c.text,m.unit,m.condition,m.reviewer,CASE WHEN p.version=m.version THEN 'current' ELSE 'historical' END
            FROM comparisons m JOIN spec_cells c USING(company_id,product_id,version,table_index,row_index,cell_index)
            JOIN products p ON p.company_id=m.company_id AND p.product_id=m.product_id
            WHERE (? IS NULL OR m.company_id=?) ORDER BY m.field,m.company_id,m.product_id,m.version''',
            (company_id,company_id)).fetchall()
        write('comparisons.csv', ['company_id','product_id','version','table_index','row_index','cell_index','field',
              'original_text','unit','condition','reviewer','version_status'], mappings)
        return {'products': len(rows), 'cells': len(cells), 'mappings': len(mappings), 'directory': str(directory)}

    def import_legacy_history(self, source_db, company_id):
        """Archive legacy rows without resurrecting their retired product identities."""
        path = Path(source_db).resolve()
        allowed = ('history', 'product_map', 'product_map_events', 'product_sitemap_urls', 'frontier', 'memberships')
        legacy = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        legacy.row_factory = sqlite3.Row
        counts = {}
        try:
            tables = {r[0] for r in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            with self.db:
                for table in allowed:
                    if table not in tables:
                        continue
                    rows = [dict(r) for r in legacy.execute('SELECT * FROM ' + table)]
                    for row in rows:
                        self.db.execute('INSERT OR IGNORE INTO historical_evidence VALUES(?,?,?,?)',
                            (f'{company_id}:{path}', table, fingerprint(row), encoded(row)))
                    counts[table] = len(rows)
        finally:
            legacy.close()
        return counts

    def stats(self):
        names = ('products', 'product_versions', 'sources', 'blobs', 'spec_tables', 'spec_cells', 'changes', 'bindings', 'historical_evidence')
        return {name: self.db.execute('SELECT count(*) FROM ' + name).fetchone()[0] for name in names}
