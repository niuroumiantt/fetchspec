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
# Suggested parameter keys per data class, taken from the target rows' own disclosure_type
# wording. They name what to look for in vendor tables; they are not values or mappings.
PARAMETER_HINTS = {
    'spec': ['model', 'form_factor', 'rated_capacity', 'rated_power', 'dimensions', 'weight', 'interfaces'],
    'operation': ['rated_power', 'power_share', 'efficiency_curve', 'pue_contribution', 'lifetime', 'mtbf', 'utilization'],
}


def instance_adapters(target):
    """Adapters whose company names appear in the row's own publisher instances."""
    text = ' '.join(target.get('instances', []) + [target.get('publisher_category') or '']).casefold()
    matched = []
    for company in sorted(ADAPTERS):
        profile = load_profile(company)
        names = {company, profile.get('company_en', '')} | set(profile.get('instance_aliases', []))
        if any(name and name.casefold() in text for name in names):
            matched.append(company)
    return matched


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
        kind = tid.rsplit('.', 1)[-1] if tid.startswith('P.') else 'factor'
        records.append({'target_id': tid, 'part_id': part, 'variable_class': target['variable_class'],
                        'upstream_status': target['status'], 'next_due': target.get('next_due'),
                        'sensitivity_rank': target.get('sensitivity_rank'), 'model_inputs': target.get('model_inputs', []),
                        'disclosure_type': target.get('disclosure_type'), 'data_kind': kind,
                        'parameter_hints': PARAMETER_HINTS.get(kind, []),
                        'instances': target.get('instances', []), 'adapters': adapters,
                        'instance_adapters': instance_adapters(target),
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


NEXT = {
    'received': 'done locally; upstream import is the author\'s step',
    'received_validation_only': 'send the package to the production receiver, then receipt --environment production',
    'packaged': 'transfer the package and import the receiver receipt',
    'bound': 'map-field the parameters this row asks for, then package',
}


def plan(report, limit=40):
    """Rank what to do next for each target: the demand side of the pipeline.

    Order: rows already moving, then rows an adapter can collect now (its vendor is named
    in the row's own instances), then rows needing review or a new adapter. Within a group,
    earlier next_due and higher sensitivity (lower rank) come first.
    """
    def action(row):
        if row['stage'] in NEXT:
            return 0, NEXT[row['stage']]
        if row['instance_adapters']:
            return 1, 'collect --company ' + ' | '.join(row['instance_adapters']) + ' from an official product page, then bind'
        if row['adapters']:
            return 2, 'adapter covers the part but the named vendors differ: review instances or pick an official page from ' + ', '.join(row['adapters'])
        return 3, 'no adapter: add one for a vendor named in instances'
    ranked = []
    for row in report['records']:
        if row['stage'] == 'received' or row['upstream_status'] in {'sourced', 'assumed'}:
            continue
        group, text = action(row)
        ranked.append({'group': group, 'action': text, **{k: row[k] for k in (
            'target_id', 'stage', 'next_due', 'sensitivity_rank', 'instances', 'model_inputs', 'parameter_hints')}})
    ranked.sort(key=lambda r: (r['group'], r['next_due'] or '9999', r['sensitivity_rank'] or 99, r['target_id']))
    groups = {name: sum(r['group'] == i for r in ranked) for i, name in enumerate(('in_flight', 'collect_now', 'review_instances', 'new_adapter'))}
    return {'snapshot_id': report['snapshot_id'], 'actionable': len(ranked), 'groups': groups, 'queue': ranked[:limit],
            'skipped': 'targets already sourced/assumed upstream or received in production'}
