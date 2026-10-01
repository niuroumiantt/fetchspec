"""Siemens (siemens.com): product pages whose specifications live in linked catalog PDFs.

The en-us product pages (NXAIR, SIVACON S8, SIVACON 8PS, Cerberus …) carry marketing text and
no table. Their technical data is in the official catalog and brochure PDFs they link on
assets.new.siemens.com. The adapter follows those PDF links only (profiles, environmental
declarations and tool flyers excluded) and reads technical-data pages as layout rows
(``siemens_catalog``), never re-columning them.
"""
import hashlib
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from ..extraction import html_page
from ..siemens_catalog import METHOD, tables_from_pdf
from .base import ProductAdapter

PRODUCT = re.compile(r'^/en-us/products/(?:[a-z0-9][a-z0-9-]*/)+$')
ASSET = re.compile(r'^/siemens/assets/api/uuid:[0-9a-f-]{36}/[A-Za-z0-9._-]+\.pdf$')
NOT_SPECS = re.compile(r'profile|declaration|environmental|epd|sep\d|simaris|one-pager|report|lpit|ecotech', re.I)
CATALOG = re.compile(r'(?:^|[/_-])HA[-_.]?\d{2}[-_.]\d{2}|catalog', re.I)


class SiemensProductAdapter(ProductAdapter):
    company_id = 'siemens'

    def page_allowed(self, url):
        p = urlsplit(url)
        return p.hostname == 'www.siemens.com' and not p.query and bool(PRODUCT.match(p.path))

    def document_allowed(self, url):
        p = urlsplit(url)
        return (p.hostname == 'assets.new.siemens.com' and not p.query and bool(ASSET.match(p.path))
                and not NOT_SPECS.search(p.path.rsplit('/', 1)[-1]))

    def parse(self, body, url):
        return html_page(body, url)

    def identity(self, page, url):
        if not self.page_allowed(url):
            return None
        title = ' '.join((page.get('heading') or page.get('title') or '').split())
        # "NXAIR: Air-insulated medium-voltage switchgear | Siemens" -> "NXAIR"
        name = re.split(r'\s*[:|]\s*', title, maxsplit=1)[0].strip()
        if not name:
            return None
        key = urlsplit(url).path.rstrip('/')
        return {'id': 'siemens-' + hashlib.sha256(key.encode()).hexdigest()[:20], 'name': name,
                'kind': 'named_product' if any(t['is_specification'] for t in page.get('tables', [])) else 'family_or_directory',
                'parent_id': None}

    def candidates(self, page, url, is_directory=False):
        rows = {}
        for link in page.get('links', []):
            target = self.normalize(link['url'], url)
            if target and self.document_allowed(target):
                name = urlsplit(target).path.rsplit('/', 1)[-1]
                rows[target] = {**link, 'url': target, 'role': 'attachment', 'rank': 1 if CATALOG.search(name) else 2}
        return sorted(rows.values(), key=lambda r: (r['rank'], r['url']))

    def document_tables(self, body, kind, url):
        if kind != 'pdf' or not self.document_allowed(url):
            return None
        with tempfile.TemporaryDirectory(prefix='fetchspec-siemens-') as directory:
            path = Path(directory) / 'catalog.pdf'
            path.write_bytes(body)
            tables = tables_from_pdf(path)
        if not tables:
            return None  # fall back to the generic extractor, which states its own gap
        return {'tables': tables, 'method': METHOD, 'status': 'native_tables_extracted',
                'uncertainty': 'Layout rows from catalog technical-data pages: wrapped labels and values spanning '
                               'several rating columns are kept as printed; the reviewer names the column in map-field.'}
