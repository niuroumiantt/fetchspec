#!/usr/bin/env python3
"""Independent completeness check of a Fetchspec v2 package, before it leaves the collection host.

Deliberately does not import fetchspec: it re-reads the package bytes and checks what the
receiver and the author will rely on.

- ``SHA256SUMS`` lists exactly the files under ``files/`` and every hash matches;
- every manifest item's path, size and SHA match its file and its SHA256SUMS line;
- every target row on an item exists in the target table and belongs to team fetchspec;
- every parameter observation has all contract fields, cites the SHA of the very item it
  sits on, names a target on that item with the row's own part_id, and its value appears
  verbatim in that source (an official JSON component's ``details[].value``; for HTML the
  visible text with tags removed and entities decoded, including the HTML a Next.js page
  carries in its ``self.__next_f.push`` payload; for PDF the text ``pdftotext`` reads, plain
  and ``-layout``; otherwise the source bytes). Text sources compare with whitespace
  removed on both sides, since markup and layout move spaces (``V<span>DC</span>``);
- with ``--require-format``, every target row carries each listed format (e.g. the product
  page ``html`` and its ``json`` component).

Prints a per-target summary and exits 1 when anything is missing.
"""
import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

FIELDS = ('company_id', 'product_id', 'target_id', 'part_id', 'parameter_name', 'value', 'unit',
          'condition', 'source_url', 'source_sha256', 'observed_at')


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _visible_text(body):
    """HTML as a reader sees it: tags removed, entities decoded, whitespace collapsed."""
    text = re.sub(r'<(script|style)\b.*?</\1>', ' ', body.decode('utf-8', 'replace'), flags=re.S | re.I)
    return ' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', text)).split())


def _next_payload(body):
    """The HTML text a Next.js page pushes to ``self.__next_f`` (JSON string literals; never executed)."""
    parts = []
    for chunk in re.findall(r'self\.__next_f\.push\((\[.*?\])\)</script>', body.decode('utf-8', 'replace'), re.S):
        try:
            value = json.loads(chunk)
        except ValueError:
            continue
        parts.extend(v for v in value[1:] if isinstance(v, str))
    return _visible_text(''.join(parts).encode('utf-8')) if parts else ''


def _pdf_text(path):
    """pdftotext output, plain and -layout; None when Poppler is not installed."""
    tool = shutil.which('pdftotext')
    if not tool:
        return None
    return '\n'.join(subprocess.run([tool, *flags, str(path), '-'], capture_output=True, timeout=120).stdout.decode('utf-8', 'replace')
                     for flags in ([], ['-layout']))


def _squeeze(text):
    return re.sub(r'\s+', '', text)


def verify(package, targets_path, require_formats=()):
    package = Path(package)
    manifest = json.loads((package / 'manifest.json').read_text(encoding='utf-8'))
    rows = {t['id']: t for t in json.loads(Path(targets_path).read_text(encoding='utf-8'))['targets']}
    problems, per_target = [], {}
    sums = {}
    for line in (package / 'SHA256SUMS').read_text(encoding='utf-8').splitlines():
        if line.strip():
            digest, relative = line.split('  ', 1)
            sums[relative] = digest
    files = {p.relative_to(package).as_posix() for p in (package / 'files').rglob('*') if p.is_file()}
    if set(sums) != files:
        problems.append('SHA256SUMS and files/ differ: ' + ', '.join(sorted(set(sums) ^ files)))
    for relative, digest in sums.items():
        if (package / relative).is_file() and _sha((package / relative).read_bytes()) != digest:
            problems.append('hash mismatch: ' + relative)
    for item in manifest.get('items', []):
        body = (package / item['path']).read_bytes()
        if _sha(body) != item['sha256'] or len(body) != item['bytes'] or sums.get(item['path']) != item['sha256']:
            problems.append('item identity does not match its file: ' + item['path'])
        for target in item.get('target_ids', []):
            if rows.get(target, {}).get('team') != 'fetchspec':
                problems.append('target not owned by fetchspec: ' + target)
            entry = per_target.setdefault(target, {'formats': set(), 'items': 0, 'observations': []})
            entry['formats'].add(item['format'])
            entry['items'] += 1
        component, text = None, None
        if item['format'] == 'json':
            component = {d.get('value') for d in json.loads(body).get('details', []) if isinstance(d, dict)}
        elif item['format'] == 'html':
            text = _squeeze(_visible_text(body) + ' ' + _next_payload(body))
        elif item['format'] == 'pdf':
            pdf = _pdf_text(package / item['path'])
            if pdf is None and any(e.get('parameter_observations') for e in item.get('product_evidence', [])):
                problems.append('pdftotext (Poppler) is not installed; cannot check values in ' + item['path'])
            text = _squeeze(pdf or '')
        for evidence in item.get('product_evidence', []):
            for obs in evidence.get('parameter_observations', []):
                name = obs.get('parameter_name', '?')
                missing = [k for k in FIELDS if k not in obs]
                if missing:
                    problems.append(f'{name}: missing fields {missing}')
                    continue
                if obs['source_sha256'] != item['sha256']:
                    problems.append(f'{name}: cites a different file than the one it sits on')
                if obs['target_id'] not in item.get('target_ids', []):
                    problems.append(f'{name}: target {obs["target_id"]} is not on its item')
                if rows.get(obs['target_id'], {}).get('part_id') != obs['part_id']:
                    problems.append(f'{name}: part_id differs from the target row')
                if component is not None:
                    found = obs['value'] in component
                elif text is not None:
                    found = bool(_squeeze(obs['value'])) and _squeeze(obs['value']) in text
                else:
                    found = obs['value'].encode() in body
                if not found:
                    problems.append(f'{name}: value {obs["value"]!r} not found in its source')
                if obs['target_id'] in per_target:
                    per_target[obs['target_id']]['observations'].append(
                        f'{name}={obs["value"]}' + (f' [{obs["unit"]}]' if obs['unit'] else ''))
    for target, entry in sorted(per_target.items()):
        absent = set(require_formats) - entry['formats']
        if absent:
            problems.append(f'{target}: no {sorted(absent)} source')
    return {'delivery_id': manifest.get('delivery_id'), 'contract_version': manifest.get('contract_version'),
            'items': len(manifest.get('items', [])), 'files': len(files),
            'targets': {t: {'items': e['items'], 'formats': sorted(e['formats']), 'observations': e['observations']}
                        for t, e in sorted(per_target.items())},
            'problems': problems, 'complete': not problems}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('package', type=Path)
    parser.add_argument('--targets', type=Path, required=True,
                        help='tco_targets.json of the snapshot the package was built from')
    parser.add_argument('--require-format', action='append', default=[], metavar='FORMAT')
    args = parser.parse_args(argv)
    result = verify(args.package, args.targets, args.require_format)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['complete'] else 1


if __name__ == '__main__':
    sys.exit(main())
