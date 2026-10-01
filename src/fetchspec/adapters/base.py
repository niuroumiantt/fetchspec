import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..inventory import load_profile

FORMATS = {'pdf', 'doc', 'docx', 'docm', 'xls', 'xlsx', 'xlsm', 'xlsb', 'ppt', 'pptx', 'pptm'}


def language(url):
    p = urlsplit(url)
    value = p.path.lower() + '?' + p.query.lower()
    if re.search(r'(?:/|_|-|lang(?:uage)?=)(?:ja|jp|de|fr|es|ko|it|pt|ru)(?:[/_.-]|$|&)', value):
        return None
    if p.hostname.endswith('.cn') or re.search(r'(?:zh|chinese)(?:[-_/=]|$)', value):
        return 'zh'
    return 'en'


class ProductAdapter:
    company_id = None

    def __init__(self, known_catalog=None):
        self.profile = load_profile(self.company_id)
        self.known = (known_catalog or {}).get('products', [])

    def normalize(self, url, base=''):
        p = urlsplit(urljoin(base, url))
        if p.username or p.password or p.scheme != 'https' or p.port not in (None, 443):
            return None
        if p.hostname not in self.profile['allowed_hosts'] or language(urlunsplit(p)) is None:
            return None
        return urlunsplit((p.scheme, p.netloc.lower(), p.path, p.query, ''))

    def format(self, url):
        suffix = urlsplit(url).path.rsplit('.', 1)[-1].lower()
        return suffix if suffix in FORMATS else None

    def component_allowed(self, url, content_type):
        """Whether a URL the product page names is an official JSON data component (none by default)."""
        return False

    def component_tables(self, body, url):
        raise ValueError('no official data component parser for ' + self.company_id)

    def candidates(self, page, url, is_directory=False):
        rows = []
        for link in page.get('links', []):
            target = self.normalize(link['url'], url)
            if not target:
                continue
            kind = self.format(target)
            spec = bool(re.search(r'spec|datasheet|data.sheet|technical|规格|規格|数据表|技術', link.get('label', '') + ' ' + target, re.I))
            if kind and spec:
                rows.append({**link, 'url': target, 'role': 'attachment', 'rank': 1 if kind == 'pdf' else 2})
            elif self.page_allowed(target) and (is_directory or spec):
                rows.append({**link, 'url': target, 'role': 'product' if is_directory else 'specification', 'rank': 0})
        return sorted(rows, key=lambda r: (r['rank'], r['url']))
