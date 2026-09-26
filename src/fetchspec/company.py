"""Resumable public company acquisition; originals are never overwritten or deleted.

SQLite is the acquisition ledger, NOT the inresearch fact/approval database.
Discovery snapshots and document blobs deliberately have separate counts/paths.
"""
from collections import Counter
from contextlib import contextmanager
import fcntl
import hashlib
from html import unescape
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
from urllib.error import HTTPError
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
import zipfile

from .inventory import (FORMATS, InventoryFetcher, atomic_bytes, atomic_json,
                        canonical_url, guess_kind, utc_now)


def digest(body):
    return hashlib.sha256(body).hexdigest()


def safe_name(value):
    return re.sub(r"[^\w.()-]+", "_", value, flags=re.UNICODE).strip("._")[:130] or "document"


def document_kind(body, url, content_type=""):
    """Inspect bytes, not HTTP success or filename alone. Never execute Office content."""
    if body.lstrip()[:1024].startswith(b"%PDF-"):
        if b"%%EOF" not in body[-16384:]:
            raise ValueError("PDF lacks final EOF marker; quarantined as error")
        return "pdf"
    if body.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names:
                try:
                    mimetype = archive.read("mimetype")
                except (KeyError, OSError, zipfile.BadZipFile):
                    return None
                return {b"application/vnd.oasis.opendocument.text": "odt",
                        b"application/vnd.oasis.opendocument.spreadsheet": "ods",
                        b"application/vnd.oasis.opendocument.presentation": "odp"}.get(mimetype)
            info = archive.getinfo("[Content_Types].xml")
            if info.file_size > 2 * 1024 * 1024:
                raise ValueError("oversize Office content types")
            types = archive.read(info)
            if "word/document.xml" in names:
                return "docm" if b"macroEnabled" in types else "docx"
            if "xl/workbook.bin" in names:
                return "xlsb"
            if "xl/workbook.xml" in names:
                return "xlsm" if b"macroEnabled" in types else "xlsx"
            if "ppt/presentation.xml" in names:
                return "pptm" if b"macroEnabled" in types else "pptx"
    if body.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        if "WordDocument".encode("utf-16le") in body:
            return "doc"
        if any(name.encode("utf-16le") in body for name in ("Workbook", "Book")):
            return "xls"
        if "PowerPoint Document".encode("utf-16le") in body:
            return "ppt"
        raise ValueError("unidentified or encrypted OLE document")
    if body.startswith(b"{\\rtf"):
        return "rtf"
    if Path(urlsplit(url).path).suffix.lower() == ".csv" and not body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return "csv"
    return None


class PageLinks(HTMLParser):
    """Shared HTML discovery; header/footer links retain context, not product ownership."""
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.links, self.forms, self.title, self.breadcrumbs = [], [], [], [], []
        self.form, self.anchor = None, None
        self.sku_rels, self.system_blade = [], False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if "sku-model" in classes and attrs.get("rel"):
            self.sku_rels.append(attrs["rel"].strip().lower())
        if "system-blade" in classes:
            self.system_blade = True
        chrome = tag in {"header", "footer"} or any(x in (attrs.get("id", "") + " " + attrs.get("class", "")).lower()
                                                    for x in ("megamenu", "mega-menu", "main-navigation", "products_menu", "subnav"))
        if tag not in self.VOID:
            self.stack.append((tag, chrome, "breadcrumb" in (attrs.get("class", "") + attrs.get("id", ""))))
        context = "site_navigation" if any(x[1] for x in self.stack) else "page_body"
        if tag == "a":
            self.anchor = {"href": attrs.get("href", ""), "label": "", "context": context}
            self.links.append(self.anchor)
        for key in ("data-href", "data-url", "onclick", "src"):
            value = attrs.get(key, "")
            if value:
                self.links.append({"href": value, "label": key, "context": context})
        if tag == "form":
            self.form = {"action": attrs.get("action", ""), "method": attrs.get("method", "get").lower(), "fields": {}}
        if tag == "input" and self.form is not None and attrs.get("name") and attrs.get("type", "").lower() == "hidden":
            self.form["fields"][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "a":
            self.anchor = None
        if tag == "form" and self.form is not None:
            self.forms.append(self.form)
            self.form = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor["label"] += data[:1000]
        if any(tag == "title" for tag, _, _ in self.stack):
            self.title.append(data)
        if any(crumb for _, _, crumb in self.stack):
            self.breadcrumbs.append(data.strip())


class SupermicroAdapter:
    """Only vendor-specific URLs, read-only forms and classification live here."""
    def __init__(self, profile):
        self.profile = profile

    def normalize(self, link, base):
        url = urljoin(base, unescape(link).strip().replace("\\/", "/"))
        parts = urlsplit(url)
        if parts.hostname in self.profile["allowed_hosts"] and parts.scheme == "http":
            parts = parts._replace(scheme="https")
        # Encode Unicode/spaces while retaining escaped bytes and significant queries.
        parts = parts._replace(path=quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
                               query=quote(parts.query, safe="%=&?/:@!$'()*+,;[]-._~"))
        return canonical_url(urlunsplit(parts))

    def in_scope(self, url):
        p = urlsplit(url)
        allowed = self.profile.get("allowed_hosts_by_host", {}).get(p.hostname, self.profile["allowed_hosts"])
        if p.hostname not in allowed or p.scheme != "https" or p.port not in (None, 443):
            return False
        if guess_kind(url) in FORMATS or "/products/system/datasheet/" in p.path.lower():
            return True
        if Path(p.path).suffix.lower() in {".jpg", ".jpeg", ".png", ".svg", ".gif", ".mp4", ".js", ".css", ".zip", ".exe", ".iso", ".bin", ".rpm", ".dmg"}:
            return False
        path = p.path.lower()
        first = path.split('/')[1]
        if first in self.profile["supported_locales"]:
            path = '/en/' + path.split('/', 2)[-1] if path.count('/') >= 2 else '/en'
        return any(path.startswith(prefix.lower()) for prefix in self.profile["page_prefixes"])

    def categories(self, url):
        path = unquote(urlsplit(url).path).lower().rstrip("/")
        # These are inferred memberships, NOT an assertion that related links belong to a product.
        path = re.sub(r"^/(zh-tw|zh-cn|ja-jp|de-de|es-es|fr-fr)/", "/en/", path)
        labels = [row["label"] for row in self.profile["category_roots"]
                  if any(path == p or path.startswith(p + "/") for p in row["paths"])]
        if "/products/motherboard/" in path:
            labels.append("Building Blocks")
        if "/products/system/" in path and "/datasheet/" not in path:
            labels.append("Servers & Storage")
        return sorted(set(labels))

    def collection(self, url, label=""):
        value = (url + " " + label).lower()
        for name, pattern in [("pcn", r"\bpcn\b|product.change.notification"), ("datasheets", "datasheet"),
                              ("case-studies", "case.?stud(?:y|ies)|success.?stor"),
                              ("brochures", "brochure"), ("white-papers", "white.?paper"),
                              ("solution-briefs", "solution.?brief"), ("product-guides", "product.?guide"),
                              ("compatibility-and-test", "compatib|test.report|80plus"), ("manuals", "/manual|manual")]:
            if re.search(pattern, value):
                return name
        return "other-documents"

    def discover(self, html, base):
        parser = PageLinks()
        parser.feed(html)
        links = []
        for row in parser.links:
            href = row["href"].strip()
            candidates = [href] if not href.lower().startswith(("javascript:", "mailto:", "tel:", "#")) and row["label"] != "onclick" else []
            # Extract actual literal document URLs; never evaluate JavaScript.
            candidates += re.findall(r"['\"]([^'\"<>\s]+\.(?:pdf|docx?|dotx?|xlsx?|xltx?|xlsm?|xlsb|csv|pptx?|pptm|ppsx?|potx?|rtf|odt|ods|odp)(?:\?[^'\"<>\s]*)?)['\"]", href, re.I)
            candidates += re.findall(r"['\"]([^'\"<>\s]*/products/system/datasheet/[^'\"<>\s]+)['\"]", href, re.I)
            for candidate in candidates:
                try:
                    links.append({**row, "url": self.normalize(candidate, base), "original_href": href, "method": "GET", "payload": ""})
                except ValueError:
                    pass
        # Literal document references embedded in script/JSON are an explicitly weaker context.
        for candidate in re.findall(r"['\"]((?:https?://|/)[^'\"<>\s]+\.(?:pdf|docx?|xlsx?|docm|xlsm|xlsb)(?:\?[^'\"<>\s]*)?)['\"]", html.replace("\\/", "/"), re.I):
            try:
                links.append({"url": self.normalize(candidate, base), "label": "document literal", "context": "document_literal",
                              "original_href": candidate, "method": "GET", "payload": ""})
            except ValueError:
                pass
        for form in parser.forms:
            fields = form["fields"]
            try:
                action = self.normalize(form["action"], base)
            except ValueError:
                continue
            if (action == "https://www.supermicro.com/support/resources/results.php" and form["method"] == "post"
                    and fields.get("Resource") == "Manuals" and fields.get("ProductID", "").isdigit()
                    and set(fields) <= {"Resource", "ProductID", "ProductName"}):
                links.append({"url": action, "method": "POST", "payload": urlencode(sorted(fields.items())),
                              "label": "Manufacturer Manuals lookup", "context": "page_body", "original_href": form["action"]})
        # Reproduce only the public button URL rule observed in the manufacturer's spec.js.
        # No guessed SKU from the URL, no JavaScript evaluation, and no SRS button fabrication.
        if parser.system_blade:
            locale = urlsplit(base).path.split('/')[1]
            locale = locale if locale in self.profile["supported_locales"] else "en"
            for sku in set(parser.sku_rels):
                if re.fullmatch(r"[a-z0-9_-]{3,180}", sku) and not sku.startswith("srs"):
                    url = f"https://www.supermicro.com/{locale}/products/system/datasheet/{sku}"
                    links.append({"url": url, "method": "GET", "payload": "", "label": "Manufacturer spec.js Datasheet button",
                                  "context": "page_body", "original_href": "derived:spec.js:.system-blade/.sku-model@rel=" + sku})
        return parser, links


PAGE_ASSET_SUFFIXES = {"jpg", "jpeg", "png", "svg", "gif", "webp", "mp4", "js", "css", "json", "xml", "zip", "exe", "iso", "bin", "rpm", "dmg"}


class NvidiaAdapter:
    """NVIDIA's public product/resource site adapter.

    The shared worker owns robots, retries, the SQLite frontier and
    content-addressed storage.  This adapter only describes NVIDIA's public
    URL shape, first-level product groupings and document-purpose labels.
    It intentionally refuses gated paths and does not attempt form/API
    submission or JavaScript execution.
    """
    def __init__(self, profile):
        self.profile = profile
        # (host, space) pairs whose every page is archived; filled from the
        # ledger's space_archive decisions before any scope check.
        self.archive_spaces = set()

    def archive_page(self, parts):
        path = parts.path
        space = path.strip("/").split("/", 1)[0]
        if (parts.hostname, space) not in self.archive_spaces or "/__" in path:
            return False
        # onclick/script fragments such as self['drawer-…'].close() resolve
        # against the page path and are not pages.
        if re.search(r"[()$<>{}\[\]'\"\s]|this\.|self\[", unquote(path + "?" + parts.query)):
            return False
        return Path(path).suffix.lower().lstrip(".") not in PAGE_ASSET_SUFFIXES

    def normalize(self, link, base):
        url = urljoin(base, unescape(link).strip().replace("\\/", "/"))
        parts = urlsplit(url)
        if parts.hostname in self.profile["allowed_hosts"] and parts.scheme == "http":
            parts = parts._replace(scheme="https")
        tracking = {"accessToken", "cid", "eid", "hstc", "jso", "link", "lx", "ncid", "nvid", "ref", "srsltid", "wcmmode"}
        pairs = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                 if key not in tracking and not key.lower().startswith("utm")]
        parts = parts._replace(path=quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
                               query=urlencode(pairs, doseq=True))
        return canonical_url(urlunsplit(parts))

    def _excluded(self, url):
        value = url.lower()
        return any(token.lower() in value for token in self.profile.get("path_exclude", []))

    def language_allowed(self, url):
        """Keep English and Chinese; reject explicit other-language variants.

        NVIDIA's unmarked DAM URLs are predominantly canonical English assets.
        Localized variants are identified by locale path, a language suffix, or
        an explicit language query parameter. No translation is inferred from
        PDF contents during acquisition.
        """
        parts = urlsplit(url)
        path = unquote(parts.path).lower()
        segments = [segment for segment in path.split("/") if segment]
        target_locales = {"en", "en-us", "en-gb", "en-au", "en-in", "en-sg", "en-eu", "en-me",
                          "en-my", "en-ph", "en-sa", "en-ua", "en-am", "zh", "zh-cn", "zh-tw",
                          "scn", "tcn"}
        non_target_locales = {"cs-cz", "da-dk", "de-at", "de-ch", "de-de", "es-es", "fi-fi",
                              "fr-be", "fr-fr", "it-it", "nb-no", "nl-nl", "pl-pl", "ro-ro",
                              "sv-se", "tr-tr", "es-la", "pt-br", "ja-jp", "ko-kr", "he-il",
                              "id-id", "vi-vn", "th-th", "ar-sa", "ru-am", "uk-ua", "es-ar",
                              "es-cl", "es-mx", "es-py", "de", "es", "fr", "it", "nl", "pl",
                              "pt", "ja", "jp", "ko", "kr", "ru", "uk", "ar", "th", "vi",
                              "id", "da", "fi", "sv", "no", "nb", "cs", "ro", "tr", "he"}
        # Locale-prefixed website paths are unambiguous. nvidia.cn's bare root
        # is its Chinese storefront; /en-us/ is the English storefront.
        if parts.hostname in {"www.nvidia.com", "nvidia.com"} and segments:
            first = segments[0]
            if re.fullmatch(r"[a-z]{2}-[a-z]{2}", first) and first not in target_locales:
                return False
        if parts.hostname in {"www.nvidia.cn", "nvidia.cn"} and segments:
            first = segments[0]
            if re.fullmatch(r"[a-z]{2}-[a-z]{2}", first) and first not in target_locales:
                return False
        language_values = {value.lower().replace("_", "-") for key, value in parse_qsl(parts.query, keep_blank_values=True)
                           if key.lower() in {"lang", "language", "locale", "hl"}}
        if any(value in non_target_locales or (re.fullmatch(r"[a-z]{2}-[a-z]{2}", value) and value not in target_locales)
               for value in language_values):
            return False
        suffix = Path(path).suffix
        if suffix:
            stem = path[:-len(suffix)]
            match = re.search(r"(?:[-_.])([a-z]{2}(?:-[a-z]{2})?|scn|tcn)$", stem)
            if match:
                marker = match.group(1)
                if marker in non_target_locales or (re.fullmatch(r"[a-z]{2}-[a-z]{2}", marker) and marker not in target_locales):
                    return False
        return True

    def in_scope(self, url):
        p = urlsplit(url)
        allowed_hosts = self.profile.get("allowed_hosts_by_host", {}).get(p.hostname, self.profile["allowed_hosts"])
        if p.hostname not in allowed_hosts or p.scheme != "https" or p.port not in (None, 443):
            return False
        if self._excluded(url):
            return False
        suffix = Path(p.path).suffix.lower().lstrip(".")
        if suffix in FORMATS:
            return self.language_allowed(url)
        # Documents on official secondary hosts are in scope, but their HTML
        # navigation is not crawled as a separate website.
        page_hosts = set(self.profile.get("page_hosts", ["www.nvidia.com", "nvidia.com"]))
        if p.hostname not in page_hosts:
            return False
        if not self.language_allowed(url):
            return False
        # Documentation hosts organised as one space per product: only the
        # space landing page is opened; it links the full-manual PDF.
        host_pattern = self.profile.get("page_path_regex_by_host", {}).get(p.hostname)
        if host_pattern:
            return re.fullmatch(host_pattern, p.path) is not None or self.archive_page(p)
        # Script fragments pulled from onclick-style attributes (e.g.
        # "NVIDIAGDC.button.click(this, ...)") resolve to 404 pages.
        if re.search(r"[()$<>{}\s]|this\.", unquote(p.path + "?" + p.query)):
            return False
        # Pages (not documents) are limited to the declared storefront locales;
        # regional copies repeat the same attachments.
        page_locales = self.profile.get("page_locales")
        if page_locales and p.hostname in {"www.nvidia.com", "nvidia.com"}:
            first = p.path.lower().lstrip("/").split("/", 1)[0]
            if first not in page_locales:
                return False
        if suffix in {"jpg", "jpeg", "png", "svg", "gif", "webp", "mp4", "js", "css", "zip", "exe", "iso", "bin", "rpm", "dmg"}:
            return False
        path = p.path.lower()
        locale_prefix = self.profile.get("locale_prefixes", {}).get(p.hostname)
        if locale_prefix == "/":
            first, _, rest = path.lstrip("/").partition("/")
            path = "/en-us/" + rest if first in {"zh-cn", "zh-tw", "en-us"} else "/en-us" + path
        elif locale_prefix and path.startswith(locale_prefix.lower() + "/"):
            path = "/en-us/" + path[len(locale_prefix) + 1:]
        elif p.hostname in {"www.nvidia.com", "nvidia.com"}:
            parts = path.split("/", 2)
            if len(parts) > 2 and re.fullmatch(r"[a-z]{2}-[a-z]{2}", parts[1]):
                path = "/en-us/" + parts[2]
        return any(path == prefix.rstrip("/").lower() or path.startswith(prefix.rstrip("/").lower() + "/")
                   for prefix in self.profile.get("page_prefixes", []))

    def categories(self, url):
        path = unquote(urlsplit(url).path).lower().rstrip("/")
        host = urlsplit(url).hostname
        locale_prefix = self.profile.get("locale_prefixes", {}).get(host)
        if locale_prefix == "/":
            first, _, rest = path.lstrip("/").partition("/")
            path = "/en-us/" + rest if first in {"zh-cn", "zh-tw", "en-us"} else "/en-us" + path
        elif locale_prefix and path.startswith(locale_prefix.lower() + "/"):
            path = "/en-us/" + path[len(locale_prefix) + 1:]
        elif urlsplit(url).hostname in {"www.nvidia.com", "nvidia.com"}:
            parts = path.split("/", 2)
            if len(parts) > 2 and re.fullmatch(r"[a-z]{2}-[a-z]{2}", parts[1]):
                path = "/en-us/" + parts[2]
        if host in self.profile.get("host_categories", {}):
            return sorted(set(self.profile["host_categories"][host]))
        labels = [row["label"] for row in self.profile.get("category_roots", [])
                  if any(path == prefix.rstrip("/").lower() or
                         path.startswith(prefix.rstrip("/").lower() + "/")
                         for prefix in row["paths"])]
        return sorted(set(labels))

    def collection(self, url, label=""):
        value = (url + " " + label).lower()
        for name, pattern in [
                ("pcn", r"\bpcn\b|product.change.notification"),
                ("datasheets", r"datasheet|data.sheet|technical.brief|specification"),
                ("brochures", r"brochure"),
                ("white-papers", r"white.?paper"),
                ("solution-briefs", r"solution.?brief|reference.?architecture"),
                ("case-studies", r"case.?stud(?:y|ies)|success.?stor"),
                ("product-guides", r"product.?guide|quick.?start|getting.?started"),
                ("manuals", r"manual|user.?guide|installation|release.?notes"),
                ("presentations", r"presentation|webinar|on.?demand"),
        ]:
            if re.search(pattern, value):
                return name
        return "other-documents"

    def discover(self, html, base):
        parser = PageLinks()
        parser.feed(html)
        links = []
        seen = set()
        for row in parser.links:
            href = row["href"].strip()
            candidates = []
            if href and not href.lower().startswith(("javascript:", "mailto:", "tel:", "#")):
                candidates.append(href)
            # Keep literal asset references embedded in attributes/scripts, but
            # never evaluate JavaScript or synthesize a guessed document URL.
            candidates += re.findall(r"['\"]([^'\"<>\s]+\.(?:pdf|docx?|docm|dotx?|dotm|xlsx?|xltx?|xlsm?|xlsb|csv|pptx?|pptm|ppsx?|ppsm|potx?|potm|rtf|odt|ods|odp)(?:\?[^'\"<>\s]*)?)['\"]", href, re.I)
            for candidate in candidates:
                try:
                    url = self.normalize(candidate, base)
                except ValueError:
                    continue
                if not self.in_scope(url) or url in seen:
                    continue
                seen.add(url)
                links.append({**row, "url": url, "method": "GET", "payload": "", "original_href": href})
        for candidate in re.findall(r"['\"]((?:https?://|/)[^'\"<>\s]+\.(?:pdf|docx?|docm|dotx?|dotm|xlsx?|xltx?|xlsm?|xlsb|csv|pptx?|pptm|ppsx?|ppsm|potx?|potm|rtf|odt|ods|odp)(?:\?[^'\"<>\s]*)?)['\"]",
                                    html.replace("\\/", "/"), re.I):
            try:
                url = self.normalize(candidate, base)
            except ValueError:
                continue
            if not self.in_scope(url) or url in seen:
                continue
            seen.add(url)
            links.append({"url": url, "label": "document literal", "context": "document_literal",
                          "original_href": candidate, "method": "GET", "payload": ""})
        return parser, links


def adapter_for(profile):
    name = profile.get("adapter", "supermicro")
    if name == "supermicro":
        return SupermicroAdapter(profile)
    if name == "nvidia":
        return NvidiaAdapter(profile)
    raise ValueError("unsupported company adapter: " + str(name))


class CompanyLedger:
    def __init__(self, root, profile):
        self.root, self.profile = Path(root), profile
        self.base = self.root / "ledger" / "companies" / profile["company_id"]
        self.base.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.base / "crawl.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY, url TEXT NOT NULL, method TEXT NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', priority INTEGER, depth INTEGER, attempts INTEGER DEFAULT 0,
            first_seen TEXT, last_checked TEXT, etag TEXT, modified TEXT, latest_sha TEXT, kind TEXT,
            categories TEXT, error TEXT, source_role TEXT);
          CREATE INDEX IF NOT EXISTS queue ON requests(state, priority, depth);
          CREATE TABLE IF NOT EXISTS edges (
            parent TEXT, child TEXT, label TEXT, context TEXT, original_href TEXT, first_seen TEXT, last_seen TEXT,
            PRIMARY KEY(parent, child, label, context, original_href));
          CREATE TABLE IF NOT EXISTS blobs (sha TEXT PRIMARY KEY, kind TEXT, bytes INTEGER, path TEXT, first_seen TEXT);
          CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY, run TEXT, request TEXT, observed_at TEXT, status INTEGER, sha TEXT,
            kind TEXT, final_url TEXT, metadata TEXT, error TEXT);
          CREATE TABLE IF NOT EXISTS pages (
            request TEXT, sha TEXT, path TEXT, title TEXT, breadcrumbs TEXT, observed_at TEXT,
            PRIMARY KEY(request,sha));
          CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT, profile TEXT);
          CREATE TABLE IF NOT EXISTS exclusions (url TEXT, parent TEXT, reason TEXT, PRIMARY KEY(url,parent,reason));
          CREATE TABLE IF NOT EXISTS views (sha TEXT, request TEXT, path TEXT PRIMARY KEY, category_basis TEXT);
          CREATE TABLE IF NOT EXISTS space_archive (
            host TEXT, space TEXT, pages INTEGER, archived INTEGER, sitemap TEXT, decided_at TEXT,
            PRIMARY KEY(host, space));
        """)

    def enqueue(self, url, *, method="GET", payload="", depth=0, priority=4, categories=(), source_role="link"):
        key = digest((method + "\n" + url + "\n" + payload).encode())
        old = self.db.execute("SELECT categories FROM requests WHERE id=?", (key,)).fetchone()
        combined = sorted(set(categories) | set(json.loads(old[0]) if old else []))
        self.db.execute("""INSERT INTO requests(id,url,method,payload,priority,depth,first_seen,categories,source_role)
                        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                        priority=min(priority,excluded.priority),depth=min(depth,excluded.depth),categories=excluded.categories""",
                        (key, url, method, payload, priority, depth, utc_now(), json.dumps(combined), source_role))
        return key

    def store_blob(self, body, kind):
        sha = digest(body)
        prior = self.db.execute("SELECT * FROM blobs WHERE sha=?", (sha,)).fetchone()
        path = Path(prior["path"]) if prior else Path("blobs") / sha[:2] / (sha + "." + kind)
        target = self.root / path
        if target.exists():
            if digest(target.read_bytes()) != sha:
                raise ValueError("existing content-addressed blob failed integrity check")
        else:
            atomic_bytes(target, body)
            target.chmod(0o444)
        self.db.execute("INSERT OR IGNORE INTO blobs VALUES(?,?,?,?,?)", (sha, kind, len(body), str(path), utc_now()))
        return sha, target

    def make_views(self, request, sha, kind, adapter):
        row = self.db.execute("SELECT * FROM requests WHERE id=?", (request,)).fetchone()
        blob = self.db.execute("SELECT path FROM blobs WHERE sha=?", (sha,)).fetchone()
        filename = safe_name(unquote(Path(urlsplit(row["url"]).path).name))
        filename = f"{filename}__{sha[:16]}.{kind}" if not filename.lower().endswith('.' + kind) else f"{filename[:-(len(kind)+1)]}__{sha[:16]}.{kind}"
        labels = json.loads(row["categories"])
        collection = adapter.collection(row["url"])
        paths = [(Path("collections") / collection, "document-purpose heuristic, not vendor category")]
        paths += [(Path("vendor-categories") / safe_name(c), "URL/source-page inference; not reviewed product ownership") for c in labels]
        if not labels:
            paths.append((Path("unassigned"), "no verified vendor category association yet"))
        for section, basis in paths:
            rel = Path("library") / self.profile["company_id"] / section / filename
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            source = self.root / blob[0]
            if not target.exists() and not target.is_symlink():
                target.symlink_to(os.path.relpath(source, target.parent))
            elif not target.is_symlink() or target.resolve() != source.resolve():
                raise ValueError("library view collision; existing file preserved")
            self.db.execute("INSERT OR IGNORE INTO views VALUES(?,?,?,?)", (sha, request, str(rel), basis))

    def report(self, run=None):
        db = self.db
        states = dict(db.execute("SELECT state,count(*) FROM requests GROUP BY state"))
        kinds = dict(db.execute("SELECT kind,count(*) FROM blobs GROUP BY kind"))
        document_kinds = sorted(FORMATS)
        placeholders = ",".join("?" for _ in document_kinds)
        recent = db.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        result = {"company": self.profile["company_id"], "generated_at": utc_now(),
                  "run": dict(recent) if recent else None, "queue": states,
                  "unique_document_contents": sum(kinds.values()), "document_types": kinds,
                  "unique_document_bytes": db.execute("SELECT coalesce(sum(bytes),0) FROM blobs").fetchone()[0],
                  "document_source_requests": db.execute(f"SELECT count(*) FROM requests WHERE kind IN ({placeholders})", document_kinds).fetchone()[0],
                  "page_snapshots": db.execute("SELECT count(*) FROM pages").fetchone()[0],
                  "excluded_links": db.execute("SELECT count(*) FROM exclusions").fetchone()[0],
                  "failed_samples": [dict(r) for r in db.execute("SELECT url,state,error FROM requests WHERE state IN ('error','blocked') LIMIT 15")],
                  "website_coverage_complete": False, "known_gaps": self.profile["known_gaps"],
                  "reading_completed": False, "spark_transferred": False, "inresearch_facts_adopted": False,
                  "data_root": str(self.root), "ledger": str(self.base / 'crawl.sqlite')}
        if result["run"]:
            result["run"].pop("profile")
            result["current_run_successful_document_responses"] = db.execute(
                f"SELECT count(*) FROM observations WHERE run=? AND kind IN ({placeholders}) AND status=200",
                (recent["id"], *document_kinds)).fetchone()[0]
        return result


def worker_active(base):
    """True while another process holds this company's worker lock."""
    lock = Path(base) / "worker.lock"
    if not lock.exists():
        return False
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
        return False


def progress_summary(ledger, window_seconds=3600):
    """Operator view: queue, recent throughput, ETA and grouped errors.

    Throughput counts observations in the trailing window, so the ETA is an
    estimate from recent pace, not a promise of site completeness.
    """
    db = ledger.db
    states = dict(db.execute("SELECT state,count(*) FROM requests GROUP BY state"))
    recent = db.execute("SELECT id,started_at,finished_at,status FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - window_seconds))
    window = db.execute("SELECT count(*),min(observed_at),max(observed_at) FROM observations WHERE observed_at>=?", (since,)).fetchone()
    count, first, last = window
    rate = None
    if count and count > 1 and first != last:
        span = (_parse_ts(last) - _parse_ts(first))
        rate = round(count / span * 3600, 1) if span > 0 else None
    pending = states.get("pending", 0) + states.get("fetching", 0)
    errors = Counter()
    for (error,) in db.execute("SELECT error FROM requests WHERE state IN ('error','blocked')"):
        errors[(error or "unknown").split(":")[0]] += 1
    return {
        "company": ledger.profile["company_id"],
        "generated_at": utc_now(),
        "worker_active": worker_active(ledger.base),
        "last_run": dict(recent) if recent else None,
        "queue": states,
        "pending": pending,
        "unique_documents": db.execute("SELECT count(*) FROM blobs").fetchone()[0],
        "unique_document_bytes": db.execute("SELECT coalesce(sum(bytes),0) FROM blobs").fetchone()[0],
        "window_seconds": window_seconds,
        "window_observations": count,
        "last_observation": last,
        "requests_per_hour": rate,
        "eta_hours": round(pending / rate, 1) if rate else None,
        "error_types": dict(errors.most_common(10)),
        "data_root": str(ledger.root),
    }


def format_summary(summary):
    queue = summary["queue"]
    run = summary["last_run"] or {}
    lines = [
        f"{summary['company']}  worker={'running' if summary['worker_active'] else 'stopped'}  last_run={run.get('status')} ({run.get('started_at', '-')})",
        f"queue: pending={summary['pending']} done={queue.get('done', 0)} error={queue.get('error', 0)} "
        f"blocked={queue.get('blocked', 0)} excluded={queue.get('excluded', 0)}",
        f"documents: {summary['unique_documents']} unique, {summary['unique_document_bytes'] / 1e6:.1f} MB",
        f"pace (last {summary['window_seconds'] // 60} min): {summary['window_observations']} requests, "
        f"{summary['requests_per_hour'] or '-'} /h, ETA {summary['eta_hours'] if summary['eta_hours'] is not None else '-'} h",
        f"last activity: {summary['last_observation'] or '-'}",
    ]
    if summary["error_types"]:
        lines.append("errors: " + ", ".join(f"{k}={v}" for k, v in summary["error_types"].items()))
    lines.append(f"data root: {summary['data_root']}")
    return "\n".join(lines)


def _parse_ts(value):
    from datetime import datetime
    return datetime.fromisoformat(value).timestamp()


@contextmanager
def company_lock(base):
    base.mkdir(parents=True, exist_ok=True)
    with (base / "worker.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def import_inventory(ledger, adapter, manifest):
    accepted_hosts = set(ledger.profile["allowed_hosts"])
    for line in Path(manifest).read_text().splitlines():
        item = json.loads(line)
        url = adapter.normalize(item["url"], item["url"])
        if urlsplit(url).hostname in accepted_hosts and adapter.in_scope(url):
            priority = 0 if guess_kind(url) in FORMATS else (4 if "/en/" in url else 7)
            if "/faq" in url.lower():
                priority = 9
            ledger.enqueue(url, priority=priority, categories=adapter.categories(url), source_role="sitemap")
            ledger.db.execute("DELETE FROM exclusions WHERE url=? AND parent='sitemap' AND reason='outside declared public product/document scope'", (url,))
        else:
            ledger.db.execute("INSERT OR IGNORE INTO exclusions VALUES(?,?,?)", (url, "sitemap", "outside declared public product/document scope"))
    for row in ledger.profile.get("discovery_entrypoints", []):
        ledger.enqueue(row["url"], priority=1, source_role=row["role"])
    base_url = ledger.profile.get("base_url", "https://www.supermicro.com").rstrip("/")
    for row in ledger.profile.get("category_roots", []):
        for path in row["paths"]:
            ledger.enqueue(base_url + path, priority=2, categories=[row["label"]], source_role="vendor_category_entry")
    ledger.db.commit()


def import_legacy_documents(ledger, adapter):
    """Reuse verified legacy originals. Do not rewrite the legacy catalog or invent retrieval time."""
    path = ledger.root / "ledger" / "catalog.json"
    if not path.exists():
        return
    for item in json.loads(path.read_text()).get("records", []):
        if item.get("company_id") != ledger.profile["company_id"] or item.get("kind") not in FORMATS:
            continue
        url = item.get("requested_url") or item["source_url"]
        key = ledger.enqueue(url, categories=adapter.categories(url), priority=0, source_role="legacy_catalog")
        if ledger.db.execute("SELECT 1 FROM observations WHERE request=? LIMIT 1", (key,)).fetchone():
            continue
        blob = (ledger.root / item["blob"]).resolve()
        if not blob.is_relative_to(ledger.root.resolve()):
            raise ValueError("legacy blob path outside data root")
        body = blob.read_bytes()
        if digest(body) != item["sha256"] or document_kind(body, url) != item["kind"]:
            raise ValueError("legacy document integrity/type mismatch")
        sha, _ = ledger.store_blob(body, item["kind"])
        ledger.db.execute("UPDATE requests SET state='done',latest_sha=?,kind=? WHERE id=?", (sha, item["kind"], key))
        ledger.db.execute("INSERT INTO observations(run,request,observed_at,status,sha,kind,final_url,metadata) VALUES(?,?,?,?,?,?,?,?)",
                          ("legacy_import", key, None, item.get("http_status"), sha, item["kind"], item["source_url"],
                           json.dumps({"imported_at": utc_now(), "legacy_collected_date": item.get("collected_date"), "source_catalog": str(path)})))
        ledger.make_views(key, sha, item["kind"], adapter)
    ledger.db.commit()


def export_documents(ledger):
    rows = []
    for blob in ledger.db.execute("SELECT * FROM blobs ORDER BY kind,sha"):
        sources = [dict(row) for row in ledger.db.execute(
            """SELECT DISTINCT r.id,r.url,r.method,r.payload,r.categories FROM requests r
               JOIN observations o ON r.id=o.request WHERE o.sha=?""", (blob["sha"],))]
        rows.append({**dict(blob), "sources": sources,
                     "views": [r[0] for r in ledger.db.execute("SELECT path FROM views WHERE sha=?", (blob["sha"],))]})
    atomic_bytes(ledger.base / "documents.jsonl", ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows).encode())


def archive_small_spaces(ledger, adapter, fetcher, run, index, archive):
    """Queue every page of documentation spaces small enough to archive whole.

    Small spaces are product hardware guides (adapters, cables, transceivers,
    switches) that publish specifications as HTML with no PDF.  Large spaces
    are software manuals and release notes; they keep root-only treatment.
    Each space's sitemap is read once and the decision is kept in the ledger.
    """
    errors = []
    for loc in re.findall(rb"<loc>\s*([^<\s]+)\s*</loc>", index):
        sitemap = unescape(loc.decode("utf-8", "replace"))
        parts = urlsplit(sitemap)
        space = parts.path.strip("/").split("/", 1)[0]
        if not space or space.startswith("__") or "/__sitemaps/" not in parts.path:
            continue
        if ledger.db.execute("SELECT 1 FROM space_archive WHERE host=? AND space=?", (parts.hostname, space)).fetchone():
            continue
        try:
            body, meta = fetcher.get(sitemap)
        except Exception as exc:
            errors.append({"url": sitemap, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            continue
        atomic_bytes(ledger.base / "runs" / run / "space-sitemaps" / (digest(body) + ".xml"), body)
        pages = sorted({canonical_url(unescape(p.decode("utf-8", "replace")))
                        for p in re.findall(rb"<loc>\s*([^<\s]+)\s*</loc>", body)})
        archived = len(pages) <= archive["max_pages"]
        ledger.db.execute("INSERT INTO space_archive VALUES(?,?,?,?,?,?)",
                          (parts.hostname, space, len(pages), int(archived), sitemap, utc_now()))
        if archived:
            adapter.archive_spaces.add((parts.hostname, space))
            for url in pages:
                if adapter.in_scope(url):
                    ledger.enqueue(url, priority=3, categories=adapter.categories(url), source_role="space_page_archive")
        ledger.db.commit()
    return errors


def enqueue_space_roots(ledger, adapter, fetcher, run):
    """Enqueue one landing page per documentation space listed in a sitemap.

    Space sitemaps list every page; only the root is needed because it links
    the full-manual attachment. The sitemap body is kept as source evidence.
    """
    errors = []
    for source in ledger.profile.get("space_sitemaps", []):
        try:
            body, meta = fetcher.get(source["url"])
        except Exception as exc:
            # An unavailable space index must not stop the rest of the frontier.
            errors.append({"url": source["url"], "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            continue
        atomic_bytes(ledger.base / "runs" / run / "space-sitemaps" / (digest(body) + ".xml"), body)
        roots = set()
        for loc in re.findall(rb"<loc>\s*([^<\s]+)\s*</loc>", body):
            parts = urlsplit(unescape(loc.decode("utf-8", "replace")))
            space = parts.path.strip("/").split("/", 1)[0]
            if space and not space.startswith("__"):
                roots.add(urlunsplit((parts.scheme, parts.netloc, "/" + space + "/", "", "")))
        for url in sorted(roots):
            if adapter.in_scope(url):
                ledger.enqueue(url, priority=1, categories=adapter.categories(url), source_role=source["role"])
        archive = ledger.profile.get("space_page_archive")
        if archive:
            errors += archive_small_spaces(ledger, adapter, fetcher, run, body, archive)
        ledger.db.commit()
    if errors:
        atomic_json(ledger.base / "runs" / run / "space-sitemap-errors.json", errors)


# Errors a later attempt can plausibly clear: transfer timeouts, dropped or
# refused connections, TLS handshakes and 5xx.  4xx, robots/allowlist blocks
# and content-type mismatches are terminal and are not retried.
TRANSIENT_ERROR_PREFIXES = (
    "TimeoutError:", "timeout:", "URLError:", "ConnectionError:", "ConnectionResetError:",
    "ConnectionAbortedError:", "ConnectionRefusedError:", "RemoteDisconnected:", "IncompleteRead:",
    "SSLError:", "OSError:", "HTTPError: HTTP Error 5", "ValueError: incomplete response",
    "ValueError: robots unavailable for host",
)


def is_transient_error(error):
    return bool(error) and error.startswith(TRANSIENT_ERROR_PREFIXES)


def run_company(profile, root, manifest=None, max_requests=0, recheck=False, force=False, progress=None, fetcher=None,
                retry_errors=False):
    ledger = CompanyLedger(root, profile)
    adapter = adapter_for(profile)
    with company_lock(ledger.base):
        if manifest:
            import_inventory(ledger, adapter, manifest)
        import_legacy_documents(ledger, adapter)
        if hasattr(adapter, "archive_spaces"):
            adapter.archive_spaces = {(row[0], row[1]) for row in
                                      ledger.db.execute("SELECT host,space FROM space_archive WHERE archived=1")}
        if recheck:
            ledger.db.execute("UPDATE requests SET state='pending',attempts=0 WHERE state!='blocked'")
        if retry_errors:
            for failed in ledger.db.execute("SELECT id,error FROM requests WHERE state='error'").fetchall():
                if is_transient_error(failed["error"]):
                    ledger.db.execute("UPDATE requests SET state='pending',attempts=0 WHERE id=?", (failed["id"],))
        ledger.db.execute("UPDATE requests SET state='pending' WHERE state='fetching'")
        # Apply changed language/scope policy to an existing resumable frontier
        # before any network request. Keep records for audit; never delete them.
        for candidate in ledger.db.execute("SELECT id,url FROM requests WHERE state='pending'").fetchall():
            if not adapter.in_scope(candidate["url"]):
                reason = "excluded by current language or public-scope policy"
                ledger.db.execute("UPDATE requests SET state='excluded',error=? WHERE id=?", (reason, candidate["id"]))
                ledger.db.execute("INSERT OR IGNORE INTO exclusions VALUES(?,?,?)", (candidate["url"], "policy-refresh", reason))
        ledger.db.commit()
        run = utc_now().replace(":", "").replace("+", "_")
        ledger.db.execute("INSERT INTO runs VALUES(?,?,NULL,'running',?)", (run, utc_now(), json.dumps(profile)))
        ledger.db.commit()
        fetcher = fetcher or InventoryFetcher(profile)
        status, processed, consecutive_failures = "running", 0, 0
        low_yield_limit = profile.get("max_pages_without_new_document", 0)
        pages_since_new_document = 0
        try:
            receipts = fetcher.prepare_robots()
            atomic_json(ledger.base / "runs" / run / "robots.json", receipts)
            atomic_json(ledger.base / "runs" / run / "profile.json", profile)
            enqueue_space_roots(ledger, adapter, fetcher, run)
            evidence_url = profile.get("adapter_evidence", {}).get("datasheet_button_source")
            if evidence_url and isinstance(fetcher, InventoryFetcher):
                evidence, evidence_meta = fetcher.get(evidence_url)
                evidence_sha = digest(evidence)
                atomic_bytes(ledger.base / "adapter-evidence" / (evidence_sha + ".js"), evidence)
                atomic_json(ledger.base / "runs" / run / "adapter-evidence.json", {"url": evidence_url, "sha256": evidence_sha, **evidence_meta})
                if not all(token in evidence for token in (b"getNormalizedSkuRel", b"products/system/datasheet/", b".system-blade", b".sku-model")):
                    raise ValueError("manufacturer datasheet button implementation changed; adapter review needed")
            while True:
                if (ledger.base / "STOP").exists():
                    status = "paused_stop_file"
                    break
                if shutil.disk_usage(ledger.root).free < profile["min_free_bytes"]:
                    status = "paused_low_disk"
                    break
                if max_requests and processed >= max_requests:
                    status = "paused_request_budget"
                    break
                if low_yield_limit and pages_since_new_document >= low_yield_limit:
                    # Many pages opened with no new document: the scope is wrong, not the pace.
                    status = "paused_low_yield"
                    break
                row = ledger.db.execute("SELECT * FROM requests WHERE state='pending' ORDER BY priority,depth,first_seen LIMIT 1").fetchone()
                if row is None:
                    status = "frontier_exhausted_with_gaps"
                    break
                ledger.db.execute("UPDATE requests SET state='fetching', attempts=attempts+1 WHERE id=?", (row["id"],))
                ledger.db.commit()
                meta, sha, kind = {}, None, None
                try:
                    headers = {}
                    if row["method"] == "GET" and not force:
                        if row["etag"]:
                            headers["If-None-Match"] = row["etag"]
                        if row["modified"]:
                            headers["If-Modified-Since"] = row["modified"]
                    body, meta = fetcher.get(row["url"], headers=headers,
                                             data=row["payload"].encode() if row["method"] == "POST" else None,
                                             cap=profile["max_document_bytes"])
                    kind = document_kind(body, meta["final_url"], meta.get("content_type", ""))
                    if kind:
                        known = ledger.db.execute("SELECT 1 FROM blobs WHERE sha=?", (digest(body),)).fetchone()
                        sha, _ = ledger.store_blob(body, kind)
                        if not known:
                            pages_since_new_document = 0
                        ledger.make_views(row["id"], sha, kind, adapter)
                    elif "html" in meta.get("content_type", "").lower() or body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                        if guess_kind(row["url"]) in FORMATS or "/products/system/datasheet/" in row["url"]:
                            raise ValueError("document URL returned HTML, not a document")
                        kind, sha = "html", digest(body)
                        # Hosts whose pages are the documentation itself keep
                        # snapshots even when discovery pages are not saved.
                        archive_host = urlsplit(row["url"]).hostname in profile.get("save_pages_hosts", [])
                        save_page = profile.get("save_discovery_pages", True) or archive_host
                        path = ledger.base / "snapshots" / sha[:2] / (sha + ".html")
                        new_snapshot = save_page and not path.exists()
                        if new_snapshot:
                            atomic_bytes(path, body)
                        # A newly archived documentation page is new content for
                        # the low-yield guard, like a new document.
                        pages_since_new_document = 0 if (archive_host and new_snapshot) else pages_since_new_document + 1
                        parser, links = adapter.discover(body.decode("utf-8", "replace"), meta["final_url"])
                        if save_page:
                            ledger.db.execute("INSERT OR IGNORE INTO pages VALUES(?,?,?,?,?,?)",
                                              (row["id"], sha, str(path.relative_to(ledger.root)), ''.join(parser.title)[:1000], json.dumps(parser.breadcrumbs), utc_now()))
                        seen_links = set()
                        for link in links:
                            url = link["url"]
                            identity = (url, link["method"], link["payload"], link["context"])
                            if identity in seen_links:
                                continue
                            seen_links.add(identity)
                            # Repeated menu/literal links are recoverable from each HTML snapshot.
                            # Index once, never fabricate per-product ownership for navigation.
                            if link["context"] != "page_body" and ledger.db.execute("SELECT 1 FROM requests WHERE id=?", (
                                    digest((link["method"] + '\n' + url + '\n' + link["payload"]).encode()),)).fetchone():
                                continue
                            is_document = guess_kind(url) in FORMATS or "/products/system/datasheet/" in url
                            is_form = link["method"] == "POST"
                            reason = None
                            if not adapter.in_scope(url):
                                reason = "outside declared public product/document scope"
                            elif not is_document and not is_form and row["depth"] >= profile["max_discovery_depth"]:
                                reason = "discovery depth guard; unresolved coverage"
                            elif ledger.db.execute("SELECT count(*) FROM requests").fetchone()[0] >= profile["max_requests"]:
                                reason = "queue size guard; unresolved coverage"
                            if reason:
                                ledger.db.execute("INSERT OR IGNORE INTO exclusions VALUES(?,?,?)", (url, row["id"], reason))
                                continue
                            categories = adapter.categories(url)
                            if link["context"] == "page_body" and (is_document or is_form):
                                categories += json.loads(row["categories"])
                            priority = 0 if is_document else (1 if is_form else (2 if "/resources?page=" in url else 5))
                            child = ledger.enqueue(url, method=link["method"], payload=link["payload"], depth=row["depth"] + 1,
                                                   priority=priority, categories=categories)
                            existing_doc = ledger.db.execute("SELECT latest_sha,kind FROM requests WHERE id=?", (child,)).fetchone()
                            if existing_doc["kind"] in FORMATS and existing_doc["latest_sha"]:
                                ledger.make_views(child, existing_doc["latest_sha"], existing_doc["kind"], adapter)
                            ledger.db.execute("""INSERT INTO edges VALUES(?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET last_seen=excluded.last_seen""",
                                              (row["id"], child, link["label"].strip()[:500], link["context"], link["original_href"][:2000], utc_now(), utc_now()))
                    else:
                        raise ValueError("unsupported or unidentified response body")
                    ledger.db.execute("""UPDATE requests SET state='done',last_checked=?,etag=?,modified=?,latest_sha=?,kind=?,error=NULL WHERE id=?""",
                                      (utc_now(), meta.get("etag"), meta.get("last_modified"), sha, kind, row["id"]))
                    ledger.db.execute("INSERT INTO observations(run,request,observed_at,status,sha,kind,final_url,metadata) VALUES(?,?,?,?,?,?,?,?)",
                                      (run, row["id"], utc_now(), meta["status"], sha, kind, meta["final_url"], json.dumps(meta)))
                    consecutive_failures = 0
                except Exception as exc:
                    code = exc.code if isinstance(exc, HTTPError) else None
                    unchanged = code == 304 and bool(row["latest_sha"])
                    error = None if unchanged else f"{type(exc).__name__}: {str(exc)[:400]}"
                    blocked = "robots disallowed" in str(exc).lower() or "allowlist" in str(exc).lower()
                    state = "done" if unchanged else ("blocked" if blocked else "error")
                    ledger.db.execute("UPDATE requests SET state=?,last_checked=?,error=? WHERE id=?", (state, utc_now(), error, row["id"]))
                    ledger.db.execute("INSERT INTO observations(run,request,observed_at,status,sha,kind,metadata,error) VALUES(?,?,?,?,?,?,?,?)",
                                      (run, row["id"], utc_now(), code, row["latest_sha"] if unchanged else None,
                                       row["kind"] if unchanged else None, json.dumps(meta), error))
                    # A stale sitemap entry is an expected terminal result, not
                    # evidence that the remote service is unavailable.  Only
                    # transport failures and 5xx responses contribute to the
                    # circuit breaker; 429 remains an immediate pause below.
                    retryable_failure = not isinstance(exc, HTTPError) or (code is not None and code >= 500)
                    if not unchanged and not blocked and retryable_failure:
                        consecutive_failures += 1
                    else:
                        consecutive_failures = 0
                    if code in {429, 503} or consecutive_failures >= 10:
                        status = "paused_remote_errors"
                ledger.db.commit()
                processed += 1
                if processed % 10 == 0 or kind in FORMATS or status != "running":
                    report = ledger.report(run)
                    atomic_json(ledger.base / "status.json", report)
                    if progress:
                        progress({"processed": processed, "queue": report["queue"], "documents": report["document_types"], "last_url": row["url"], "worker_status": status})
                if processed % 100 == 0:
                    export_documents(ledger)
                if status != "running":
                    break
        except BaseException:
            status = "interrupted_or_setup_error"
            raise
        finally:
            ledger.db.execute("UPDATE runs SET finished_at=?,status=? WHERE id=?", (utc_now(), status, run))
            ledger.db.commit()
            result = ledger.report(run)
            export_documents(ledger)
            atomic_json(ledger.base / "status.json", result)
            atomic_json(ledger.base / "runs" / run / "status.json", result)
            ledger.db.close()
        return result
