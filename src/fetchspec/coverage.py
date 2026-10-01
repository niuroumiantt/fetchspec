"""Per-target rollout view: which adapter covers a target and how far it has moved.

Read-only. Stages are local facts (adapter declared, explicit binding, package,
receipt per environment); upstream Git status is copied from the target snapshot
and never inferred from them.
"""
from contextlib import closing
import csv
import json
import re
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


def build(root, snapshot, seed_dir=None):
    from .seeds import by_target as seeds_by_target, load as load_seeds
    root = Path(root).expanduser()
    parts = adapter_parts()
    seeded = seeds_by_target(load_seeds(seed_dir))
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
                        'seeds': sorted(seed['url'] for seed in seeded.get(tid, [])),
                        'bound_products': sorted(bindings.get(tid, ())), 'deliveries': sorted(packaged.get(tid, ())),
                        'receipt_environments': environments, 'stage': stage})
    summary = {stage: sum(r['stage'] == stage for r in records) for stage in STAGES}
    summary['seeded'] = sum(bool(r['seeds']) for r in records)
    uncovered = sorted({r['part_id'] for r in records if r['stage'] == 'no_adapter' and r['part_id']})
    return {'schema_version': 1, 'snapshot_id': snapshot['snapshot_id'], 'upstream_commit': snapshot['upstream']['commit'],
            'targets': len(records), 'summary': summary, 'adapter_parts': parts, 'parts_without_adapter': uncovered,
            'records': records, 'authority': 'local rollout view; upstream status is copied, never inferred'}


def write_csv(report, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ['target_id', 'part_id', 'variable_class', 'upstream_status', 'stage', 'adapters', 'seeds',
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


TARGET_ID = re.compile(r'^[A-Za-z][A-Za-z0-9_.-]{0,199}$')
UPSTREAM_STATUSES = {'sourced', 'assumed', 'delivered', 'needed'}


def load_backflow(source, snapshot, *, timeout=20):
    """Read inresearch's per-target backflow (docs/upstream/backflow-request.md) from a file or https URL.

    Untrusted input: wrong team or schema rejects the whole document; malformed rows and rows
    not owned by Fetchspec in the current snapshot are dropped, as inresearch does for /api/news.
    """
    text = str(source)
    if text.startswith('https://'):
        from urllib.request import Request, urlopen
        with urlopen(Request(text, headers={'Accept': 'application/json'}), timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError('backflow document exceeds 8 MiB')
    elif '://' in text:
        raise ValueError('backflow source must be a local file or an https URL')
    else:
        raw = Path(text).expanduser().read_bytes()
    document = json.loads(raw)
    if (not isinstance(document, dict) or document.get('schema_version') != 1 or document.get('team') != 'fetchspec'
            or not isinstance(document.get('by_target'), dict)):
        raise ValueError('backflow is not a schema_version 1 document for team fetchspec')
    owned = {row['id'] for row in snapshot['targets']}
    rows, dropped = {}, 0
    for target_id, row in document['by_target'].items():
        if (not isinstance(target_id, str) or not TARGET_ID.fullmatch(target_id) or target_id not in owned
                or not isinstance(row, dict) or row.get('status') not in UPSTREAM_STATUSES
                or type(row.get('received_items', 0)) is not int or row.get('received_items', 0) < 0
                or not isinstance(row.get('companies', []), list)
                or any(not isinstance(c, str) or len(c) > 80 for c in row.get('companies', []))):
            dropped += 1
            continue
        rows[target_id] = {'status': row['status'], 'received_items': row.get('received_items', 0),
                           'last_received_at': row.get('last_received_at') if isinstance(row.get('last_received_at'), str) else None,
                           'companies': sorted(set(row.get('companies', [])))}
    return {'generated_at': document.get('generated_at') if isinstance(document.get('generated_at'), str) else None,
            'targets_sha256': document.get('targets_sha256') if isinstance(document.get('targets_sha256'), str) else None,
            'by_target': rows, 'dropped': dropped}


def plan(report, limit=40, backflow=None):
    """Rank what to do next for each target: the demand side of the pipeline.

    Order: rows already moving, then rows that can be collected now (a reviewed seed page,
    or an adapter whose vendor is named in the row's own instances), then rows needing
    review or a new adapter. Within a group,
    earlier next_due and higher sensitivity (lower rank) come first.
    """
    upstream = (backflow or {}).get('by_target', {})

    def action(row):
        flow = upstream.get(row['target_id'])
        if flow and flow['received_items'] and flow['status'] == 'needed':
            return 0, 'inresearch received ' + str(flow['received_items']) + ' item(s); waiting for the author\'s deliveries import'
        if row['stage'] in NEXT:
            return 0, NEXT[row['stage']]
        if row['seeds']:
            return 1, f"collect-seeds --target {row['target_id']} --bind ({len(row['seeds'])} reviewed seed page(s)), then map-field"
        if row['instance_adapters']:
            return 1, ('collect --company ' + ' | '.join(row['instance_adapters'])
                       + ' from an official product page, then bind; no reviewed seed yet (seeds/<company>.json)')
        if row['adapters']:
            return 2, 'adapter covers the part but the named vendors differ: review instances or pick an official page from ' + ', '.join(row['adapters'])
        return 3, 'no adapter: add one for a vendor named in instances'
    ranked, closed_upstream = [], 0
    for row in report['records']:
        flow = upstream.get(row['target_id'])
        status = flow['status'] if flow else row['upstream_status']  # backflow is newer than the snapshot
        if row['stage'] == 'received' or status in {'sourced', 'assumed'}:
            continue
        if status == 'delivered':
            closed_upstream += 1
            continue
        group, text = action(row)
        ranked.append({'group': group, 'action': text, **{k: row[k] for k in (
            'target_id', 'stage', 'next_due', 'sensitivity_rank', 'instances', 'seeds', 'model_inputs', 'parameter_hints')},
            **({'upstream': flow} if flow else {})})
    ranked.sort(key=lambda r: (r['group'], r['next_due'] or '9999', r['sensitivity_rank'] or 99, r['target_id']))
    groups = {name: sum(r['group'] == i for r in ranked) for i, name in enumerate(('in_flight', 'collect_now', 'review_instances', 'new_adapter'))}
    result = {'snapshot_id': report['snapshot_id'], 'actionable': len(ranked), 'groups': groups, 'queue': ranked[:limit],
              'skipped': 'targets already sourced/assumed/delivered upstream or received in production'}
    if backflow is not None:
        result['backflow'] = {'generated_at': backflow.get('generated_at'), 'rows': len(upstream),
                              'dropped': backflow.get('dropped', 0), 'delivered_upstream': closed_upstream}
    return result
