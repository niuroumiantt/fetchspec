"""Target-driven product evidence CLI. No command writes research facts."""
import argparse
import json
from pathlib import Path
import sys

from .adapters import ADAPTERS
from .inventory import atomic_json, utc_now
from .products import ProductStore, fingerprint
from .store import default_data_root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=None, help='new pipeline data root (defaults to configured Fetchspec root/pipeline)')
    sub = parser.add_subparsers(dest='command', required=True)
    sync = sub.add_parser('sync-targets', help='snapshot current inresearch demand and supply contract')
    sync.add_argument('--upstream', type=Path, required=True)
    migrate = sub.add_parser('migrate', help='read-only legacy import into this new data root')
    migrate.add_argument('--catalog', type=Path, required=True)
    migrate.add_argument('--archive-root', type=Path, required=True)
    migrate.add_argument('--history-db', type=Path)
    collect = sub.add_parser('collect', help='bounded official product/source refresh for explicit target demand')
    collect.add_argument('--company', required=True, choices=sorted(ADAPTERS))
    collect.add_argument('--url', action='append', required=True)
    collect.add_argument('--target', action='append', required=True)
    collect.add_argument('--max-pages', type=int, default=20)
    collection_mode = collect.add_mutually_exclusive_group()
    collection_mode.add_argument('--refresh', action='store_true')
    collection_mode.add_argument('--reparse', action='store_true')
    mapping_sync = sub.add_parser('map-sync', help='reconcile official product sitemap candidates without collecting pages')
    mapping_sync.add_argument('--company', required=True, choices=sorted(ADAPTERS))
    mapping_sync.add_argument('--target', action='append', required=True)
    map_ack = sub.add_parser('map-ack', help='acknowledge reviewed sitemap candidates after collection/review')
    map_ack.add_argument('--company', required=True, choices=sorted(ADAPTERS))
    map_ack.add_argument('--url', action='append', required=True)
    map_ack.add_argument('--reason', required=True)
    bind = sub.add_parser('bind', help='explicitly bind reviewed products to demand; no fuzzy inference')
    bind.add_argument('--company', required=True)
    bind.add_argument('--product', action='append', required=True)
    bind.add_argument('--target', action='append', required=True)
    bind.add_argument('--reason', required=True)
    package = sub.add_parser('package', help='build immutable v2 package from current bindings')
    package.add_argument('--company', required=True)
    package.add_argument('--product', action='append', required=True)
    package.add_argument('--delivery-id')
    receive = sub.add_parser('receipt', help='verify receiver acknowledgement against local package')
    receive.add_argument('--input', type=Path, required=True)
    receive.add_argument('--environment', default='receiver')
    proposal = sub.add_parser('author-proposal', help='export review proposal; never modifies author checkout')
    proposal.add_argument('--delivery-id', required=True)
    proposal.add_argument('--output', type=Path, required=True)
    proposal.add_argument('--environment', default='receiver')
    assignments = sub.add_parser('assignments', help='export inresearch deliveries-import input from a validated receipt')
    assignments.add_argument('--delivery-id', required=True)
    assignments.add_argument('--output', type=Path, required=True)
    assignments.add_argument('--environment', default='receiver')
    assignments.add_argument('--by', default='fetchspec')
    assignments.add_argument('--allow-validation', action='store_true', help='rehearsal only: accept local_receiver_validation receipts')
    export = sub.add_parser('export', help='export original cells and product map')
    export.add_argument('--company')
    export.add_argument('--directory', type=Path, required=True)
    mapping = sub.add_parser('map-field', help='record a reviewed comparison key without altering the raw cell')
    mapping.add_argument('--company', required=True)
    mapping.add_argument('--product', required=True)
    for key in ('table', 'row', 'cell'):
        mapping.add_argument('--' + key, type=int, required=True)
    for key in ('field', 'unit', 'condition', 'reviewer'):
        mapping.add_argument('--' + key, required=True)
    mapping.add_argument('--target', action='append', help='target rows this parameter answers; default all bound targets')
    listing = sub.add_parser('list', help='query candidate products by company/name/kind')
    listing.add_argument('--company', required=True)
    listing.add_argument('--name', default='')
    listing.add_argument('--kind')
    listing.add_argument('--limit', type=int, default=30)
    catalog_export = sub.add_parser('catalog', help='export structured candidate catalog; NVIDIA schema remains receiver compatible')
    catalog_export.add_argument('--company', required=True)
    catalog_export.add_argument('--output', type=Path, required=True)
    coverage = sub.add_parser('coverage', help='per-target rollout: adapter, binding, package, receipt')
    coverage.add_argument('--csv', type=Path)
    coverage.add_argument('--stage', choices=['no_adapter', 'adapter_ready', 'bound', 'packaged', 'received_validation_only', 'received'])
    planning = sub.add_parser('plan', help='demand queue: next action per open target, ranked by due date and sensitivity')
    planning.add_argument('--limit', type=int, default=40)
    sub.add_parser('status')
    args = parser.parse_args(argv)
    root = (args.root or (default_data_root() / 'pipeline')).expanduser().resolve()
    try:
        result = execute(args, root)
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({'error': str(exc), 'command': args.command}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    report = result.get('acquisition_report') or result
    return 1 if args.command in {'collect', 'map-sync'} and report.get('errors') else 0


def execute(args, root):
    from .targets import sync_targets, load_snapshot, validate_target_ids
    from .delivery_v2 import build_package, import_receipt, export_author_proposal, export_assignments
    if args.command == 'sync-targets':
        snapshot = sync_targets(args.upstream, root)
        return {'snapshot_id': snapshot['snapshot_id'], 'targets': len(snapshot['targets']), 'upstream': snapshot['upstream']}
    if args.command == 'receipt':
        return import_receipt(root, args.input, snapshot=load_snapshot(root), environment=args.environment)
    if args.command == 'author-proposal':
        return export_author_proposal(root, args.delivery_id, snapshot=load_snapshot(root), output_path=args.output, environment=args.environment)
    if args.command == 'plan':
        from .coverage import build as build_coverage, plan
        return plan(build_coverage(root, load_snapshot(root)), limit=max(1, args.limit))
    if args.command == 'coverage':
        from .coverage import build as build_coverage, write_csv
        report = build_coverage(root, load_snapshot(root))
        if args.csv:
            write_csv(report, args.csv)
        records = [r for r in report['records'] if not args.stage or r['stage'] == args.stage]
        return {k: report[k] for k in ('snapshot_id', 'upstream_commit', 'targets', 'summary', 'parts_without_adapter', 'authority')} | {
            'records': [{k: r[k] for k in ('target_id', 'stage', 'adapters', 'bound_products', 'receipt_environments')} for r in records]
            if args.stage else f'{len(records)} records; use --stage or --csv for rows'}
    if args.command == 'assignments':
        result = export_assignments(root, args.delivery_id, snapshot=load_snapshot(root), output_path=args.output,
                                    environment=args.environment, by=args.by, allow_validation=args.allow_validation)
        return {'output': str(args.output), 'records': len(result['records']),
                'target_ids': [r['target_id'] for r in result['records']], **result['source']}
    if args.command == 'migrate':
        archive = args.archive_root.resolve()
        if root == archive or root.is_relative_to(archive):
            raise ValueError('migration requires a separate data root, outside the legacy archive')
    with ProductStore(root) as store:
        if args.command == 'migrate':
            archive = args.archive_root.resolve()
            if root == archive or root.is_relative_to(archive):
                raise ValueError('migration requires a separate data root, outside the legacy archive')
            payload = json.loads(args.catalog.read_text())
            result = store.ingest_catalog(payload, archive)
            if args.history_db:
                result['historical_rows'] = store.import_legacy_history(args.history_db, payload['company_id'])
            result['counts'] = store.stats()
            return result
        if args.command == 'map-field':
            return store.map_field(args.company,args.product,args.table,args.row,args.cell,args.field,args.unit,args.condition,args.reviewer,
                                   target_ids=args.target, snapshot=load_snapshot(root) if args.target else None)
        if args.command == 'catalog':
            payload = store.export_catalog(args.company)
            atomic_json(args.output,payload)
            return {'output': str(args.output), 'products': len(payload['products']), 'authority': 'candidate_only'}
        if args.command == 'status':
            return store.stats()
        if args.command == 'list':
            products = store.export_catalog(args.company)['products']
            selected = [p for p in products if args.name.casefold() in p['name'].casefold() and (not args.kind or args.kind == p.get('kind'))]
            return {'matched': len(selected), 'products': [{k: p.get(k) for k in ('id', 'name', 'kind', 'parent_id', 'source_url', 'extraction_status')} for p in selected[:max(1, args.limit)]]}
        if args.command == 'export':
            return store.export_csv(args.directory, args.company)
        snapshot = load_snapshot(root)
        if args.command == 'map-sync':
            from .product_map import sync_map
            validate_target_ids(snapshot, args.target)
            result = sync_map(root,args.company,known_catalog=store.export_catalog(args.company))
            result['target_ids'] = args.target
            result['snapshot_id'] = snapshot['snapshot_id']
            plan = result.pop('plan')
            result['pending_candidates'] = len(plan)
            result['plan_preview'] = plan[:10]
            result['full_plan'] = str(root / 'acquisition' / args.company / 'map-plan.json')
            return result
        if args.command == 'map-ack':
            from .product_map import acknowledge_map
            if not args.reason.strip():
                raise ValueError('map acknowledgement needs a review reason')
            request = {'company_id': args.company, 'urls': sorted(set(args.url)), 'reason': args.reason, 'reviewed_at': utc_now()}
            atomic_json(root / 'map-reviews' / (fingerprint(request) + '.json'), request)
            return acknowledge_map(root,args.company,args.url)
        if args.command == 'bind':
            return store.bind(snapshot, args.company, args.product, args.target, args.reason)
        if args.command == 'collect':
            from .acquisition import collect
            validate_target_ids(snapshot, args.target)
            if not 1 <= args.max_pages <= 1000:
                raise ValueError('max-pages must be between 1 and 1000')
            scope = {'snapshot_id': snapshot['snapshot_id'], 'company_id': args.company,
                     'target_ids': sorted(set(args.target)), 'urls': sorted(set(args.url))}
            scope_id = fingerprint(scope)
            atomic_json(root / 'scopes' / (scope_id + '.json'), scope)
            payload = collect(root, args.company, args.url, max_pages=args.max_pages, refresh=args.refresh, reparse=args.reparse,
                              known_catalog=store.export_catalog(args.company))
            result = store.ingest_catalog(payload, root)
            result.update(scope_id=scope_id, acquisition_report=payload.get('acquisition_report'),
                          binding='review explicit product IDs with bind before delivery')
            return result
        if args.command == 'package':
            items = store.delivery_items(snapshot, args.company, args.product)
            return build_package(root, snapshot, args.company, items, delivery_id=args.delivery_id)
        raise ValueError('unknown command')


if __name__ == '__main__':
    sys.exit(main())
