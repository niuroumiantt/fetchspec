import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from fetchspec.company import CompanyLedger, run_company
from fetchspec.delivery import build_delivery
from fetchspec.inventory import load_profile

from test_company import BASE, PDF, PDF2, FakeFetcher


def crawl(tmp, pages):
    p = load_profile("supermicro"); p["min_free_bytes"] = 0
    ledger = CompanyLedger(tmp, p)
    ledger.enqueue(BASE + "/en/products/a"); ledger.db.commit(); ledger.db.close()
    run_company(p, tmp, fetcher=FakeFetcher(pages))
    return p


class DeliveryTests(unittest.TestCase):
    def test_package_matches_contract_and_checksums(self):
        with TemporaryDirectory() as tmp:
            page = BASE + "/en/products/a"
            p = crawl(tmp, {page: (b'<html><a href="/a.pdf">A</a><a href="/copy.pdf">Same</a></html>', "text/html"),
                            BASE + "/a.pdf": (PDF, "application/pdf"), BASE + "/copy.pdf": (PDF, "application/pdf")})
            result = build_delivery(p, tmp, delivery_id="d1")
            package = Path(result["package"])
            envelope = json.loads((package / "manifest.json").read_text())
            contract = ["provider_id", "delivery_id", "task_id_or_discovery", "collector_revision"]
            self.assertTrue(all(envelope[key] for key in contract))
            items = [json.loads(line) for line in (package / "items.jsonl").read_text().splitlines()]
            self.assertEqual(len(items), 1)  # identical bytes from two URLs are one item
            item = items[0]
            for key in ["source_item_id", "source", "retrieved_at", "sha256", "content_type", "completeness", "access_scope", "version_relation"]:
                self.assertIn(key, item)
            self.assertEqual({u["url"] for u in item["source"]["urls"]}, {BASE + "/a.pdf", BASE + "/copy.pdf"})
            self.assertEqual(item["version_relation"], {"type": "new"})
            check = subprocess.run(["shasum", "-a", "256", "-c", "SHA256SUMS"], cwd=package, capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            views = list((package / "library").rglob("*.pdf"))
            self.assertTrue(views and all(v.is_symlink() and v.resolve().exists() for v in views))

    def test_second_package_is_incremental_and_marks_revision(self):
        with TemporaryDirectory() as tmp:
            page, doc = BASE + "/en/products/a", BASE + "/a.pdf"
            pages = {page: (b'<html><a href="/a.pdf">A</a></html>', "text/html"), doc: (PDF, "application/pdf")}
            p = crawl(tmp, pages)
            build_delivery(p, tmp, delivery_id="d1")
            self.assertEqual(build_delivery(p, tmp, delivery_id="d2")["status"], "nothing_new")
            pages[doc] = (PDF2, "application/pdf")
            run_company(p, tmp, recheck=True, fetcher=FakeFetcher(pages))
            result = build_delivery(p, tmp, delivery_id="d3", task_id="task-1")
            self.assertEqual(result["item_count"], 1)
            package = Path(result["package"])
            item = json.loads((package / "items.jsonl").read_text())
            self.assertEqual(item["version_relation"]["type"], "revision")
            self.assertEqual(json.loads((package / "manifest.json").read_text())["task_id_or_discovery"], {"task_id": "task-1"})


if __name__ == "__main__":
    unittest.main()
