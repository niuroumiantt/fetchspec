"""Reviewed official seed pages per target row, and the batch run over them.

A seed says: this official product page answers these target rows, because <reason>.
Seeds live in Git (``seeds/<company>.json``) so every page choice is reviewable. The batch
run collects each seed with a bounded request budget; with ``bind`` it binds only the
products found at that seed's own URL, using the seed's reason. Nothing is inferred
from names, and a seed whose targets left the current snapshot is skipped, not rewritten.
"""
import json
from pathlib import Path

from .adapters import ADAPTERS, adapter_for
from .inventory import ROOT, atomic_json
from .products import fingerprint

SEED_DIR = ROOT / 'seeds'
MAX_SEED_PAGES = 20


def load(seed_dir=None):
    """All seeds, validated against each company's adapter scope."""
    seeds = []
    for path in sorted(Path(seed_dir or SEED_DIR).glob('*.json')):
        document = json.loads(path.read_text(encoding='utf-8'))
        company = document.get('company_id')
        if company not in ADAPTERS or path.stem != company:
            raise ValueError(f'{path.name}: company_id must name a registered adapter and match the file name')
        adapter = adapter_for(company)
        for index, seed in enumerate(document.get('seeds', [])):
            where = f'{path.name} seed {index + 1}'
            url = adapter.normalize(seed.get('url', '')) if isinstance(seed.get('url'), str) else None
            targets = seed.get('targets')
            reason = seed.get('reason')
            pages = seed.get('max_pages', 3)
            if not url or not adapter.page_allowed(url):
                raise ValueError(f'{where}: url is not an allowed official product page for {company}')
            if (not isinstance(targets, list) or not targets or len(set(targets)) != len(targets)
                    or any(not isinstance(t, str) or not t for t in targets)):
                raise ValueError(f'{where}: targets must be a non-empty list of target IDs')
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 300:
                raise ValueError(f'{where}: a review reason (at most 300 characters) is required')
            if type(pages) is not int or not 1 <= pages <= MAX_SEED_PAGES:
                raise ValueError(f'{where}: max_pages must be 1..{MAX_SEED_PAGES}')
            seeds.append({'company_id': company, 'url': url, 'targets': sorted(targets), 'reason': reason.strip(),
                          'product': seed.get('product') if isinstance(seed.get('product'), str) else None,
                          'max_pages': pages, 'reviewed_at': document.get('reviewed_at')})
    return seeds


def by_target(seeds):
    index = {}
    for seed in seeds:
        for target in seed['targets']:
            index.setdefault(target, []).append(seed)
    return index


def collect_targets(root, store, snapshot, company, urls, targets, *, max_pages, refresh=False, reparse=False, fetcher=None):
    """The single collect path shared by `collect` and `collect-seeds`. Returns (result, payload)."""
    from .acquisition import collect
    from .targets import validate_target_ids
    validate_target_ids(snapshot, targets)
    if not 1 <= max_pages <= 1000:
        raise ValueError('max-pages must be between 1 and 1000')
    scope = {'snapshot_id': snapshot['snapshot_id'], 'company_id': company,
             'target_ids': sorted(set(targets)), 'urls': sorted(set(urls))}
    scope_id = fingerprint(scope)
    atomic_json(Path(root) / 'scopes' / (scope_id + '.json'), scope)
    payload = collect(root, company, urls, fetcher=fetcher, max_pages=max_pages, refresh=refresh, reparse=reparse,
                      known_catalog=store.export_catalog(company))
    result = store.ingest_catalog(payload, root)
    result.update(scope_id=scope_id, acquisition_report=payload.get('acquisition_report'),
                  binding='review explicit product IDs with bind before delivery')
    return result, payload


def _seed_products(payload, adapter, url):
    """Products observed at the seed URL itself (or its redirect target).

    Any kind counts: the seed is the human review, and a reviewed family page whose official
    comparison table lists the models (NVIDIA HGX) is a valid carrier. Binding still needs tables.
    """
    found = payload.get('products', [])
    def urls(product):
        seen = {product.get('source_url'), product.get('product_url')}
        seen.update(page.get('url') for page in product.get('official_pages', []))
        return {adapter.normalize(u) for u in seen if isinstance(u, str)}
    exact = [p for p in found if url in urls(p)]
    if exact:
        return exact
    finals = {s.get('source_url') for s in payload.get('sources', []) if s.get('requested_url') == url}
    redirected = [p for p in found if urls(p) & {adapter.normalize(u) for u in finals if u}]
    return redirected


def run(root, store, snapshot, seeds, *, budget=200, bind=False, refresh=False, dry_run=False, include_closed=False,
        fetcher=None):
    """Collect every selected seed within one request budget; optionally bind from seed reasons."""
    rows = {row['id']: row for row in snapshot['targets']}
    remaining, report = budget, []
    for seed in seeds:
        targets = [t for t in seed['targets'] if t in rows]
        entry = {'company_id': seed['company_id'], 'url': seed['url'], 'targets': targets, 'product': seed['product']}
        if not targets:
            report.append({**entry, 'status': 'skipped', 'why': 'targets not in the current snapshot'})
            continue
        if not include_closed and all(rows[t].get('status') != 'needed' for t in targets):
            report.append({**entry, 'status': 'skipped', 'why': 'targets already sourced, assumed or delivered upstream'})
            continue
        if dry_run:
            report.append({**entry, 'status': 'planned', 'max_pages': seed['max_pages']})
            continue
        if remaining < 1:
            report.append({**entry, 'status': 'skipped', 'why': 'request budget used up'})
            continue
        try:
            result, payload = collect_targets(root, store, snapshot, seed['company_id'], [seed['url']], targets,
                                              max_pages=min(seed['max_pages'], remaining), refresh=refresh,
                                              fetcher=fetcher)
        except (ValueError, OSError, KeyError) as exc:
            report.append({**entry, 'status': 'error', 'error': str(exc)})
            continue
        acquisition = result.get('acquisition_report') or {}
        remaining -= acquisition.get('attempted', 0)
        products = _seed_products(payload, adapter_for(seed['company_id']), seed['url'])
        with_tables = [p['id'] for p in products if p.get('tables')]
        entry.update(status='collected' if with_tables else 'no_tables' if products else 'no_product',
                     products=[{'id': p['id'], 'name': p['name'], 'kind': p.get('kind'), 'tables': len(p.get('tables', [])),
                                'extraction_status': p.get('extraction_status')} for p in products],
                     attempted=acquisition.get('attempted', 0), errors=acquisition.get('errors', []))
        if bind and with_tables:
            store.bind(snapshot, seed['company_id'], with_tables, targets, 'seed: ' + seed['reason'])
            entry['bound'] = {'products': with_tables, 'targets': targets}
        report.append(entry)
    counts = {}
    for entry in report:
        counts[entry['status']] = counts.get(entry['status'], 0) + 1
    return {'seeds': len(report), 'counts': counts, 'requests_used': budget - remaining, 'budget': budget,
            'bound_targets': sorted({t for e in report for t in e.get('bound', {}).get('targets', [])}),
            'results': report,
            'binding_rule': 'only products found at the seed URL with native tables; reason taken from the reviewed seed'}
