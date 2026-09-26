import copy
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError
import zipfile

from fetchspec.company import (CompanyLedger, NvidiaAdapter, PageLinks, SupermicroAdapter, document_kind,
                               format_summary, import_inventory, progress_summary, run_company)
from fetchspec.inventory import InventoryFetcher, load_profile, parse_sitemap, run_inventory
from fetchspec.robots import Robots


PDF = b"%PDF-1.4\nfixture one\n%%EOF\n"
PDF2 = b"%PDF-1.4\nfixture changed revision\n%%EOF\n"
BASE = "https://www.supermicro.com"
NVIDIA = "https://www.nvidia.com"


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def prepare_robots(self):
        return [{"url": BASE + "/robots.txt", "text": "User-agent: *\nAllow: /"}]

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        body, kind = value
        return body, {"status": 200, "final_url": url, "content_type": kind, "etag": '"' + hashlib.sha256(body).hexdigest() + '"'}


class RobotsTests(unittest.TestCase):
    def test_supermicro_wildcard_and_anchor(self):
        robots = Robots("User-agent: *\nDisallow: /wftp/*\nDisallow: /*?secret=$\nAllow: /wftp/public/\n", "InResearchFetchspec/0.2")
        self.assertFalse(robots.allowed(BASE + "/wftp/a.pdf"))
        self.assertTrue(robots.allowed(BASE + "/wftp/public/a.pdf"))
        self.assertFalse(robots.allowed(BASE + "/x?secret="))
        self.assertTrue(robots.allowed(BASE + "/x?secret=no"))

    def test_matching_groups_and_tie(self):
        r = Robots("User-agent: *\nDisallow: /\nUser-agent: InResearchFetchspec\nDisallow: /a\nUser-agent: InResearchFetchspec\nAllow: /a\nDisallow: /b\n", "InResearchFetchspec/0.2")
        self.assertTrue(r.allowed(BASE + "/a"))
        self.assertFalse(r.allowed(BASE + "/b"))
        self.assertTrue(r.allowed(BASE + "/c"))

    def test_fetcher_fail_closed_and_redirect_allowlist(self):
        f = InventoryFetcher(load_profile("supermicro"))
        with self.assertRaises(ValueError):
            f.check(BASE + "/manuals/a.pdf")
        f.robots["www.supermicro.com"] = Robots("User-agent: *\nDisallow: /wftp/*", "InResearchFetchspec/0.2")
        f.check(BASE + "/manuals/a.pdf")
        for url in [BASE + "/wftp/a.pdf", "https://www.supermicro.com.attacker.test/a.pdf", "https://www.supermicro.com:8080/a.pdf", "http://www.supermicro.com/a.pdf"]:
            with self.assertRaises(ValueError):
                f.check(url)

    def test_optional_host_tls_failure_stays_blocked(self):
        f = InventoryFetcher(load_profile("supermicro"))
        def fake_get(url):
            if url == "https://supermicro.com/robots.txt":
                raise ValueError("certificate verification failure")
            return b"User-agent: *\nDisallow: /wftp/*", {"status": 200, "final_url": url}
        f.get = fake_get
        receipts = f.prepare_robots()
        self.assertEqual(receipts[1]["status"], "blocked")
        f.check(BASE + "/manuals/test.pdf")
        with self.assertRaises(ValueError):
            f.check("https://supermicro.com/manuals/test.pdf")


class DiscoveryTests(unittest.TestCase):
    def test_nvidia_adapter_scope_categories_and_documents(self):
        adapter = NvidiaAdapter(load_profile("nvidia"))
        self.assertTrue(adapter.in_scope(NVIDIA + "/en-us/data-center/h100/"))
        self.assertFalse(adapter.in_scope(NVIDIA + "/de-de/data-center/h100/"))
        # Research scope: regional English copies and non-research sections are skipped.
        self.assertFalse(adapter.in_scope(NVIDIA + "/en-gb/data-center/h100/"))
        self.assertFalse(adapter.in_scope(NVIDIA + "/zh-tw/data-center/h100/"))
        self.assertTrue(adapter.in_scope(NVIDIA + "/zh-cn/networking/"))
        self.assertTrue(adapter.in_scope(NVIDIA + "/en-us/products/workstations/"))
        self.assertFalse(adapter.in_scope(NVIDIA + "/en-us/geforce/news/x/"))
        self.assertFalse(adapter.in_scope(NVIDIA + "/en-us/drivers/details/1/"))
        self.assertFalse(adapter.in_scope(NVIDIA + "/en-us/on-demand/session/x/"))
        self.assertFalse(adapter.in_scope(NVIDIA + "/gtc/session-catalog/"))
        self.assertFalse(adapter.in_scope(NVIDIA + "/content/dam/docs/datasheet-fr.pdf"))
        self.assertFalse(adapter.in_scope(NVIDIA + "/content/dam/docs/datasheet.pdf?language=de-de"))
        self.assertTrue(adapter.in_scope(NVIDIA + "/content/dam/docs/datasheet-zh-cn.pdf"))
        self.assertTrue(adapter.in_scope("https://www.nvidia.cn/zh-cn/data-center/h100/"))
        self.assertTrue(adapter.in_scope(NVIDIA + "/content/dam/en-zz/Solutions/Data-Center/a100/a.pdf"))
        self.assertFalse(adapter.in_scope(NVIDIA + "/content/gated/a.pdf"))
        self.assertFalse(adapter.in_scope(NVIDIA + "/en-us/data-center/h100/hero.jpg"))
        self.assertEqual(adapter.categories(NVIDIA + "/en-us/data-center/h100/"), ["Data Center & AI"])
        self.assertEqual(adapter.categories("https://www.nvidia.cn/zh-cn/data-center/h100/"), ["Data Center & AI"])
        self.assertEqual(adapter.normalize("/content/dam/a.pdf?ncid=tracking&utm_source=x&version=2", NVIDIA + "/en-us/data-center/h100/"),
                         NVIDIA + "/content/dam/a.pdf?version=2")
        _, links = adapter.discover('<main><a href="/content/dam/a.pdf">Datasheet</a></main>', NVIDIA + "/en-us/data-center/h100/")
        self.assertTrue(any(row["url"] == NVIDIA + "/content/dam/a.pdf" for row in links))

    def test_sitemap_loc_not_image_loc(self):
        kind, rows = parse_sitemap(b'<urlset xmlns:image="urn:image"><url><loc>https://www.supermicro.com/en/products/a</loc><image:image><image:loc>https://www.supermicro.com/a.jpg</image:loc></image:image></url></urlset>')
        self.assertEqual(kind, "urlset")
        self.assertEqual(len(rows), 1)
        with self.assertRaises(ValueError):
            parse_sitemap(b'<!DOCTYPE a><urlset/>')

    def test_form_and_javascript_literals_without_execution(self):
        adapter = SupermicroAdapter(load_profile("supermicro"))
        html = '''<header><a href="/global.pdf">Menu PDF</a></header><main>
        <a href="javascript:redirect('/manuals/test.pdf');">Manual</a>
        <form action="/support/resources/results.php" method="post"><input type="hidden" name="ProductID" value="123"><input type="hidden" name="Resource" value="Manuals"><input type="hidden" name="ProductName" value="test"></form>
        <form action="/delete" method="post"><input type="hidden" name="ProductID" value="123"><input type="hidden" name="Resource" value="Manuals"></form>
        </main>'''
        parser, links = adapter.discover(html, BASE + "/en/products/a")
        self.assertTrue(any(r["url"].endswith("/manuals/test.pdf") and r["context"] == "page_body" for r in links))
        self.assertTrue(any(r["url"].endswith("/global.pdf") and r["context"] == "site_navigation" for r in links))
        self.assertEqual(len([r for r in links if r["method"] == "POST"]), 1)
        self.assertTrue(adapter.in_scope(BASE + "/manuals/spec.xlsx?version=2"))
        self.assertFalse(adapter.in_scope("https://evil.test/spec.pdf"))

    def test_inventory_complete_is_not_website_complete(self):
        p = load_profile("supermicro")
        p["sitemaps"] = [{"role": "test", "url": BASE + "/sitemap.xml"}]
        f = FakeFetcher({p["sitemap_index"]: (b'<sitemapindex><sitemap><loc>https://www.supermicro.com/sitemap.xml</loc></sitemap></sitemapindex>', 'application/xml'),
                         BASE + "/sitemap.xml": (b'<urlset><url><loc>https://www.supermicro.com/en/products/a</loc></url></urlset>', 'application/xml')})
        with TemporaryDirectory() as tmp:
            report = run_inventory(p, Path(tmp), fetcher=f)
            self.assertEqual(report["unique_url_candidates"], 1)
            self.assertFalse(report["website_coverage_complete"])
            self.assertEqual(report["downloaded_product_documents_this_run"], 0)

    def test_inventory_selects_published_sitemaps_from_index(self):
        p = load_profile("nvidia")
        index = b'<sitemapindex><sitemap><loc>https://www.nvidia.com/en-us/en-us.sitemap.xml</loc></sitemap><sitemap><loc>https://www.nvidia.com/fr-fr/fr-fr.sitemap.xml</loc></sitemap><sitemap><loc>https://www.nvidia.com/zh-tw/zh-tw.sitemap.xml</loc></sitemap><sitemap><loc>https://www.nvidia.com/zh-cn/zh-cn.sitemap.xml</loc></sitemap><sitemap><loc>https://www.nvidia.com/gtc/sitemap_sessions.xml</loc></sitemap></sitemapindex>'
        page = b'<urlset><url><loc>https://www.nvidia.com/en-us/data-center/a</loc></url></urlset>'
        f = FakeFetcher({p["sitemap_index"]: (index, "application/xml"),
                         "https://www.nvidia.com/en-us/en-us.sitemap.xml": (page, "application/xml"),
                         "https://www.nvidia.com/zh-cn/zh-cn.sitemap.xml": (page, "application/xml"),
                         "https://www.nvidia.com/gtc/sitemap_sessions.xml": (page, "application/xml")})
        with TemporaryDirectory() as tmp:
            report = run_inventory(p, Path(tmp), fetcher=f)
        urls = {call[0] for call in f.calls}
        self.assertIn("https://www.nvidia.com/zh-cn/zh-cn.sitemap.xml", urls)
        self.assertNotIn("https://www.nvidia.com/zh-tw/zh-tw.sitemap.xml", urls)
        self.assertNotIn("https://www.nvidia.com/fr-fr/fr-fr.sitemap.xml", urls)
        self.assertNotIn("https://www.nvidia.com/gtc/sitemap_sessions.xml", urls)
        self.assertEqual(report["unique_url_candidates"], 1)

    def test_actual_frontend_datasheet_button_rule(self):
        a = SupermicroAdapter(load_profile("supermicro"))
        _, links = a.discover('<div class="system-blade"><h1 class="sku-model" rel=" SYS-ABC ">SKU</h1></div>', BASE + "/en/products/system/a")
        self.assertTrue(any(r["url"] == BASE + "/en/products/system/datasheet/sys-abc" for r in links))
        for html in ['<div class="system-blade"><i class="sku-model" rel="srs-abc"></i></div>', '<h1 class="sku-model" rel="sys-abc"></h1>']:
            self.assertFalse(a.discover(html, BASE + "/en/products/system/a")[1])


class DocumentTests(unittest.TestCase):
    def test_magic_not_filename(self):
        self.assertEqual(document_kind(PDF, BASE + "/download?id=1"), "pdf")
        self.assertIsNone(document_kind(b"<html>error</html>", BASE + "/file.pdf", "application/pdf"))
        with self.assertRaises(ValueError):
            document_kind(b"%PDF-1.4 truncated", BASE + "/a.pdf")

    def test_office_zip_variants(self):
        for entry, macro, expected in [("word/document.xml", False, "docx"), ("word/document.xml", True, "docm"),
                                        ("xl/workbook.xml", False, "xlsx"), ("xl/workbook.xml", True, "xlsm"), ("xl/workbook.bin", True, "xlsb")]:
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as archive:
                archive.writestr("[Content_Types].xml", "macroEnabled" if macro else "normal")
                archive.writestr(entry, "fixture")
            self.assertEqual(document_kind(data.getvalue(), BASE + "/unreliable-name"), expected)


class WorkerTests(unittest.TestCase):
    def profile(self):
        p = load_profile("supermicro")
        p["min_free_bytes"] = 0
        return p

    def test_resume_rename_dedup_and_changed_version(self):
        with TemporaryDirectory() as tmp:
            p = self.profile()
            ledger = CompanyLedger(tmp, p)
            a, b = BASE + "/datasheet/a.pdf", BASE + "/datasheet/renamed.pdf"
            ledger.enqueue(a, priority=0)
            ledger.enqueue(b, priority=0)
            ledger.db.commit(); ledger.db.close()
            f = FakeFetcher({a: (PDF, "application/pdf"), b: (PDF, "application/pdf")})
            first = run_company(p, tmp, max_requests=1, fetcher=f)
            self.assertEqual(first["queue"]["pending"], 1)
            second = run_company(p, tmp, fetcher=f)
            self.assertEqual(second["unique_document_contents"], 1)
            self.assertEqual(second["document_source_requests"], 2)
            self.assertEqual(len(list(Path(tmp, "blobs").rglob("*.pdf"))), 1)
            f.pages[a] = (PDF2, "application/pdf")
            third = run_company(p, tmp, recheck=True, force=True, fetcher=f)
            self.assertEqual(third["unique_document_contents"], 2)
            self.assertEqual(len(list(Path(tmp, "blobs").rglob("*.pdf"))), 2)
            manifest = [json.loads(s) for s in (Path(tmp) / "ledger/companies/supermicro/documents.jsonl").read_text().splitlines()]
            self.assertEqual(len(manifest), 2)
            self.assertEqual(len(manifest[0]["sources"]) + len(manifest[1]["sources"]), 3)
            self.assertFalse(third["website_coverage_complete"])

    def test_html_not_document_and_robots_block(self):
        with TemporaryDirectory() as tmp:
            p = self.profile(); ledger = CompanyLedger(tmp, p)
            a, b = BASE + "/a.pdf", BASE + "/wftp/a.pdf"
            ledger.enqueue(a); ledger.enqueue(b); ledger.db.commit(); ledger.db.close()
            f = FakeFetcher({a: (b"<html>error</html>", "text/html"), b: ValueError("robots missing or disallowed")})
            result = run_company(p, tmp, fetcher=f)
            self.assertEqual(result["unique_document_contents"], 0)
            self.assertEqual(result["queue"], {"blocked": 1, "error": 1})

    def test_nvidia_language_policy_excludes_existing_frontier_before_fetch(self):
        with TemporaryDirectory() as tmp:
            p = load_profile("nvidia")
            p["min_free_bytes"] = 0
            p["category_roots"] = []
            ledger = CompanyLedger(tmp, p)
            english = NVIDIA + "/en-us/data-center/h100/"
            german = NVIDIA + "/de-de/data-center/h100/"
            french_pdf = NVIDIA + "/content/dam/datasheet-fr.pdf"
            ledger.enqueue(english, priority=0)
            ledger.enqueue(german, priority=0)
            ledger.enqueue(french_pdf, priority=0)
            ledger.db.commit(); ledger.db.close()
            f = FakeFetcher({english: (b'<html><a href="/content/dam/datasheet-fr.pdf">French</a></html>', "text/html")})
            run_company(p, tmp, fetcher=f)
            self.assertEqual([call[0] for call in f.calls if call[0] != english], [])
            check = CompanyLedger(tmp, p)
            states = {row["url"]: row["state"] for row in check.db.execute("SELECT url,state FROM requests")}
            check.db.close()
            self.assertEqual(states[german], "excluded")
            self.assertEqual(states[french_pdf], "excluded")

    def test_stale_document_links_do_not_trip_remote_error_pause(self):
        with TemporaryDirectory() as tmp:
            p = self.profile(); ledger = CompanyLedger(tmp, p)
            urls = [BASE + f"/manuals/stale-{index}.pdf" for index in range(10)]
            for url in urls:
                ledger.enqueue(url, priority=0)
            ledger.db.commit(); ledger.db.close()
            fetcher = FakeFetcher({url: HTTPError(url, 404, "Not Found", {}, None) for url in urls})
            result = run_company(p, tmp, fetcher=fetcher)
            self.assertEqual(result["run"]["status"], "frontier_exhausted_with_gaps")
            self.assertEqual(result["queue"], {"error": 10})

    def test_page_304_does_not_skip_attachment_check(self):
        with TemporaryDirectory() as tmp:
            p = self.profile(); ledger = CompanyLedger(tmp, p)
            page, doc = BASE + "/en/products/a", BASE + "/a.pdf"
            ledger.enqueue(page); ledger.db.commit(); ledger.db.close()
            f = FakeFetcher({page: (b'<html><title>A</title><a href="/a.pdf">File</a></html>', "text/html"), doc: (PDF, "application/pdf")})
            run_company(p, tmp, fetcher=f)
            f.pages[page] = HTTPError(page, 304, "unchanged", {}, None)
            f.pages[doc] = (PDF2, "application/pdf")
            result = run_company(p, tmp, recheck=True, fetcher=f)
            self.assertEqual(result["unique_document_contents"], 2)
            self.assertEqual(result["page_snapshots"], 1)
            self.assertTrue(any(call[1]["headers"].get("If-None-Match") for call in f.calls))

    def test_progress_summary_reports_pace_errors_and_idle_worker(self):
        with TemporaryDirectory() as tmp:
            p = self.profile(); ledger = CompanyLedger(tmp, p)
            page, doc, bad = BASE + "/en/products/a", BASE + "/a.pdf", BASE + "/b.pdf"
            ledger.enqueue(page); ledger.db.commit(); ledger.db.close()
            f = FakeFetcher({page: (b'<html><a href="/a.pdf">A</a><a href="/b.pdf">B</a></html>', "text/html"),
                             doc: (PDF, "application/pdf"), bad: HTTPError(bad, 500, "boom", {}, None)})
            run_company(p, tmp, fetcher=f)
            check = CompanyLedger(tmp, p)
            summary = progress_summary(check)
            check.db.close()
            self.assertFalse(summary["worker_active"])
            self.assertEqual(summary["pending"], 0)
            self.assertEqual(summary["unique_documents"], 1)
            self.assertEqual(summary["window_observations"], 3)
            self.assertEqual(summary["error_types"], {"HTTPError": 1})
            self.assertIn("worker=stopped", format_summary(summary))

    def test_low_yield_guard_pauses_when_pages_bring_no_new_documents(self):
        with TemporaryDirectory() as tmp:
            p = self.profile(); p["max_pages_without_new_document"] = 2
            ledger = CompanyLedger(tmp, p)
            pages = [BASE + f"/en/products/p{index}" for index in range(4)]
            for page in pages:
                ledger.enqueue(page)
            ledger.db.commit(); ledger.db.close()
            f = FakeFetcher({page: (b"<html><title>x</title></html>", "text/html") for page in pages})
            result = run_company(p, tmp, fetcher=f)
            self.assertEqual(result["run"]["status"], "paused_low_yield")
            self.assertEqual(len(f.calls), 2)
            self.assertEqual(result["queue"]["pending"], 2)

    def test_stop_file_pauses_before_next_request(self):
        with TemporaryDirectory() as tmp:
            p = self.profile(); ledger = CompanyLedger(tmp, p)
            ledger.enqueue(BASE + "/a.pdf"); ledger.db.commit(); ledger.db.close()
            (Path(tmp) / "ledger" / "companies" / p["company_id"] / "STOP").touch()
            f = FakeFetcher({})
            result = run_company(p, tmp, fetcher=f)
            self.assertEqual(result["run"]["status"], "paused_stop_file")
            self.assertEqual([c for c in f.calls], [])


if __name__ == "__main__":
    unittest.main()
