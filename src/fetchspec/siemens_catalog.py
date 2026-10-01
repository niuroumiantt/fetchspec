"""Siemens catalog PDFs: technical-data pages kept as layout rows, never re-columned.

Siemens catalogs and brochures (HA 25.73 NXAIR, SIVACON S8plus, SIVACON 8PS …) mark each
technical-data page with a running header line that starts with "Technical data", followed on
the same line or the next by the section ("Electrical data", "LI system", "Dimensions").
Their tables wrap labels over several lines, carry a separate unit column and let one value
span several rating columns. Re-building that grid would mean guessing which column a value
belongs to, so this parser does not: each non-empty line of ``pdftotext -layout`` becomes one
row, split only at runs of two or more spaces, and each cell keeps its start column ``x`` on
the page. A reviewer reads the row next to the page and names the column in ``map-field``'s
condition. Footnotes go to the table notes; page footers and figure reference codes are dropped.
"""
import re
import shutil
import subprocess

HEADER = re.compile(r'^Technical data(?:\s{2,}(?P<section>\S.*))?$')
SKIP_SECTIONS = re.compile(r'product range|room planning|typical for|project checklist|contents', re.I)
FOOTER = re.compile(r'·\s*Siemens\s+[A-Z]{2,3}\s*[\d.]+|^\d{1,3}\s+Technical data$|^Technical data\s+\d{1,3}$')
FIGURE = re.compile(r'^R-[A-Z]{2}\d{2}-\d{2,4}[a-z]?(?:\s+\w{2,4})?$')
FOOTNOTE = re.compile(r'^\d{1,2}\)\s+\S')
SEGMENT = re.compile(r'\S(?:.*?\S)??(?=\s{2,}|$)')  # shortest run up to a 2+ space gap
CONTROL = re.compile(r'[\x00-\x08\x0b\x0e-\x1f]')
METHOD = 'siemens_catalog_pdf_layout_rows'


def _segments(line):
    # pdftotext leaves backspaces and soft hyphens from the catalog's typesetting; they are not text.
    line = CONTROL.sub(' ', line).replace('\xad', '')
    return [(m.start(), m.group()) for m in SEGMENT.finditer(line)]


def tables_from_text(text):
    """Tables from pdftotext -layout output that still contains form feeds between pages."""
    tables = []
    for number, page in enumerate(text.split('\f'), start=1):
        lines = page.splitlines()
        top = [i for i, line in enumerate(lines[:12]) if HEADER.match(line.strip())]
        if not top:
            continue
        start = top[0]
        section = HEADER.match(lines[start].strip()).group('section') or ''
        body = lines[start + 1:]
        if not section:
            first = next((i for i, line in enumerate(body) if line.strip()), None)
            if first is not None and len(_segments(body[first])) == 1 and len(body[first].strip()) <= 80:
                section, body = body[first].strip(), body[first + 1:]
        section = ' '.join(section.split())
        if SKIP_SECTIONS.search(section):
            continue
        rows, notes = [], []
        for line in body:
            stripped = line.strip()
            if not stripped or FOOTER.search(stripped):
                continue
            if FOOTNOTE.match(stripped):
                notes.append(' '.join(stripped.split()))
                continue
            cells = [{'text': text, 'header': False, 'colspan': 1, 'rowspan': 1, 'x': x}
                     for x, text in _segments(line) if not FIGURE.match(text)]
            if cells:
                rows.append(cells)
        # A data page has several label/value lines carrying numbers; drawings and prose do not.
        if sum(1 for r in rows if len(r) >= 2 and any(re.search(r'\d', c['text']) for c in r[1:])) < 3:
            continue
        tables.append({'index': len(tables) + 1, 'page': number,
                       'section': f'p.{number} · {section or "Technical data"}', 'rows': rows,
                       'notes': '\n'.join(notes + [
                           'Siemens 样本技术数据页的版面行：每行按两个以上空格切分，x 为该段在版面中的起始字符列；'
                           '跨行的标签与跨列的数值保持原样，不推断所属列。']),
                       'is_specification': True, 'method': METHOD, 'extraction_method': METHOD})
    return tables


def tables_from_pdf(path):
    tool = shutil.which('pdftotext')
    if not tool:
        return []
    result = subprocess.run([tool, '-layout', str(path), '-'], capture_output=True, text=True, timeout=120, check=False)
    if result.returncode or not result.stdout.strip():
        return []
    return tables_from_text(result.stdout)
