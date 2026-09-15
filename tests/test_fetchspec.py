from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from fetchspec.catalog import compile_rule, load_rules
from fetchspec.crawl import Fetcher, crawl
from fetchspec.match import host_allowed, path_allowed, route_product_line
from fetchspec.robots import Robots
from fetchspec.store import Store


class MatchTests(unittest.TestCase):
    def test_host_and_path(self):
        rule = compile_rule({
            "rule_id": "t", "company_id": "nvidia", "company_en": "NVIDIA",
            "ecosystems": ["compute"], "allowed_hosts": ["nvidia.com"],
            "start_urls": ["https://www.nvidia.com/en-us/data-center/h100/"],
            "path_include": ["/en-us/data-center/", "\\.pdf$"],
            "path_exclude": ["infiniband", "connectx"],
            "product_lines": [{"product_line": "g", "library_path": "library/n/", "bom_parts": ["gpu"], "path_hints": ["h100"]}],
        })
        self.assertTrue(host_allowed("www.nvidia.com", rule["allowed_hosts"]))
        self.assertTrue(path_allowed("https://www.nvidia.com/en-us/data-center/h100/", rule))
        self.assertFalse(path_allowed("https://www.nvidia.com/en-us/networking/infiniband/", rule))
        line = route_product_line("https://www.nvidia.com/en-us/data-center/h100/", rule)
        self.assertEqual(line["product_line"], "g")

    def test_robots_disallow(self):
        robots = Robots("User-agent: *\nDisallow: /content/g/\nAllow: /en-us/\n", "InResearchFetchspec/0.1")
        self.assertFalse(robots.allowed("https://nvidia.com/content/g/secret.pdf"))
        self.assertTrue(robots.allowed("https://nvidia.com/en-us/data-center/"))


class LocalSite(ThreadingHTTPServer):
    allow_reuse_address = True


def start_site(pages):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path not in pages:
                self.send_error(404)
                return
            body, ctype = pages[path]
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = LocalSite(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class CrawlTests(unittest.TestCase):
    def test_demo_rules_load(self):
        rules = load_rules(demo_only=True)
        ids = {r["company_id"] for r in rules}
        self.assertEqual(ids, {"nvidia", "intel", "supermicro", "vertiv"})

    def test_local_fetch_and_store(self):
        html = b"""<html><a href="/ds.pdf">H100 datasheet</a><a href="/skip">careers</a></html>"""
        pdf = b"%PDF-1.4 demo"
        server = start_site({
            "/robots.txt": (b"User-agent: *\nAllow: /\n", "text/plain"),
            "/gpu/": (html, "text/html"),
            "/ds.pdf": (pdf, "application/pdf"),
        })
        host = f"127.0.0.1:{server.server_address[1]}"
        try:
            rule = compile_rule({
                "rule_id": "local", "company_id": "nvidia", "company_en": "NVIDIA",
                "ecosystems": ["compute"], "allowed_hosts": ["127.0.0.1"],
                "start_urls": [f"http://{host}/gpu/"],
                "path_include": ["/gpu/", "\\.pdf$"],
                "path_exclude": ["skip"],
                "link_text_include": ["datasheet", "h100"],
                "discover": ["pdf"],
                "product_lines": [{
                    "product_line": "训练/推理GPU",
                    "library_path": "library/3-算力芯片与核心器件/NVIDIA/训练-推理GPU/",
                    "bom_parts": ["gpu"],
                    "path_hints": ["gpu", "h100"],
                }],
                "limits": {"delay_seconds": 0.5, "max_pages": 3, "max_assets": 3, "max_depth": 2, "max_bytes": 10000},
            })
            with TemporaryDirectory() as tmp:
                result = crawl(rule, tmp, dry_run=False, clock=lambda _s: None)
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["counts"]["assets"], 1)
                self.assertTrue(result["assets"][0]["sha256"])
                blob = Path(tmp) / result["assets"][0]["blob"]
                self.assertEqual(blob.read_bytes(), pdf)
        finally:
            server.shutdown()
            server.server_close()

    def test_store_idempotent(self):
        with TemporaryDirectory() as tmp:
            store = Store(tmp)
            a, rel, _ = store.put(b"hello", ".pdf", "library/x/", "a.pdf")
            b, rel2, _ = store.put(b"hello", ".pdf", "library/x/", "b.pdf")
            self.assertEqual(a, b)
            self.assertEqual(rel, rel2)
            self.assertEqual(len(list(Path(tmp, "blobs").rglob("*.pdf"))), 1)


if __name__ == "__main__":
    unittest.main()
