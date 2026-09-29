import copy
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from fetchspec.targets import (EXECUTION_FIELDS, IDENTITY_FIELDS, SOURCE_FILES, STATUSES,
                              load_snapshot, sync_targets, validate_documents, validate_target_ids)


def documents():
    contract = {"version": "1.5", "providers": [{"id": "fetchspec"}],
                "generated_target_contract": {"version": "2.0", "source": "framework/tco_targets.json",
                    "selection": "team == fetchspec", "identity_fields": sorted(IDENTITY_FIELDS),
                    "execution_fields": sorted(EXECUTION_FIELDS)}}
    rows = []
    for part in ("gpu", "server"):
        rows.append({"id": f"P.{part}.spec", "team": "fetchspec", "part_id": part,
                     "factor_ids": [], "variable_class": 1, "data_class": "reference", "status": "needed",
                     "disclosure_type": "Product specifications", "publisher_category": "Manufacturer",
                     "instances": ["NVIDIA" if part == "gpu" else "Supermicro"], "mechanism": "vendor_page",
                     "host": "aws", "calendar": "Product updates", "next_due": "2026-10-01"})
    rows.append({"id": "P.gpu.price", "team": "fetchquotes"})
    return contract, {"version": "2.1.0", "generated_from": {"bom": "2.2"},
                      "statuses": {name: name for name in STATUSES}, "targets": rows}


def commit(root):
    subprocess.run(["git", "-C", str(root), "add", "framework"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fetchspec Test", "-c",
                    "user.email=test@example.invalid", "commit", "--no-gpg-sign", "-m", "test inputs"],
                   check=True, capture_output=True)


def upstream(root):
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    contract, document = documents()
    (root / "framework").mkdir()
    for relative, value in zip(SOURCE_FILES, (contract, document)):
        (root / relative).write_text(json.dumps(value))
    commit(root)
    return root


class TargetSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.upstream = upstream(self.base / "upstream")
        self.state = self.base / "state"

    def test_sync_retains_full_inputs_selects_only_fetchspec_and_is_stable(self):
        snapshot = sync_targets(self.upstream, self.state)
        self.assertEqual([r["id"] for r in snapshot["targets"]], ["P.gpu.spec", "P.server.spec"])
        self.assertEqual(len(snapshot["target_document"]["targets"]), 3)
        self.assertEqual(len(snapshot["upstream"]["commit"]), 40)
        self.assertEqual(sync_targets(self.upstream, self.state), snapshot)
        self.assertEqual(load_snapshot(self.state), snapshot)
        for relative in SOURCE_FILES:
            body = (self.state / "targets/snapshots" / snapshot["snapshot_id"] / Path(relative).name).read_bytes()
            self.assertEqual(body, (self.upstream / relative).read_bytes())
        self.assertEqual(subprocess.run(["git", "-C", str(self.upstream), "status", "--porcelain"],
                                       capture_output=True, text=True).stdout, "")

    def test_unknown_wrong_team_duplicate_and_empty_target_bindings_rejected(self):
        snapshot = sync_targets(self.upstream, self.state)
        for ids in ([], ["unknown"], ["P.gpu.price"], ["P.gpu.spec", "P.gpu.spec"], [None]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                validate_target_ids(snapshot, ids)

    def test_stale_contract_and_partial_target_shapes_rejected(self):
        contract, document = documents()
        variants = []
        old = copy.deepcopy(contract); old["version"] = "1.4"; variants.append((old, document))
        old = copy.deepcopy(contract); old["generated_target_contract"]["version"] = "1.0"; variants.append((old, document))
        old = copy.deepcopy(document); old["version"] = "1.0"; variants.append((contract, old))
        old = copy.deepcopy(document); del old["targets"][0]["variable_class"]; variants.append((contract, old))
        old = copy.deepcopy(document); old["targets"].append(old["targets"][0]); variants.append((contract, old))
        for supply, targets in variants:
            with self.subTest(supply=supply["version"], version=targets["version"]), self.assertRaises(ValueError):
                validate_documents(supply, targets)

    def test_additive_1x_contracts_accepted_other_majors_rejected(self):
        contract, document = documents()
        for version in ("1.5", "1.6", "1.12"):
            with self.subTest(version=version):
                accepted = copy.deepcopy(contract); accepted["version"] = version
                self.assertEqual([row["id"] for row in validate_documents(accepted, document)],
                                 [row["id"] for row in validate_documents(contract, document)])
        for version in ("1.4", "1.05x", "2.0", "1", 1.6, None):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "unsupported supply contract"):
                rejected = copy.deepcopy(contract); rejected["version"] = version
                validate_documents(rejected, document)

    def test_dirty_upstream_inputs_are_not_an_authoritative_snapshot(self):
        path = self.upstream / SOURCE_FILES[1]
        path.write_text(path.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "uncommitted"):
            sync_targets(self.upstream, self.state)
        self.assertFalse(self.state.exists())

    def test_changed_inputs_keep_prior_snapshot_and_detect_tampering(self):
        first = sync_targets(self.upstream, self.state)
        path = self.upstream / SOURCE_FILES[1]
        value = json.loads(path.read_text()); value["targets"][0]["instances"].append("New model")
        path.write_text(json.dumps(value)); commit(self.upstream)
        second = sync_targets(self.upstream, self.state)
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(load_snapshot(self.state, first["snapshot_id"]), first)
        saved = self.state / "targets/snapshots" / second["snapshot_id"] / path.name
        saved.write_text(saved.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            load_snapshot(self.state)

    def test_checkout_behind_remote_baseline_is_rejected(self):
        subprocess.run(["git", "-C", str(self.upstream), "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
        path = self.upstream / SOURCE_FILES[1]
        path.write_text(path.read_text() + "\n"); commit(self.upstream)
        with self.assertRaisesRegex(ValueError, "origin/main"):
            sync_targets(self.upstream, self.state)


if __name__ == "__main__":
    unittest.main()
