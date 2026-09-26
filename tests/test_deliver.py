import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fetchspec.company import CompanyLedger, run_company
from fetchspec.deliver import build_delivery
import test_company
from test_company import BASE, PDF, PDF2, FakeFetcher

REQUIRED = ["provider_id", "delivery_id", "task_id_or_discovery", "collector_revision", "items"]
ITEM_REQUIRED = ["source_item_id", "source", "retrieved_at", "sha256", "content_type", "completeness", "access_scope", "version_relation"]


class DeliveryTests(unittest.TestCase):
    profile = test_company.WorkerTests.profile

    def crawl(self, tmp, pages):
        p = self.profile(); ledger = CompanyLedger(tmp, p)
        for url in pages:
            ledger.enqueue(url, priority=0)
        ledger.db.commit(); ledger.db.close()
        run_company(p, tmp, fetcher=FakeFetcher(pages))
        return p

    def test_package_matches_contract_and_is_incremental(self):
        with TemporaryDirectory() as tmp:
            a, b = BASE + "/manuals/a.pdf", BASE + "/manuals/b.pdf"
            p = self.crawl(tmp, {a: (PDF, "application/pdf"), b: (PDF2, "application/pdf")})
            first = build_delivery(p, tmp, delivery_id="d1", task_id="task-1")
            self.assertEqual(first["items"], 2)
            package = Path(tmp) / "deliveries" / "d1"
            manifest = json.loads((package / "manifest.json").read_text())
            self.assertEqual(manifest["contract_version"], "1.1")
            for key in REQUIRED:
                self.assertIn(key, manifest)
            self.assertEqual(manifest["task_id_or_discovery"], "task-1")
            for item in manifest["items"]:
                for key in ITEM_REQUIRED:
                    self.assertIn(key, item)
                body = (package / item["path"]).read_bytes()
                self.assertEqual(hashlib.sha256(body).hexdigest(), item["sha256"])
                self.assertEqual(item["version_relation"], {"type": "original"})
                self.assertIn("original_filename", item["source"])
                self.assertIn(item["source"]["language"], {"en", "zh", "en_or_unmarked"})
            sums = (package / "SHA256SUMS").read_text().splitlines()
            self.assertEqual(len(sums), 2)
            # Already delivered content is not repeated.
            second = build_delivery(p, tmp, delivery_id="d2")
            self.assertEqual((second["items"], second["already_delivered"]), (0, 2))

    def test_changed_version_names_the_superseded_sha(self):
        with TemporaryDirectory() as tmp:
            a = BASE + "/manuals/a.pdf"
            p = self.crawl(tmp, {a: (PDF, "application/pdf")})
            build_delivery(p, tmp, delivery_id="d1")
            run_company(p, tmp, fetcher=FakeFetcher({a: (PDF2, "application/pdf")}), recheck=True, force=True)
            result = build_delivery(p, tmp, delivery_id="d2")
            manifest = json.loads((Path(tmp) / "deliveries" / "d2" / "manifest.json").read_text())
            self.assertEqual(result["items"], 1)
            self.assertEqual(manifest["items"][0]["version_relation"],
                             {"type": "new_version", "supersedes_sha256": hashlib.sha256(PDF).hexdigest()})

    def test_corrupted_blob_is_reported_not_shipped(self):
        with TemporaryDirectory() as tmp:
            a = BASE + "/manuals/a.pdf"
            p = self.crawl(tmp, {a: (PDF, "application/pdf")})
            sha = hashlib.sha256(PDF).hexdigest()
            blob = next((Path(tmp) / "blobs").rglob(sha + ".pdf"))
            blob.chmod(0o644); blob.write_bytes(b"%PDF-1.4 tampered")
            result = build_delivery(p, tmp, delivery_id="d1")
            self.assertEqual((result["items"], result["sha_mismatch"]), (0, [sha]))


if __name__ == "__main__":
    unittest.main()
