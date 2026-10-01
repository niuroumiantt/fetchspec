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
FORMATS = {"pdf", "doc", "docx", "docm", "dot", "dotx", "dotm",
           "xls", "xlsx", "xlsm", "xlsb", "xlt", "xltx", "xltm", "csv",
           "ppt", "pptx", "pptm", "pps", "ppsx", "ppsm", "pot", "potx", "potm",
           "rtf", "odt", "ods", "odp"}


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
        allowed_hosts = self.profile.get("allowed_hosts_by_host", {}).get(parts.hostname, self.profile["allowed_hosts"])
        if parts.hostname not in allowed_hosts or parts.scheme != "https" or parts.port not in (None, 443):
            raise ValueError("redirect or URL outside HTTPS host allowlist")
        # Even robots retrieval must not follow a redirect into an arbitrary page.
        if parts.path != "/robots.txt":
            policy = self.robots.get(parts.netloc)
            # Unavailable robots is a per-run condition (a dropped connection at
            # startup); a disallow rule is the site's decision.  Keep them apart
            # so only the latter is recorded as a terminal block.
            if policy is None:
                raise ValueError("robots unavailable for host")
            if not policy.allowed(url):
                raise ValueError("robots disallowed")

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
            # urllib's response.read(n) may wait until all n bytes arrive. A
            # slow-trickling CDN can therefore hold one 64 KiB read past the
            # wall-clock budget before control returns here. Read only the
            # bytes already available from http.client's buffered response so
            # the deadline is checked throughout a long transfer.
            # HTTPResponse.read1 decodes chunked transfer encoding; its raw .fp does not, so a
            # chunked PDF read from .fp would carry the chunk-size lines inside the body.
            reader = response if hasattr(response, "read1") else getattr(response, "fp", response)
            read_chunk = getattr(reader, "read1", response.read)
            while True:
                # Per-read timeout catches stalls; this caps total transfer time.
                budget = self.profile.get("max_response_seconds", self.profile["timeout_seconds"] * 3)
                if time.monotonic() - started > budget:
                    raise TimeoutError("response wall-clock budget exceeded")
                chunk = read_chunk(min(16384, cap + 1 - total))
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

    def _get_robots(self, url, attempts=3):
        # One dropped connection must not close a whole host for the run.
        for attempt in range(attempts):
            try:
                return self.get(url)
            except (HTTPError, ValueError):
                raise
            except Exception:
                if attempt == attempts - 1:
                    raise
                time.sleep(2 * (attempt + 1))

    def prepare_robots(self):
        receipts = []
        # Exact-host policies are never reused across distinct hosts.
        for host in self.profile["robots_hosts"] if "robots_hosts" in self.profile else self.profile["allowed_hosts"]:
            url = "https://" + host + "/robots.txt"
            try:
                body, meta = self._get_robots(url)
            except HTTPError as exc:
                if exc.code not in {404, 410}:
                    receipts.append({"url": url, "status": "blocked", "error": str(exc)[:300]})
                    continue
                body, meta = b"", {"final_url": url, "status": exc.code}
            except Exception as exc:
                # An unavailable optional host must not open its robots policy or
                # prevent acquisition from independently verified allowed hosts.
                receipts.append({"url": url, "status": "blocked", "error": type(exc).__name__ + ": " + str(exc)[:300]})
                continue
            self.robots[host] = Robots(body.decode("utf-8", "replace"), USER_AGENT)
            receipts.append({"url": url, "text": body.decode("utf-8", "replace"), **meta})
        if not self.robots:
            raise ValueError("robots unavailable for every declared robots host: " + str(receipts))
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
        summary["blocked_hosts"] = [r for r in policies if r.get("status") == "blocked"]
    except Exception as exc:
        summary.update(status="blocked_robots", finished_at=utc_now())
        summary["errors"].append({"stage": "robots", "error": type(exc).__name__, "detail": str(exc)[:300]})
        atomic_json(run / "status.json", summary)
        return summary
    found, root_sources = {}, []
    # Durable URL-level baseline. Sitemap omissions are observations, not
    # deletion evidence, so absent records remain preserved for review.
    state_path = base / "current_state.json"
    try:
        prior_state = json.loads(state_path.read_text()) if state_path.exists() else {"urls": {}}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("existing sitemap state is unreadable; inspect before continuing") from exc
    complete_path = base / "latest-complete-sitemaps.json"
    if complete_path.is_file():
        try:
            complete = json.loads(complete_path.read_text())
            if complete.get("status") == "sitemaps_complete" and prior_state.get("run_id") != complete.get("run_id"):
                manifest_path = (Path(data_root) / complete["url_manifest"]).resolve()
                if manifest_path.is_relative_to(Path(data_root).resolve()) and manifest_path.is_file():
                    restored_urls = {}
                    for line in manifest_path.read_text().splitlines():
                        item = json.loads(line)
                        fingerprint = hashlib.sha256(json.dumps(item.get("sources", []), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                        restored_urls[item["url"]] = {"kind_hint": item.get("kind_hint", "page_or_download_endpoint"),
                            "source_fingerprint": fingerprint, "sources": item.get("sources", []),
                            "last_seen_run": complete["run_id"], "last_seen_at": complete.get("finished_at"), "status": "observed"}
                    restored_sitemaps = {s["url"]: {"sha256": s.get("sha256"), "etag": s.get("etag"),
                        "last_modified": s.get("last_modified"), "entries": s.get("entries"), "snapshot": s.get("snapshot"),
                        "observed_at": s.get("observed_at")} for s in complete.get("sitemaps", [])}
                    prior_state = {"run_id": complete["run_id"], "urls": restored_urls,
                                   "sitemaps": restored_sitemaps, "status": "sitemaps_complete"}
        except (OSError, KeyError, json.JSONDecodeError):
            pass
    sources = [{"role": "sitemap_index", "url": profile["sitemap_index"]}] + profile["sitemaps"]
    source_keys = {(row["role"], row["url"]) for row in sources}
    index_body = None
    index_meta = None
    source_state = dict(prior_state.get("sitemaps", {}))
    for source in sources:
        if source["role"] == "sitemap_index":
            try:
                index_body, index_meta = fetcher.get(source["url"])
            except Exception:
                index_body = None
            break
    index_errors = []
    # Traverse only index entries explicitly selected by a profile. This keeps
    # scope reviewable while supporting the vendor's published sitemap topology.
    for selector in profile.get("sitemap_index_select", []):
        try:
            if index_body is None:
                raise ValueError("sitemap index unavailable")
            _, index_rows = parse_sitemap(index_body)
            for index_row in index_rows:
                url = index_row["loc"]
                if re.search(selector["url_regex"], url, re.I):
                    item = {"role": selector["role_prefix"] + urlsplit(url).path.strip("/").split("/")[-1].replace(".sitemap.xml", ""), "url": url}
                    key = (item["role"], item["url"])
                    if key not in source_keys:
                        sources.append(item)
                        source_keys.add(key)
        except Exception as exc:
            index_errors.append({"role": selector["role_prefix"], "url": profile["sitemap_index"],
                                 "error": type(exc).__name__, "detail": str(exc)[:300]})
    for source in sources:
        try:
            prior_source = prior_state.get("sitemaps", {}).get(source["url"], {})
            validators = {}
            if prior_source.get("etag"):
                validators["If-None-Match"] = prior_source["etag"]
            if prior_source.get("last_modified"):
                validators["If-Modified-Since"] = prior_source["last_modified"]
            try:
                if source["role"] == "sitemap_index" and index_body is not None:
                    body, meta = index_body, index_meta
                else:
                    body, meta = fetcher.get(source["url"], headers=validators)
            except HTTPError as exc:
                if exc.code != 304 or not prior_source.get("snapshot"):
                    raise
                cached_snapshot = (Path(data_root) / prior_source["snapshot"]).resolve()
                body = cached_snapshot.read_bytes()
                if hashlib.sha256(body).hexdigest() != prior_source.get("sha256"):
                    raise ValueError("cached sitemap snapshot integrity failure")
                meta = {"final_url": source["url"], "status": 304, "content_type": "application/xml",
                        "etag": prior_source.get("etag"), "last_modified": prior_source.get("last_modified")}
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
            source_state[source["url"]] = {"sha256": digest, "etag": meta.get("etag"),
                "last_modified": meta.get("last_modified"), "observed_at": entry["observed_at"], "entries": len(rows),
                "snapshot": str(snapshot.relative_to(data_root))}
            if progress:
                progress(entry)
        except Exception as exc:
            summary["errors"].append({**source, "error": type(exc).__name__, "detail": str(exc)[:300]})
        atomic_json(run / "status.json", summary)
    # Persist every origin; do not merge language variants or distinct query strings.
    rows = sorted(found.values(), key=lambda item: item["url"])
    previous_urls = prior_state.get("urls", {})
    now_urls, changes = {}, Counter()
    for item in rows:
        url = item["url"]
        fingerprint = hashlib.sha256(json.dumps(item["sources"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        old = previous_urls.get(url)
        status = "new" if old is None else ("changed" if old.get("source_fingerprint") != fingerprint else "unchanged")
        item["map_status"] = status
        changes[status] += 1
        now_urls[url] = {"kind_hint": item["kind_hint"], "source_fingerprint": fingerprint,
                         "sources": item["sources"], "last_seen_run": run_id,
                         "last_seen_at": utc_now(), "status": status}
    selected_urls = {r["url"] for r in sources}
    selected_errors = [error for error in summary["errors"] if error.get("url") in selected_urls]
    observation_complete = not selected_errors and not index_errors
    for url in sorted(set(previous_urls) - set(now_urls)):
        if observation_complete:
            now_urls[url] = {**previous_urls[url], "status": "absent_from_sitemap",
                             "absence_observed_run": run_id, "absence_observed_at": utc_now()}
            changes["absent_from_sitemap"] += 1
        else:
            now_urls[url] = {**previous_urls[url], "status": "not_observed_due_to_incomplete_sitemap_run"}
    body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode()
    atomic_bytes(run / "urls.jsonl", body)
    summary["index_selection_errors"] = index_errors
    summary["unselected_index_sitemaps"] = [u for u in root_sources if u not in selected_urls]
    summary.update(status="sitemaps_complete" if not selected_errors and not index_errors else "sitemaps_incomplete",
                   finished_at=utc_now(), unique_url_candidates=len(rows),
                   kind_hints=dict(Counter(r["kind_hint"] for r in rows)),
                   additional_sitemaps_not_traversed=summary["unselected_index_sitemaps"],
                   url_manifest=str((run / "urls.jsonl").relative_to(data_root)),
                   url_manifest_sha256=hashlib.sha256(body).hexdigest(),
                   report_path=str(run / "status.json"), website_coverage_complete=False)
    summary["reconciliation"] = {"new": changes["new"], "changed": changes["changed"],
        "unchanged": changes["unchanged"], "absent_from_sitemap": changes["absent_from_sitemap"],
        "absence_is_not_deletion": True, "previous_run": prior_state.get("run_id")}
    atomic_json(state_path, {"schema_version": 1, "company_id": company, "run_id": run_id,
        "status": summary["status"], "observed_at": summary["finished_at"], "sitemaps": source_state, "urls": now_urls,
        "reconciliation": summary["reconciliation"]})
    atomic_json(run / "status.json", summary)
    # Keep last successful and last attempted pointers separate.
    atomic_json(base / "latest-attempt.json", summary)
    if summary["status"] == "sitemaps_complete":
        atomic_json(base / "latest-complete-sitemaps.json", summary)
    return summary
