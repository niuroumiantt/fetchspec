"""Replayable company sitemap inventory. Never downloads product attachments.

This discovery ledger is separate from the legacy downloaded-file catalog.
Inventory counts are URL candidates, not verified files or coverage percentages.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler
import xml.etree.ElementTree as ET

from .catalog import ROOT
from .robots import Robots

USER_AGENT = "InResearchFetchspec/0.2 (+https://github.com/niuroumiantt/fetchspec)"
FORMATS = {"pdf", "doc", "docx", "xls", "xlsx", "docm", "xlsm", "xlsb"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_bytes(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".inventory-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def load_profile(name):
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise ValueError("invalid company profile name")
    profile = json.loads((ROOT / "profiles" / (name + ".json")).read_text())
    if profile.get("profile_version") != 1 or profile.get("company_id") != name:
        raise ValueError("unsupported or mismatched profile")
    return profile


def canonical_url(url):
    value = urlsplit(url.strip())
    if value.scheme not in {"https", "http"} or not value.hostname or value.username or value.password:
        raise ValueError("unsafe URL")
    # Preserve queries: version, language and download identifiers can be significant.
    return urlunsplit((value.scheme.lower(), value.netloc.lower(), value.path or "/", value.query, ""))


def parse_sitemap(body):
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("DTD/entity declarations are not accepted")
    root = ET.fromstring(body)
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in {"urlset", "sitemapindex"}:
        raise ValueError("not a sitemap")
    expected = "url" if kind == "urlset" else "sitemap"
    rows = []
    for child in root:
        if child.tag.rsplit("}", 1)[-1] != expected:
            continue
        fields = {element.tag.rsplit("}", 1)[-1]: (element.text or "").strip()
                  for element in child if element.tag.rsplit("}", 1)[-1] in {"loc", "lastmod"}}
        if not fields.get("loc"):
            raise ValueError("sitemap entry lacks location")
        fields["loc"] = canonical_url(fields["loc"])
        rows.append(fields)
    if not rows:
        raise ValueError("empty sitemap requires review")
    return kind, rows


def guess_kind(url):
    suffix = Path(urlsplit(url).path).suffix.lower().lstrip(".")
    return suffix if suffix in FORMATS else "page_or_download_endpoint"


class PolicyRedirect(HTTPRedirectHandler):
    def __init__(self, check):
        self.check = check

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.check(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class InventoryFetcher:
    def __init__(self, profile):
        self.profile = profile
        self.robots = {}
        self.last_request = 0.0
        self.opener = build_opener(PolicyRedirect(self.check), HTTPSHandler(context=ssl.create_default_context()))

    def check(self, url):
        parts = urlsplit(canonical_url(url))
        if parts.hostname not in self.profile["allowed_hosts"] or parts.scheme != "https" or parts.port not in (None, 443):
            raise ValueError("redirect or URL outside HTTPS host allowlist")
        # Even robots retrieval must not follow a redirect into an arbitrary page.
        if parts.path != "/robots.txt":
            policy = self.robots.get(parts.netloc)
            if policy is None or not policy.allowed(url):
                raise ValueError("robots missing or disallowed")

    def get(self, url, *, headers=None, data=None, cap=None):
        self.check(url)
        policy = self.robots.get(urlsplit(url).netloc)
        delay = max(self.profile["delay_seconds"], (policy.delay or 0) if policy else 0)
        time.sleep(max(0.0, self.last_request + delay - time.monotonic()))
        self.last_request = time.monotonic()
        cap = cap or self.profile["max_xml_bytes"]
        request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
        request_headers.update(headers or {})
        with self.opener.open(Request(url, headers=request_headers, data=data),
                              timeout=self.profile["timeout_seconds"]) as response:
            self.check(response.geturl())
            length = response.headers.get("Content-Length")
            if length and int(length) > cap:
                raise ValueError("response exceeds limit")
            chunks, total = [], 0
            started = time.monotonic()
            while True:
                if time.monotonic() - started > self.profile["timeout_seconds"] * 3:
                    raise TimeoutError("response wall-clock budget exceeded")
                chunk = response.read(min(65536, cap + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    raise ValueError("response exceeds limit")
                chunks.append(chunk)
            if length and total != int(length):
                raise ValueError("incomplete response")
            return b"".join(chunks), {"final_url": response.geturl(), "status": response.status,
                                       "content_type": response.headers.get("Content-Type", ""),
                                       "content_disposition": response.headers.get("Content-Disposition", ""),
                                       "etag": response.headers.get("ETag"),
                                       "last_modified": response.headers.get("Last-Modified")}

    def prepare_robots(self):
        receipts = []
        # Exact-host policies are never reused across distinct hosts.
        for host in self.profile["allowed_hosts"]:
            url = "https://" + host + "/robots.txt"
            try:
                body, meta = self.get(url)
            except HTTPError as exc:
                if exc.code not in {404, 410}:
                    raise
                body, meta = b"", {"final_url": url, "status": exc.code}
            self.robots[host] = Robots(body.decode("utf-8", "replace"), USER_AGENT)
            receipts.append({"url": url, "text": body.decode("utf-8", "replace"), **meta})
        return receipts


def run_inventory(profile, data_root, fetcher=None, progress=None):
    company = profile["company_id"]
    if not re.fullmatch(r"[a-z0-9-]+", company):
        raise ValueError("unsafe company id")
    started = utc_now()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    base = Path(data_root) / "ledger" / "companies" / company / "inventory"
    run = base / "runs" / run_id
    run.mkdir(parents=True, exist_ok=False)
    fetcher = fetcher or InventoryFetcher(profile)
    summary = {"schema_version": 1, "company_id": company, "run_id": run_id,
               "started_at": started, "finished_at": None, "status": "running",
               "scope": "declared sitemap URL inventory only; NOT full website/file inventory",
               "profile_sha256": hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest(),
               "sitemaps": [], "errors": [], "known_gaps": profile["known_gaps"],
               "downloaded_product_documents_this_run": 0}
    atomic_json(run / "profile.json", profile)
    atomic_json(run / "status.json", summary)
    try:
        policies = fetcher.prepare_robots()
        atomic_json(run / "robots.json", policies)
    except Exception as exc:
        summary.update(status="blocked_robots", finished_at=utc_now())
        summary["errors"].append({"stage": "robots", "error": type(exc).__name__, "detail": str(exc)[:300]})
        atomic_json(run / "status.json", summary)
        return summary
    found, root_sources = {}, []
    sources = [{"role": "sitemap_index", "url": profile["sitemap_index"]}] + profile["sitemaps"]
    for source in sources:
        try:
            body, meta = fetcher.get(source["url"])
            digest = hashlib.sha256(body).hexdigest()
            snapshot = base / "snapshots" / (digest + ".xml")
            if not snapshot.exists():
                atomic_bytes(snapshot, body)
            elif hashlib.sha256(snapshot.read_bytes()).hexdigest() != digest:
                raise ValueError("existing snapshot integrity failure")
            kind, rows = parse_sitemap(body)
            if source["role"] == "sitemap_index":
                if kind != "sitemapindex":
                    raise ValueError("expected sitemap index")
                root_sources = [r["loc"] for r in rows]
            elif kind != "urlset":
                raise ValueError("nested sitemap index requires explicit expansion")
            else:
                for row in rows:
                    item = found.setdefault(row["loc"], {"url": row["loc"], "kind_hint": guess_kind(row["loc"]),
                                                         "sources": [], "download_status": "not_requested"})
                    item["sources"].append({"sitemap": source["url"], "role": source["role"],
                                            "lastmod_claim": row.get("lastmod")})
            entry = {**source, **meta, "entries": len(rows), "sha256": digest,
                     "snapshot": str(snapshot.relative_to(data_root)), "observed_at": utc_now()}
            summary["sitemaps"].append(entry)
            if progress:
                progress(entry)
        except Exception as exc:
            summary["errors"].append({**source, "error": type(exc).__name__, "detail": str(exc)[:300]})
        atomic_json(run / "status.json", summary)
    # Persist every origin; do not merge language variants or distinct query strings.
    rows = sorted(found.values(), key=lambda item: item["url"])
    body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode()
    atomic_bytes(run / "urls.jsonl", body)
    selected = {r["url"] for r in profile["sitemaps"]}
    summary.update(status="sitemaps_complete" if not summary["errors"] else "sitemaps_incomplete",
                   finished_at=utc_now(), unique_url_candidates=len(rows),
                   kind_hints=dict(Counter(r["kind_hint"] for r in rows)),
                   additional_sitemaps_not_traversed=[u for u in root_sources if u not in selected],
                   url_manifest=str((run / "urls.jsonl").relative_to(data_root)),
                   url_manifest_sha256=hashlib.sha256(body).hexdigest(),
                   report_path=str(run / "status.json"), website_coverage_complete=False)
    atomic_json(run / "status.json", summary)
    # Keep last successful and last attempted pointers separate.
    atomic_json(base / "latest-attempt.json", summary)
    if not summary["errors"]:
        atomic_json(base / "latest-complete-sitemaps.json", summary)
    return summary
