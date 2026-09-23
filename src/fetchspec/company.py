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
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
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
                return None
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
    if body.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        if "WordDocument".encode("utf-16le") in body:
            return "doc"
        if any(name.encode("utf-16le") in body for name in ("Workbook", "Book")):
            return "xls"
        raise ValueError("unidentified or encrypted OLE document")
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
        if p.hostname not in self.profile["allowed_hosts"] or p.scheme != "https" or p.port not in (None, 443):
            return False
        if guess_kind(url) in FORMATS or "/products/system/datasheet/" in p.path.lower():
            return True
        if Path(p.path).suffix.lower() in {".jpg", ".jpeg", ".png", ".svg", ".gif", ".mp4", ".js", ".css", ".zip", ".exe", ".iso", ".bin", ".rpm", ".dmg"}:
            return False
        return any(p.path.lower().startswith(prefix.lower()) for prefix in self.profile["page_prefixes"])

    def categories(self, url):
        path = unquote(urlsplit(url).path).lower().rstrip("/")
        # These are inferred memberships, NOT an assertion that related links belong to a product.
        path = re.sub(r"^/(zh-tw|zh-cn|ja-jp|de-de)/", "/en/", path)
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
            candidates += re.findall(r"['\"]([^'\"<>\s]+\.(?:pdf|docx?|xlsx?|docm|xlsm|xlsb)(?:\?[^'\"<>\s]*)?)['\"]", href, re.I)
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
            locale = locale if locale in {"en", "zh-tw", "zh-cn", "ja-jp", "de-de"} else "en"
            for sku in set(parser.sku_rels):
                if re.fullmatch(r"[a-z0-9_-]{3,180}", sku) and not sku.startswith("srs"):
                    url = f"https://www.supermicro.com/{locale}/products/system/datasheet/{sku}"
                    links.append({"url": url, "method": "GET", "payload": "", "label": "Manufacturer spec.js Datasheet button",
                                  "context": "page_body", "original_href": "derived:spec.js:.system-blade/.sku-model@rel=" + sku})
        return parser, links


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
        recent = db.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        result = {"company": self.profile["company_id"], "generated_at": utc_now(),
                  "run": dict(recent) if recent else None, "queue": states,
                  "unique_document_contents": sum(kinds.values()), "document_types": kinds,
                  "unique_document_bytes": db.execute("SELECT coalesce(sum(bytes),0) FROM blobs").fetchone()[0],
                  "document_source_requests": db.execute("SELECT count(*) FROM requests WHERE kind IN ('pdf','doc','docx','docm','xls','xlsx','xlsm','xlsb')").fetchone()[0],
                  "page_snapshots": db.execute("SELECT count(*) FROM pages").fetchone()[0],
                  "excluded_links": db.execute("SELECT count(*) FROM exclusions").fetchone()[0],
                  "failed_samples": [dict(r) for r in db.execute("SELECT url,state,error FROM requests WHERE state IN ('error','blocked') LIMIT 15")],
                  "website_coverage_complete": False, "known_gaps": self.profile["known_gaps"],
                  "reading_completed": False, "spark_transferred": False, "inresearch_facts_adopted": False,
                  "data_root": str(self.root), "ledger": str(self.base / 'crawl.sqlite')}
        if result["run"]:
            result["run"].pop("profile")
            result["current_run_successful_document_responses"] = db.execute(
                "SELECT count(*) FROM observations WHERE run=? AND kind IN ('pdf','doc','docx','docm','xls','xlsx','xlsm','xlsb') AND status=200", (recent["id"],)).fetchone()[0]
        return result


@contextmanager
def company_lock(base):
    base.mkdir(parents=True, exist_ok=True)
    with (base / "worker.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def import_inventory(ledger, adapter, manifest):
    for line in Path(manifest).read_text().splitlines():
        item = json.loads(line)
        url = adapter.normalize(item["url"], item["url"])
        if adapter.in_scope(url):
            priority = 0 if guess_kind(url) in FORMATS else (4 if "/en/" in url else 7)
            ledger.enqueue(url, priority=priority, categories=adapter.categories(url), source_role="sitemap")
        else:
            ledger.db.execute("INSERT OR IGNORE INTO exclusions VALUES(?,?,?)", (url, "sitemap", "outside declared public product/document scope"))
    for row in ledger.profile["discovery_entrypoints"]:
        ledger.enqueue(row["url"], priority=1, source_role=row["role"])
    for row in ledger.profile["category_roots"]:
        for path in row["paths"]:
            ledger.enqueue("https://www.supermicro.com" + path, priority=2, categories=[row["label"]], source_role="vendor_category_entry")
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


def run_company(profile, root, manifest=None, max_requests=0, recheck=False, force=False, progress=None, fetcher=None):
    ledger = CompanyLedger(root, profile)
    adapter = SupermicroAdapter(profile)
    with company_lock(ledger.base):
        if manifest:
            import_inventory(ledger, adapter, manifest)
        import_legacy_documents(ledger, adapter)
        if recheck:
            ledger.db.execute("UPDATE requests SET state='pending',attempts=0 WHERE state!='blocked'")
        ledger.db.execute("UPDATE requests SET state='pending' WHERE state='fetching'")
        run = utc_now().replace(":", "").replace("+", "_")
        ledger.db.execute("INSERT INTO runs VALUES(?,?,NULL,'running',?)", (run, utc_now(), json.dumps(profile)))
        ledger.db.commit()
        fetcher = fetcher or InventoryFetcher(profile)
        status, processed, consecutive_failures = "running", 0, 0
        try:
            receipts = fetcher.prepare_robots()
            atomic_json(ledger.base / "runs" / run / "robots.json", receipts)
            atomic_json(ledger.base / "runs" / run / "profile.json", profile)
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
                        sha, _ = ledger.store_blob(body, kind)
                        ledger.make_views(row["id"], sha, kind, adapter)
                    elif "html" in meta.get("content_type", "").lower() or body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                        if guess_kind(row["url"]) in FORMATS or "/products/system/datasheet/" in row["url"]:
                            raise ValueError("document URL returned HTML, not a document")
                        kind, sha = "html", digest(body)
                        path = ledger.base / "snapshots" / sha[:2] / (sha + ".html")
                        if not path.exists():
                            atomic_bytes(path, body)
                        parser, links = adapter.discover(body.decode("utf-8", "replace"), meta["final_url"])
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
                    blocked = "robots" in str(exc).lower() or "allowlist" in str(exc).lower()
                    state = "done" if unchanged else ("blocked" if blocked else "error")
                    ledger.db.execute("UPDATE requests SET state=?,last_checked=?,error=? WHERE id=?", (state, utc_now(), error, row["id"]))
                    ledger.db.execute("INSERT INTO observations(run,request,observed_at,status,sha,kind,metadata,error) VALUES(?,?,?,?,?,?,?,?)",
                                      (run, row["id"], utc_now(), code, row["latest_sha"] if unchanged else None,
                                       row["kind"] if unchanged else None, json.dumps(meta), error))
                    if not unchanged and not blocked:
                        consecutive_failures += 1
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
