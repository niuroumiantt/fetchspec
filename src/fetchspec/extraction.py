"""Deterministic native evidence extraction; no downloaded code is executed."""
import io
from pathlib import Path
import posixpath
import re
import tempfile
import zipfile
from xml.etree import ElementTree as ET

from .product_catalog import Document, Node, content_nodes, native_table, pdf_spec_tables


def cell(text, *, header=False, colspan=1, rowspan=1):
    return {'text': text, 'header': header, 'colspan': colspan, 'rowspan': rowspan}


def html_page(body, url):
    """Retain vendor tables and their captions/notes without mapping raw values."""
    root = Document(body.decode('utf-8', 'replace')).root
    nodes = list(content_nodes(root))
    heading = next((n.text() for n in nodes if n.tag == 'h1'), '')
    title = next((n.text() for n in root.walk('title')), '')
    section, tables, links = heading, [], []
    parents = {}
    for parent in root.walk():
        for child in parent.children:
            if isinstance(child, Node):
                parents[id(child)] = parent
    table_sections = {}
    for index, node in enumerate(nodes):
        if node.tag in {'h1', 'h2', 'h3', 'h4'}:
            section = node.text()
        if node.tag == 'a' and node.attrs.get('href'):
            links.append({'url': node.attrs['href'], 'label': node.text(), 'section': section})
        if node.tag != 'table':
            continue
        # Layout tables wrap real specification tables. Exclude descendant
        # table text from a parent's cells, while retaining its own labels.
        def pruned(current, top=False):
            if isinstance(current, str):
                return current
            if current.tag == 'table' and not top:
                return None
            children = [pruned(c) for c in current.children]
            return Node(current.tag, current.attrs, [c for c in children if c is not None])
        rows = [r for r in native_table(pruned(node, True)) if any(c['text'] for c in r)]
        if not rows:
            continue
        title_row = rows[0] if len(rows[0]) == 1 and len(rows[0][0]['text']) < 120 else None
        own_heading = title_row[0]['text'] if title_row and (title_row[0]['colspan'] > 1 or title_row[0]['header']) else ''
        if own_heading:
            table_sections[id(node)] = own_heading
        ancestor, ancestor_titles, spec_container = parents.get(id(node)), [], False
        while ancestor is not None:
            if id(ancestor) in table_sections:
                ancestor_titles.append(table_sections[id(ancestor)])
            if ancestor.tag == 'table' and 'spec-table' in ancestor.attrs.get('class', ''):
                spec_container = True
            ancestor = parents.get(id(ancestor))
        caption = next((n.text() for n in node.walk('caption')), '')
        table_section = caption or own_heading or ' / '.join(reversed(ancestor_titles)) or section
        if len(rows) == 1 and own_heading:
            continue
        context = table_section + ' ' + ' '.join(c['text'] for r in rows for c in r)
        relevant = spec_container or bool(re.search(r'specification|technical|规格|規格|processor|memory|form factor|电源|处理器|内存', context, re.I))
        descendants = {id(n) for n in node.walk()}
        notes = []
        for following in nodes[index + 1:]:
            if id(following) in descendants:
                continue
            if following.tag in {'h1', 'h2', 'h3', 'h4', 'table'}:
                break
            if following.tag == 'p' and following.text():
                notes.append(following.text())
        tables.append({'index': len(tables) + 1, 'section': table_section,
                       'rows': rows, 'notes': '\n'.join(notes), 'is_specification': relevant,
                       'method': 'html_table', 'html_class': node.attrs.get('class', '')})
    return {'heading': heading, 'title': title, 'links': links, 'tables': tables}


def _xml(archive, name):
    info = archive.getinfo(name)
    if info.file_size > 16 * 1024 * 1024:
        raise ValueError('Office XML exceeds extraction limit')
    return ET.fromstring(archive.read(name))


def _relationships(archive, part, rels_path):
    if rels_path not in archive.namelist():
        return {}
    result = {}
    for row in _xml(archive, rels_path):
        if row.get('TargetMode') == 'External':
            continue
        target = row.get('Target', '')
        path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(part, target))
        if not path.startswith(part + '/') or path not in archive.namelist():
            continue
        result[row.get('Id')] = path
    return result


def _office_review(table):
    return {**table, 'requires_review': True,
            'review_reason': 'Native Office cells preserved; applicability, formula caches, units and formatting require review.'}


def office_tables(body, kind):
    """Native DOCX and XLSX cells; formulas remain formulas, macros never run."""
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        if sum(i.file_size for i in archive.infolist()) > 128 * 1024 * 1024:
            raise ValueError('Office expanded content exceeds extraction limit')
        if kind in {'docx', 'docm'}:
            ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            root = _xml(archive, 'word/document.xml')
            tables = []
            for table in root.findall('.//w:tbl', ns):
                rows = []
                for row in table.findall('w:tr', ns):
                    cells = []
                    for node in row.findall('w:tc', ns):
                        span = node.find('w:tcPr/w:gridSpan', ns)
                        merged = node.find('w:tcPr/w:vMerge', ns)
                        text = '\n'.join(''.join(p.itertext()) for p in node.findall('w:p', ns))
                        value = cell(text, colspan=int(span.get('{'+ns['w']+'}val', '1')) if span is not None else 1)
                        if merged is not None:
                            value['vertical_merge'] = merged.get('{'+ns['w']+'}val', 'continue')
                        cells.append(value)
                    if cells:
                        rows.append(cells)
                tables.append({'index': len(tables)+1, 'section': 'Original document table',
                               'rows': rows, 'notes': 'Original Word table; applicability requires review.',
                               'method': 'office_docx_xml', 'is_specification': True})
            return [_office_review(t) for t in tables]
        if kind in {'xlsx', 'xlsm'}:
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            strings = []
            if 'xl/sharedStrings.xml' in archive.namelist():
                strings = [''.join(n.itertext()) for n in _xml(archive, 'xl/sharedStrings.xml').findall('s:si', ns)]
            tables = []
            sheet_names = {}
            relationships = _relationships(archive, 'xl', 'xl/_rels/workbook.xml.rels')
            if 'xl/workbook.xml' in archive.namelist():
                for sheet in _xml(archive, 'xl/workbook.xml').findall('s:sheets/s:sheet', ns):
                    rid = sheet.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
                    if rid in relationships:
                        sheet_names[relationships[rid]] = sheet.get('name', relationships[rid])
            for name in sorted(n for n in archive.namelist() if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n)):

                sheet = _xml(archive, name)
                rows = []
                for row in sheet.findall('s:sheetData/s:row', ns):
                    cells = []
                    for node in row.findall('s:c', ns):
                        raw = node.findtext('s:v', '', ns)
                        kind_ = node.get('t', '')
                        text = strings[int(raw)] if kind_ == 's' and raw else ''.join(node.find('s:is', ns).itertext()) if kind_ == 'inlineStr' and node.find('s:is', ns) is not None else raw
                        value = cell(text)
                        value['coordinate'] = node.get('r')
                        if node.get('s') is not None:
                            value['style_index'] = node.get('s')
                        formula = node.findtext('s:f', None, ns)
                        if formula is not None:
                            value['formula'] = formula
                        cells.append(value)
                    if cells:
                        rows.append(cells)
                tables.append({'index': len(tables)+1, 'section': sheet_names.get(name, name), 'sheet_name': sheet_names.get(name), 'archive_path': name, 'rows': rows,
                               'merged_ranges': [n.get('ref') for n in sheet.findall('s:mergeCells/s:mergeCell', ns)],
                               'notes': 'Native stored values; formulas are retained and never evaluated. Workbook units/styles require review.',
                               'method': 'office_xlsx_xml', 'is_specification': True})
            return [_office_review(t) for t in tables]
        if kind in {'pptx', 'pptm'}:
            ns = {'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
                  'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
            relationships = _relationships(archive, 'ppt', 'ppt/_rels/presentation.xml.rels')
            slide_paths = []
            if 'ppt/presentation.xml' in archive.namelist():
                for slide in _xml(archive, 'ppt/presentation.xml').findall('p:sldIdLst/p:sldId', ns):
                    rid = slide.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
                    if rid in relationships:
                        slide_paths.append(relationships[rid])
            if not slide_paths:
                slide_paths = sorted((n for n in archive.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml', n)), key=lambda n: int(re.search(r'(\d+)\.xml$', n).group(1)))
            tables = []
            for page_number, name in enumerate(slide_paths, 1):
                slide = _xml(archive, name)
                title = ''
                for shape in slide.findall('.//p:sp', ns):
                    placeholder = shape.find('p:nvSpPr/p:nvPr/p:ph', ns)
                    if placeholder is not None and placeholder.get('type') in {'title', 'ctrTitle'}:
                        title = ' '.join(n.text or '' for n in shape.findall('.//a:t', ns))
                        break
                for table_index, table in enumerate(slide.findall('.//a:tbl', ns), 1):
                    rows = []
                    for row in table.findall('a:tr', ns):
                        cells = []
                        for node in row.findall('a:tc', ns):
                            text = '\n'.join(''.join(n.text or '' for n in paragraph.findall('.//a:t', ns)) for paragraph in node.findall('a:txBody/a:p', ns))
                            value = cell(text, colspan=max(1, min(100, int(node.get('gridSpan', '1')))), rowspan=max(1, min(100, int(node.get('rowSpan', '1')))))
                            for merge in ('hMerge', 'vMerge'):
                                if node.get(merge) is not None:
                                    value[merge] = node.get(merge)
                            cells.append(value)
                        rows.append(cells)
                    tables.append(_office_review({'index': len(tables)+1, 'section': title or f'Slide {page_number}',
                                   'slide_number': page_number, 'slide_table_index': table_index, 'archive_path': name,
                                   'rows': rows, 'notes': 'Original presentation table; applicability requires review.',
                                   'method': 'office_pptx_xml', 'is_specification': True}))
            return tables
    return []


def extract_document(body, kind):
    if kind == 'pdf':
        with tempfile.TemporaryDirectory(prefix='fetchspec-extract-') as directory:
            path = Path(directory) / 'source.pdf'
            path.write_bytes(body)
            tables = pdf_spec_tables(path)
        method = 'pdftotext_layout'
    elif kind in {'docx', 'docm', 'xlsx', 'xlsm', 'pptx', 'pptm'}:
        tables = office_tables(body, kind)
        method = 'office_native_xml'
    else:
        tables, method = [], 'unsupported_native_format'
    for table in tables:
        table.setdefault('extraction_method', method)
    return {'tables': tables, 'method': method,
            'status': 'native_tables_extracted' if tables else 'review_required_no_native_specification',
            'uncertainty': 'Native Office cells preserved; applicability, stored formula values, units and formatting require review.' if tables and method == 'office_native_xml' else None if tables else 'No usable native specification table; OCR/model or legacy Office conversion requires explicit exception review.'}
