"""Per-target rollout view: which adapter covers a target and how far it has moved.

Read-only. Stages are local facts (adapter declared, explicit binding, package,
receipt per environment); upstream Git status is copied from the target snapshot
and never inferred from them.
"""
from contextlib import closing
import csv
import json
from pathlib import Path
import sqlite3

from .adapters import ADAPTERS
from .inventory import load_profile

STAGES = ('no_adapter', 'adapter_ready', 'bound', 'packaged', 'received_validation_only', 'received')


def adapter_parts():
    """part_id -> adapters whose profile declares it; a declaration, not coverage proof."""
    parts = {}
    for company in sorted(ADAPTERS):
        for part in load_profile(company).get('target_parts', []):
            parts.setdefault(part, []).append(company)
    return parts


def _rows(path, query, args=()):
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in db.execute(query, args)]
        except sqlite3.OperationalError:
            return []


def build(root, snapshot):
    root = Path(root).expanduser()
    parts = adapter_parts()
    bindings = {}
    for row in _rows(root / 'products' / 'catalog.sqlite3',
                     'SELECT target_id,company_id,product_id FROM bindings WHERE snapshot_id=?', (snapshot['snapshot_id'],)):
        bindings.setdefault(row['target_id'], set()).add(row['company_id'] + ':' + row['product_id'])
    packaged, received = {}, {}
    ledger = root / 'delivery-ledger.sqlite'
    receipts = {}
    for row in _rows(ledger, 'SELECT delivery_id,environment FROM receipts'):
        receipts.setdefault(row['delivery_id'], set()).add(row['environment'])
    for row in _rows(ledger, 'SELECT delivery_id,package_path FROM packages'):
        manifest_path = Path(row['package_path']) / 'manifest.json'
        if not manifest_path.is_file():
            continue
        for target in json.loads(manifest_path.read_text()).get('target_ids', []):
            packaged.setdefault(target, set()).add(row['delivery_id'])
            for environment in receipts.get(row['delivery_id'], ()):
                received.setdefault(target, set()).add(environment)
    records = []
    for target in snapshot['targets']:
        tid, part = target['id'], target.get('part_id')
        adapters = parts.get(part, []) if part else []
        environments = sorted(received.get(tid, ()))
        production = [e for e in environments if e != 'local_receiver_validation']
        stage = ('received' if production else 'received_validation_only' if environments
                 else 'packaged' if tid in packaged else 'bound' if tid in bindings
                 else 'adapter_ready' if adapters else 'no_adapter')
        records.append({'target_id': tid, 'part_id': part, 'variable_class': target['variable_class'],
                        'upstream_status': target['status'], 'next_due': target.get('next_due'),
                        'instances': target.get('instances', []), 'adapters': adapters,
                        'bound_products': sorted(bindings.get(tid, ())), 'deliveries': sorted(packaged.get(tid, ())),
                        'receipt_environments': environments, 'stage': stage})
    summary = {stage: sum(r['stage'] == stage for r in records) for stage in STAGES}
    uncovered = sorted({r['part_id'] for r in records if r['stage'] == 'no_adapter' and r['part_id']})
    return {'schema_version': 1, 'snapshot_id': snapshot['snapshot_id'], 'upstream_commit': snapshot['upstream']['commit'],
            'targets': len(records), 'summary': summary, 'adapter_parts': parts, 'parts_without_adapter': uncovered,
            'records': records, 'authority': 'local rollout view; upstream status is copied, never inferred'}


def write_csv(report, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ['target_id', 'part_id', 'variable_class', 'upstream_status', 'stage', 'adapters',
              'bound_products', 'deliveries', 'receipt_environments', 'next_due', 'instances']
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in report['records']:
            writer.writerow({k: ';'.join(row[k]) if isinstance(row[k], list) else row[k] for k in fields})
