"""Store -> explicit target binding -> package -> isolated receiver -> scoped receipt."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from fetchspec.delivery_v2 import build_package, export_author_proposal, import_receipt
from fetchspec.pipeline import main
from fetchspec.products import ProductStore
from fetchspec.targets import sync_targets
from test_delivery_v2 import receipt
from test_targets import upstream


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.upstream = upstream(self.base / "upstream")
        self.root = self.base / "state"
        self.archive = self.base / "archive"
        self.archive.mkdir()
        self.snapshot = sync_targets(self.upstream, self.root)
        self.store = ProductStore(self.root)
        self.addCleanup(self.store.close)

    def catalog(self, *, company="nvidia", value="700 W", at="2026-09-01T00:00:00Z"):
        primary_url = "https://www." + company + ".com/model-a/"
        pdf_url = "https://www." + company + ".com/model-a-datasheet.pdf"
        sources = []
        for url, body, fmt in ((primary_url, ("<html><h1>Model A</h1><p>" + value + "</p></html>").encode(), "html"),
                               (pdf_url, ("%PDF-1.4 test fixture " + value).encode(), "pdf")):
            sha = hashlib.sha256(body).hexdigest()
            path = self.archive / (sha + "." + fmt)
            path.write_bytes(body)
            sources.append({"source_url": url, "sha256": sha, "snapshot_path": path.name,
                            "observed_at": at, "format": fmt, "language": "en"})
        table = {"index": 4, "section": "Electrical characteristics", "notes": "Maximum; configuration A at 25°C",
                 "rows": [[{"text": "Parameter", "header": True, "rowspan": 2}, {"text": "Original value", "header": True}],
                          [{"text": "Power"}, {"text": value, "colspan": 2}]],
                 "source_refs": [{"url": pdf_url, "sha256": sources[1]["sha256"]}]}
        product = {"id": company + ":model-a", "name": "Model A", "kind": "named_product", "parent_id": None,
                   "source_url": primary_url, "source_sha256": sources[0]["sha256"], "product_url": primary_url,
                   "observed_at": at, "tables": [table]}
        return {"company_id": company, "sources": sources, "products": [product]}

    def ingest_bind(self, payload, target="P.gpu.spec"):
        self.store.ingest_catalog(payload, self.archive)
        self.store.bind(self.snapshot, payload["company_id"], [payload["products"][0]["id"]], [target], "reviewed fixture scope")

    def package(self, company="nvidia"):
        return build_package(self.root, self.snapshot, company,
            self.store.delivery_items(self.snapshot, company, [company + ":model-a"]), collector_revision="pipeline-test")

    def test_cli_binding_package_requires_explicit_demand_and_retains_original_tables(self):
        payload = self.catalog(); self.store.ingest_catalog(payload, self.archive)
        with self.assertRaises(ValueError):
            self.package()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = main(["--root", str(self.root), "bind", "--company", "nvidia", "--product", "nvidia:model-a",
                           "--target", "P.gpu.spec", "--reason", "reviewed fixture scope"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["targets"], ["P.gpu.spec"])
        summary = self.package()
        manifest = json.loads((Path(summary["package"]) / "manifest.json").read_text())
        self.assertEqual(summary["items"], 2)
        for item in manifest["items"]:
            self.assertEqual(item["product_evidence"][0]["specification_tables"], payload["products"][0]["tables"])

    def test_bound_product_update_keeps_binding_and_delivers_new_version(self):
        first_payload = self.catalog(); self.ingest_bind(first_payload)
        first = self.package()
        updated = self.catalog(value="800 W", at="2026-09-02T00:00:00Z")
        self.store.ingest_catalog(updated, self.archive)
        second = self.package()
        self.assertNotEqual(first["delivery_id"], second["delivery_id"])
        self.assertEqual(self.store.stats()["bindings"], 1)
        self.assertEqual(self.store.stats()["product_versions"], 2)
        manifest = json.loads((Path(second["package"]) / "manifest.json").read_text())
        old_shas = {source["sha256"] for source in first_payload["sources"]}
        for item in manifest["items"]:
            self.assertEqual(item["target_ids"], ["P.gpu.spec"])
            self.assertIn(item["version_relation"]["supersedes_sha256"], old_shas)
            self.assertEqual(item["product_evidence"][0]["specification_tables"], updated["products"][0]["tables"])
        self.assertTrue((Path(first["package"]) / "manifest.json").exists())

    def test_same_bytes_later_observation_does_not_create_another_package(self):
        payload = self.catalog(); self.ingest_bind(payload)
        first = self.package()
        updated = self.catalog(at="2026-09-02T00:00:00Z")
        self.store.ingest_catalog(updated, self.archive)
        second = self.package()
        self.assertEqual(first["delivery_id"], second["delivery_id"])
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
        self.assertEqual(self.store.stats()["sources"], 4)
        self.assertEqual(self.store.stats()["product_versions"], 1)

    def test_missing_blob_and_bad_receipt_fail_without_success_then_retry(self):
        payload = self.catalog(); self.ingest_bind(payload)
        selected = self.store.delivery_items(self.snapshot, "nvidia", ["nvidia:model-a"])
        missing = Path(selected[1]["blob_path"]); saved = missing.read_bytes(); missing.unlink()
        with self.assertRaises(FileNotFoundError):
            self.package()
        self.assertFalse((self.root / "deliveries").exists())
        missing.write_bytes(saved)
        package = self.package(); acknowledgement = receipt(package)
        bad = copy.deepcopy(acknowledgement); bad["items"][0]["target_ids"] = ["P.server.spec"]
        with self.assertRaises(ValueError):
            import_receipt(self.root, bad, environment="local_receiver_validation")
        accepted = import_receipt(self.root, acknowledgement, environment="local_receiver_validation")
        self.assertEqual(accepted["status"], "receipt_validated")
        self.assertTrue(import_receipt(self.root, acknowledgement, environment="local_receiver_validation")["replayed"])

    def test_local_and_default_receipts_are_independent_and_raw_hash_is_preserved(self):
        self.ingest_bind(self.catalog()); package = self.package(); acknowledgement = receipt(package)
        path = self.base / "receipt.json"
        raw = (json.dumps(acknowledgement, indent=4) + "\n").encode(); path.write_bytes(raw)
        result = import_receipt(self.root, path, environment="local_receiver_validation", context={"receiver_commit": "test-only"})
        self.assertEqual(result["receipt_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertFalse(result["production_received"])
        with self.assertRaisesRegex(ValueError, "validated receipt"):
            export_author_proposal(self.root, package["delivery_id"])
        local = export_author_proposal(self.root, package["delivery_id"], environment="local_receiver_validation")
        self.assertEqual(local["status"], "validation_only")
        self.assertFalse(local["production_received"])
        self.assertEqual(local["receiver_context"]["receiver_commit"], "test-only")
        # Same JSON with different formatting retains the first exact raw receipt.
        replay = import_receipt(self.root, acknowledgement, environment="local_receiver_validation")
        self.assertEqual(replay["receipt_sha256"], result["receipt_sha256"])
        remote = copy.deepcopy(acknowledgement); remote["known_gaps"] = ["receiver-side note"]
        default = import_receipt(self.root, remote)
        self.assertFalse(default["replayed"])
        with sqlite3.connect(self.root / "delivery-ledger.sqlite") as db:
            rows = db.execute("SELECT environment,receipt_json FROM receipts ORDER BY environment").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1].encode(), raw)

    def test_changed_stored_raw_receipt_is_detected_before_proposal_or_replay(self):
        self.ingest_bind(self.catalog()); package = self.package(); acknowledgement = receipt(package)
        import_receipt(self.root, acknowledgement)
        with sqlite3.connect(self.root / "delivery-ledger.sqlite") as db:
            db.execute("UPDATE receipts SET receipt_json=receipt_json || ' '")
        with self.assertRaisesRegex(ValueError, "raw receipt SHA mismatch"):
            export_author_proposal(self.root, package["delivery_id"])
        with self.assertRaisesRegex(ValueError, "raw receipt SHA mismatch"):
            import_receipt(self.root, acknowledgement)

    def test_legacy_receipts_migrate_without_claiming_production(self):
        self.ingest_bind(self.catalog()); package = self.package(); acknowledgement = receipt(package)
        raw = json.dumps(acknowledgement, sort_keys=True)
        with sqlite3.connect(self.root / "delivery-ledger.sqlite") as db:
            db.execute("DROP TABLE receipts")
            db.execute("CREATE TABLE receipts(delivery_id TEXT PRIMARY KEY, receipt_sha256 TEXT, receipt_json TEXT, imported_at TEXT)")
            db.execute("INSERT INTO receipts VALUES(?,?,?,?)", (package["delivery_id"], hashlib.sha256(raw.encode()).hexdigest(), raw, "test"))
        result = import_receipt(self.root, acknowledgement)
        self.assertTrue(result["replayed"])
        self.assertEqual(result["environment"], "receiver")
        self.assertEqual(result["receipt_scope"], "unspecified")

    @unittest.skipUnless(os.environ.get("FETCHSPEC_INRESEARCH_ROOT"), "set FETCHSPEC_INRESEARCH_ROOT for real receiver validation")
    def test_real_receiver_script_completes_scoped_pipeline_for_both_companies(self):
        authority = Path(os.environ["FETCHSPEC_INRESEARCH_ROOT"])
        self.snapshot = sync_targets(authority, self.root)
        packages = []
        for company, target in (("nvidia", "P.gpu.spec"), ("supermicro", "P.server.spec")):
            self.ingest_bind(self.catalog(company=company), target)
            packages.append(self.package(company))
        script = Path(__file__).resolve().parents[1] / "scripts/verify_pipeline_receiver.py"
        report_path = self.base / "report.json"
        command = [sys.executable, str(script), "--upstream", str(authority), "--root", str(self.root), "--report", str(report_path)]
        for package in packages:
            command += ["--package", package["package"]]
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        first = subprocess.run(command, capture_output=True, text=True, env=env)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["validated"], 2)
        self.assertFalse(report["production_received"])
        self.assertTrue(all(row["idempotent"] for row in report["packages"]))
        self.assertEqual({tuple(row["part_ids"]) for row in report["packages"]}, {("gpu",), ("server",)})
        for package in packages:
            with self.assertRaises(ValueError):
                export_author_proposal(self.root, package["delivery_id"])
            proposal = export_author_proposal(self.root, package["delivery_id"], environment="local_receiver_validation")
            self.assertEqual(proposal["records"][0]["product_evidence"][0]["specification_tables"][0]["notes"], "Maximum; configuration A at 25°C")
        again = subprocess.run(command, capture_output=True, text=True, env=env)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertTrue(all(row["receipt_import_replayed"] for row in json.loads(report_path.read_text())["packages"]))


if __name__ == "__main__":
    unittest.main()
