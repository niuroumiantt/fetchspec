from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
import json
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler

from .match import host_allowed, keyword_ok, looks_pdf, path_allowed, route_product_line
from .robots import Robots
from .store import Store, now

USER_AGENT = "InResearchFetchspec/0.1 (+https://github.com/niuroumiantt/fetchspec)"


class BoundedRedirect(HTTPRedirectHandler):
    def __init__(self, allowed_hosts):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not host_allowed(urlsplit(newurl).hostname, self.allowed_hosts):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class LinkParser(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base = base
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self._flush()
            self._href = urljoin(self.base, attrs["href"])
            self._text = []

    def handle_data(self, data):
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a":
            self._flush()

    def _flush(self):
        if self._href:
            self.links.append((self._href.split("#", 1)[0], "".join(self._text).strip()))
            self._href = None
            self._text = []


def canonicalize(url):
    parts = urlsplit(url)
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


class Fetcher:
    def __init__(self, allowed_hosts, timeout=30):
        ctx = ssl.create_default_context()
        opener = build_opener(BoundedRedirect(allowed_hosts), HTTPSHandler(context=ctx))
        self.opener = opener
        self.timeout = timeout

    def get(self, url):
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,*/*"})
        with self.opener.open(req, timeout=self.timeout) as resp:
            body = resp.read()
            return {
                "url": resp.geturl(),
                "status": getattr(resp, "status", 200),
                "content_type": resp.headers.get("Content-Type", ""),
                "body": body,
            }


def robots_txt_url(url):
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, "/robots.txt", "", ""))


def load_robots(fetcher, page_url, cache):
    key = robots_txt_url(page_url)
    if key in cache:
        return cache[key]
    try:
        result = fetcher.get(key)
        text = result["body"].decode("utf-8", "replace")
    except (HTTPError, URLError, TimeoutError, ssl.SSLError):
        text = ""
    robots = Robots(text, USER_AGENT)
    cache[key] = robots
    return robots


def classify(url, content_type, rule):
    if looks_pdf(url, content_type):
        return "pdf"
    if "html" in (content_type or "").lower() or urlsplit(url).path.lower().endswith((".html", ".htm", "/")):
        return "html"
    if "html" in (content_type or "").lower():
        return "html"
    return "other"


def crawl(rule, out_dir, dry_run=True, fetcher=None, clock=time.sleep):
    limits = rule["limits"]
    fetcher = fetcher or Fetcher(rule["allowed_hosts"])
    store = Store(out_dir)
    robots_cache = {}
    seen = set()
    queue = [(canonicalize(url), 0, "") for url in rule["start_urls"]]
    pages = []
    assets = []
    skipped = []
    errors = []
    last_host = None

    def pause(page_url, host):
        nonlocal last_host
        robots = load_robots(fetcher, page_url, robots_cache)
        delay = max(limits["delay_seconds"], robots.delay or 0)
        if last_host is not None:
            clock(delay)
        last_host = host

    while queue:
        url, depth, via = queue.pop(0)
        if url in seen:
            continue
        host = urlsplit(url).hostname
        if not host_allowed(host, rule["allowed_hosts"]):
            skipped.append({"url": url, "reason": "host"})
            continue
        if not path_allowed(url, rule) and url not in {canonicalize(u) for u in rule["start_urls"]}:
            skipped.append({"url": url, "reason": "path"})
            continue
        if dry_run:
            seen.add(url)
            kind = "pdf" if looks_pdf(url) else "html"
            record = {"url": url, "depth": depth, "via": via, "kind": kind, "dry_run": True}
            if kind == "pdf":
                if len(assets) < limits["max_assets"]:
                    assets.append(record)
            else:
                if len(pages) >= limits["max_pages"]:
                    continue
                pages.append(record)
                if depth < limits["max_depth"]:
                    # dry-run cannot discover child links without fetch
                    pass
            continue
        robots = load_robots(fetcher, url, robots_cache)
        if not robots.allowed(url):
            skipped.append({"url": url, "reason": "robots"})
            continue
        seen.add(url)
        pdf_seed = looks_pdf(url)
        if pdf_seed and len(assets) >= limits["max_assets"]:
            skipped.append({"url": url, "reason": "asset_cap"})
            continue
        if not pdf_seed and len(pages) >= limits["max_pages"]:
            skipped.append({"url": url, "reason": "page_cap"})
            continue
        try:
            pause(url, host)
            result = fetcher.get(url)
        except HTTPError as exc:
            errors.append({"url": url, "error": f"http_{exc.code}"})
            continue
        except Exception as exc:
            errors.append({"url": url, "error": type(exc).__name__})
            continue
        final = canonicalize(result["url"])
        kind = classify(final, result["content_type"], rule)
        if kind not in rule["asset_types"] and kind != "html":
            skipped.append({"url": final, "reason": "type"})
            continue
        if len(result["body"]) > limits["max_bytes"]:
            skipped.append({"url": final, "reason": "too_large"})
            continue
        line = route_product_line(final, rule)
        if kind == "pdf":
            if len(assets) >= limits["max_assets"]:
                continue
            record = store.capture(
                result["body"], ".pdf", "pdf", final, url,
                result["status"], result["content_type"], rule, line,
            )
            assets.append({
                "url": final, "requested": url, "kind": "pdf",
                "sha256": record["sha256"], "blob": record["blob"],
                "library": record["file_path"], "product_line": record["product_line"],
                "model": record["model"], "doc_type": record["doc_type"],
                "status": result["status"], "bytes": record["bytes"],
            })
            continue
        if kind != "html":
            skipped.append({"url": final, "reason": "type"})
            continue
        if len(pages) >= limits["max_pages"]:
            continue
        page = {
            "url": final, "requested": url, "kind": "html", "status": result["status"],
            "bytes": len(result["body"]), "product_line": line["product_line"],
        }
        if rule["save_html"]:
            record = store.capture(
                result["body"], ".html", "html", final, url,
                result["status"], result["content_type"], rule, line,
            )
            page.update({
                "sha256": record["sha256"], "blob": record["blob"],
                "library": record["file_path"], "model": record["model"],
                "doc_type": record["doc_type"],
            })
        pages.append(page)
        if depth >= limits["max_depth"]:
            continue
        try:
            html = result["body"].decode("utf-8", "replace")
        except Exception:
            continue
        parser = LinkParser(final)
        try:
            parser.feed(html)
            parser.close()
        except Exception:
            continue
        discover = set(rule.get("discover") or ["pdf", "html"])
        remaining_pages = limits["max_pages"] - len(pages)
        remaining_assets = limits["max_assets"] - len(assets)
        queued_pdf = sum(1 for item in queue if looks_pdf(item[0]))
        queued_html = len(queue) - queued_pdf
        for href, text in parser.links:
            child = canonicalize(href)
            if child in seen:
                continue
            if not host_allowed(urlsplit(child).hostname, rule["allowed_hosts"]):
                continue
            pdf = looks_pdf(child)
            if pdf and "pdf" not in discover:
                continue
            if not pdf and "html" not in discover:
                continue
            if not path_allowed(child, rule):
                continue
            if not keyword_ok(child, text, rule, pdf):
                continue
            if pdf:
                if remaining_assets - queued_pdf <= 0:
                    continue
                queued_pdf += 1
            else:
                if remaining_pages - queued_html <= 0:
                    continue
                queued_html += 1
            queue.append((child, depth + 1, final))

    run_id = f"{rule['rule_id']}-{now().replace(':', '').replace('.', '')}"
    payload = {
        "run_id": run_id,
        "rule_id": rule["rule_id"],
        "company_id": rule["company_id"],
        "dry_run": dry_run,
        "started": now(),
        "user_agent": USER_AGENT,
        "pages": pages,
        "assets": assets,
        "skipped": skipped[:200],
        "errors": errors,
        "counts": {
            "pages": len(pages),
            "assets": len(assets),
            "skipped": len(skipped),
            "errors": len(errors),
            "seen": len(seen),
        },
    }
    path = store.write_run(run_id, payload)
    payload["run_file"] = str(path)
    return payload
