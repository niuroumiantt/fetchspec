import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fetchspec.delivery_v2 import build_package, export_author_proposal, import_receipt
from fetchspec.targets import SOURCE_FILES, sync_targets
from test_targets import commit, upstream


def item(base, *, company="nvidia", target="P.gpu.spec", body=b"<html><table><tr><td>Power</td><td>700 W</td></tr></table></html>"):
    sha = hashlib.sha256(body).hexdigest()
    blob = base / (sha + ".html")
    blob.write_bytes(body)
    return {"blob_path": str(blob), "sha256": sha, "format": "html", "source_item_id": company + ":model-a",
            "source": {"url": "https://www." + company + ".com/model-a/", "language": "en", "categories": ["Hardware"]},
            "retrieved_at": "2026-09-29T00:00:00Z", "target_ids": [target], "product_ids": [company + ":model-a"]}


def receipt(summary):
    manifest = json.loads((Path(summary["package"]) / "manifest.json").read_text())
    return {"delivery_id": manifest["delivery_id"], "manifest_sha256": summary["manifest_sha256"],
            "company_id": manifest["company_id"], "task_id_or_discovery": "discovery", "status": "received",
            "research_context": {"target_ids": manifest["target_ids"], "part_ids": manifest["part_ids"]},
            "received_items": len(manifest["items"]), "reader_handoff_count": 0,
            "items": [{"sha256": i["sha256"], "format": i["format"], "target_ids": i["target_ids"],
                       "status": "received", "reader_handoff": "held"} for i in manifest["items"]]}


class DeliveryV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.upstream = upstream(self.base / "upstream")
        self.state = self.base / "state"
        self.snapshot = sync_targets(self.upstream, self.state)
        self.item = item(self.base)

    def build(self, items=None, **kwargs):
        return build_package(self.state, self.snapshot, "nvidia", items or [self.item],
                             collector_revision="test-revision", **kwargs)

    def test_package_is_stable_per_content_binding_and_preserves_input_bytes(self):
        first = self.build()
        manifest_path = Path(first["package"]) / "manifest.json"
        original_manifest = manifest_path.read_bytes()
        second = self.build()
        self.assertEqual(first["delivery_id"], second["delivery_id"])
        self.assertTrue(second["replayed"])
        self.assertEqual(original_manifest, manifest_path.read_bytes())
        manifest = json.loads(original_manifest)
        self.assertEqual(manifest["contract_version"], "2.0")
        self.assertEqual(manifest["target_ids"], ["P.gpu.spec"])
        self.assertEqual(manifest["part_ids"], ["gpu"])
        self.assertEqual(manifest["upstream"]["commit"], self.snapshot["upstream"]["commit"])
        self.assertEqual((Path(first["package"]) / manifest["items"][0]["path"]).read_bytes(), Path(self.item["blob_path"]).read_bytes())

    def test_same_bytes_at_different_urls_keep_observations_and_one_file(self):
        other = copy.deepcopy(self.item)
        other["source_item_id"] = "nvidia:renamed-model"
        other["source"]["url"] += "renamed"
        other["target_ids"] = ["P.server.spec"]
        other["product_ids"] = ["nvidia:model-b"]
        first = self.build([self.item, other])
        second = self.build([other, self.item])
        manifest = json.loads((Path(first["package"]) / "manifest.json").read_text())
        self.assertEqual(len(manifest["items"]), 1)
        self.assertEqual(len(manifest["items"][0]["source_observations"]), 2)
        self.assertEqual(manifest["items"][0]["target_ids"], ["P.gpu.spec", "P.server.spec"])
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])

    def test_structured_evidence_keeps_original_tables_and_per_product_target_bindings(self):
        table = {"index": 1, "section": "Specification", "notes": "Maximum; configuration A", "rows": [
            [{"text": "Power", "header": True, "rowspan": 2}, {"text": "700 W", "colspan": 2}]],
            "source_refs": [{"url": self.item["source"]["url"], "sha256": self.item["sha256"]}]}
        first = copy.deepcopy(self.item)
        first["product_evidence"] = [{"product_id": first["product_ids"][0], "name": "Model A", "specification_tables": [table], "part_ids": ["untrusted-supplier-part"]}]
        second = copy.deepcopy(first)
        second["product_ids"] = ["nvidia:model-b"]
        second["product_evidence"][0]["product_id"] = "nvidia:model-b"
        second["target_ids"] = ["P.server.spec"]
        summary = self.build([first, second])
        manifest = json.loads((Path(summary["package"]) / "manifest.json").read_text())
        evidence = manifest["items"][0]["product_evidence"]
        self.assertEqual(len(evidence), 2)
        by_product = {entry["product_id"]: entry for entry in evidence}
        self.assertEqual(by_product["nvidia:model-a"]["target_ids"], ["P.gpu.spec"])
        self.assertEqual(by_product["nvidia:model-a"]["part_ids"], ["gpu"])
        self.assertEqual(by_product["nvidia:model-a"]["specification_tables"], [table])
        self.assertEqual(by_product["nvidia:model-b"]["target_ids"], ["P.server.spec"])
        import_receipt(self.state, receipt(summary))
        proposal = export_author_proposal(self.state, summary["delivery_id"])
        records = {record["target_id"]: record for record in proposal["records"]}
        self.assertEqual(records["P.gpu.spec"]["product_ids"], ["nvidia:model-a"])
        self.assertEqual(records["P.server.spec"]["product_ids"], ["nvidia:model-b"])

    def test_missing_supplemental_table_evidence_cannot_be_delivered(self):
        self.item["product_evidence"] = [{"product_id": self.item["product_ids"][0], "specification_tables": [
            {"rows": [], "source_refs": [{"url": "https://www.nvidia.com/missing.pdf", "sha256": "b" * 64}]}]}]
        with self.assertRaisesRegex(ValueError, "source reference"):
            self.build()

    def test_changed_same_url_preserves_prior_package_and_supersedes(self):
        first = self.build()
        changed = item(self.base, body=b"<html><p>New specification revision</p></html>")
        changed["version_relation"] = {"type": "new_version", "supersedes_sha256": self.item["sha256"]}
        second = self.build([changed])
        self.assertNotEqual(first["delivery_id"], second["delivery_id"])
        self.assertTrue((Path(first["package"]) / "manifest.json").exists())
        manifest = json.loads((Path(second["package"]) / "manifest.json").read_text())
        self.assertEqual(manifest["items"][0]["version_relation"]["supersedes_sha256"], self.item["sha256"])

    def test_corrupt_blob_wrong_language_empty_or_unowned_target_never_packages(self):
        invalid = []
        bad = copy.deepcopy(self.item); bad["sha256"] = "f" * 64; invalid.append([bad])
        bad = copy.deepcopy(self.item); bad["source"]["language"] = "ja"; invalid.append([bad])
        bad = copy.deepcopy(self.item); bad["target_ids"] = ["P.gpu.price"]; invalid.append([bad])
        invalid.append([])
        for candidates in invalid:
            with self.subTest(items=candidates), self.assertRaises(ValueError):
                build_package(self.state, self.snapshot, "nvidia", candidates, collector_revision="test")
        self.assertFalse((self.state / "deliveries").exists())

    def test_direct_api_rejects_unofficial_other_company_and_language_spoofing(self):
        urls = ["https://evil.example/spec.pdf", "https://www.supermicro.com/en/products/model-a/",
                "https://www.nvidia.com/ja-jp/products/model-a/", "https://www.nvidia.com/products/model-a/?locale=ja",
                "https://www.nvidia.com/products/model-a/?language=japanese", "https://www.nvidia.com/products/model-a/?lang=zh_CN",
                "https://www.nvidia.cn/products/model-a/", "https://user:pass@www.nvidia.com/model-a/",
                "https://www.nvidia.com:8443/model-a/"]
        for url in urls:
            candidate = copy.deepcopy(self.item); candidate["source"]["url"] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.build([candidate])
        with self.assertRaisesRegex(ValueError, "unsupported product adapter"):
            build_package(self.state, self.snapshot, "unregistered", [self.item], collector_revision="test")
        self.assertFalse((self.state / "deliveries").exists())

    def test_final_url_and_aliases_have_the_same_official_language_scope(self):
        for field in ("final_url", "also_seen_at"):
            for url in ("https://evil.example/spec", "https://www.supermicro.com/spec", "https://www.nvidia.com/spec?hl=ja",
                        "https://www.nvidia.cn/spec"):
                candidate = copy.deepcopy(self.item)
                candidate["source"][field] = [url] if field == "also_seen_at" else url
                with self.subTest(field=field, url=url), self.assertRaises(ValueError):
                    self.build([candidate])

    def test_reviewed_official_cdn_and_adapter_hosts_remain_accepted(self):
        for host in ("dam-cdn.nvd.orangelogic.com", "developer.nvidia.com", "resources.nvidia.com", "networking-docs.nvidia.com"):
            candidate = copy.deepcopy(self.item); candidate["source"]["url"] = "https://" + host + "/model-a/"
            with self.subTest(host=host):
                self.assertEqual(self.build([candidate])["items"], 1)

    def test_same_bytes_chinese_and_english_sources_keep_distinct_language_observations(self):
        english = copy.deepcopy(self.item)
        english["source"]["url"] = "https://www.nvidia.com/en-us/products/model-a/"
        english["source"]["also_seen_at"] = ["https://images.nvidia.com/model-a-en.html"]
        chinese = copy.deepcopy(self.item)
        chinese["source_item_id"] = "nvidia:model-a-zh"
        chinese["source"].update(url="https://www.nvidia.cn/products/model-a/", language="zh")
        summary = self.build([english, chinese])
        manifest = json.loads((Path(summary["package"]) / "manifest.json").read_text())
        self.assertEqual(len(manifest["items"]), 1)
        merged = manifest["items"][0]
        by_language = {obs["source"]["language"]: obs["source"] for obs in merged["source_observations"]}
        self.assertEqual(set(by_language), {"en", "zh"})
        self.assertEqual(by_language["zh"]["url"], chinese["source"]["url"])
        for alias in merged["source"]["also_seen_at"]:
            self.assertEqual(".cn/" in alias, merged["source"]["language"] == "zh")
        self.assertEqual(self.build([chinese, english])["manifest_sha256"], summary["manifest_sha256"])

    def test_id_collision_does_not_replace_package(self):
        first = self.build(delivery_id="explicit")
        changed = copy.deepcopy(self.item); changed["target_ids"] = ["P.server.spec"]
        with self.assertRaisesRegex(ValueError, "reused"):
            self.build([changed], delivery_id="explicit")
        self.assertEqual(self.build(delivery_id="explicit")["manifest_sha256"], first["manifest_sha256"])

    def test_receipt_exact_binding_is_idempotent_and_proposal_is_candidate_only(self):
        summary = self.build()
        acknowledgement = receipt(summary)
        result = import_receipt(self.state, acknowledgement)
        self.assertEqual(result["git_target_status"], "unchanged")
        self.assertTrue(import_receipt(self.state, acknowledgement)["replayed"])
        proposal = export_author_proposal(self.state, summary["delivery_id"])
        self.assertEqual(proposal["receipt_sha256"], result["receipt_sha256"])
        self.assertEqual(proposal["records"][0]["target_id"], "P.gpu.spec")
        self.assertEqual(proposal["records"][0]["part_id"], "gpu")
        self.assertEqual(proposal["status"], "proposed")
        self.assertFalse((self.upstream / "data").exists())

    def test_manifest_sha_and_exact_batch_item_sha_target_part_sets_are_required(self):
        summary = self.build()
        valid = receipt(summary)
        changes = [lambda r: r.update(manifest_sha256="f" * 64),
                   lambda r: r.update(delivery_id="unknown"),
                   lambda r: r["research_context"].update(target_ids=["P.server.spec"]),
                   lambda r: r["research_context"].update(part_ids=["server"]),
                   lambda r: r["items"][0].update(sha256="f" * 64),
                   lambda r: r["items"][0].update(target_ids=["P.server.spec"]),
                   lambda r: r["items"].append(r["items"][0]),
                   lambda r: r.update(received_items=0)]
        for change in changes:
            bad = copy.deepcopy(valid); change(bad)
            with self.subTest(receipt=bad), self.assertRaises(ValueError):
                import_receipt(self.state, bad)
        with sqlite3.connect(self.state / "delivery-ledger.sqlite") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, "validated receipt"):
            export_author_proposal(self.state, summary["delivery_id"])

    def test_conflicting_receipt_does_not_overwrite_first_acknowledgement(self):
        summary = self.build(); original = receipt(summary)
        first = import_receipt(self.state, original)
        changed = copy.deepcopy(original); changed["receiver_annotation"] = "different"
        with self.assertRaisesRegex(ValueError, "conflicting"):
            import_receipt(self.state, changed)
        self.assertEqual(import_receipt(self.state, original)["receipt_sha256"], first["receipt_sha256"])

    def test_current_registry_part_change_blocks_old_acknowledgement(self):
        summary = self.build()
        path = self.upstream / SOURCE_FILES[1]
        value = json.loads(path.read_text()); value["targets"][0]["part_id"] = "new-part"
        path.write_text(json.dumps(value)); commit(self.upstream)
        sync_targets(self.upstream, self.state)
        with self.assertRaisesRegex(ValueError, "mapping changed"):
            import_receipt(self.state, receipt(summary))

    def test_tampered_package_cannot_be_acknowledged(self):
        summary = self.build()
        acknowledgement = receipt(summary)
        manifest = json.loads((Path(summary["package"]) / "manifest.json").read_text())
        (Path(summary["package"]) / manifest["items"][0]["path"]).write_bytes(b"tamper")
        with self.assertRaisesRegex(ValueError, "changed"):
            import_receipt(self.state, acknowledgement)

    @unittest.skipUnless(os.environ.get("FETCHSPEC_INRESEARCH_ROOT"), "set FETCHSPEC_INRESEARCH_ROOT for receiver integration")
    def test_real_receiver_nvidia_and_supermicro_with_current_upstream_targets(self):
        authority = Path(os.environ["FETCHSPEC_INRESEARCH_ROOT"])
        snapshot = sync_targets(authority, self.state)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INRESEARCH_RUNTIME_ROOT", None)
            sys.path.insert(0, str(authority / "src"))
            try:
                from inresearch.materials.fetchspec_receive import receive
                receiver_root = self.base / "receiver-checkout"
                (receiver_root / "framework").mkdir(parents=True)
                (receiver_root / "framework/tco_targets.json").write_text(json.dumps(snapshot["target_document"]))
                for company, target in (("nvidia", "P.gpu.spec"), ("supermicro", "P.server.spec")):
                    candidate = item(self.base, company=company, target=target, body=("<html>" + company + "</html>").encode())
                    package = build_package(self.state, snapshot, company, [candidate], collector_revision="integration-test")
                    acknowledgement = receive(receiver_root, Path(package["package"]), self.base / "receiver-data")
                    imported = import_receipt(self.state, acknowledgement)
                    self.assertEqual(imported["status"], "receipt_validated")
                    self.assertEqual(acknowledgement, receive(receiver_root, Path(package["package"]), self.base / "receiver-data"))
            finally:
                sys.path.remove(str(authority / "src"))


if __name__ == "__main__":
    unittest.main()
