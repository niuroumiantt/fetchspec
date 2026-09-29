#!/usr/bin/env python3
"""Validate real v2 packages with an isolated local inresearch receiver.

This writes only the selected Fetchspec receipt ledger/report. Receiver originals,
indexes and runtime receipts live in a system TemporaryDirectory. It never sends
files to production or asserts that production has received a package.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fetchspec.delivery_v2 import import_receipt
from fetchspec.inventory import atomic_json
from fetchspec.targets import SOURCE_FILES, load_snapshot

ENVIRONMENT = "local_receiver_validation"


def verify(upstream, root, packages):
    upstream, root = Path(upstream).expanduser().resolve(), Path(root).expanduser().resolve()
    if root == upstream or root.is_relative_to(upstream):
        raise ValueError("Fetchspec state must be outside the inresearch checkout")
    snapshot = load_snapshot(root)
    commit = subprocess.run(["git", "-C", str(upstream), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(upstream), "status", "--porcelain", "--", "src", "manage.py", "framework"],
                           capture_output=True, text=True, check=True).stdout.strip()
    if dirty:
        raise ValueError("receiver implementation/authority has uncommitted changes")
    if commit != snapshot["upstream"]["commit"]:
        raise ValueError("receiver checkout commit differs from current target snapshot; sync targets first")
    for relative in SOURCE_FILES:
        body = (upstream / relative).read_bytes()
        if hashlib.sha256(body).hexdigest() != snapshot["upstream"]["files"][relative]["sha256"]:
            raise ValueError("current receiver authority differs from the target snapshot")
    report = {"schema_version": 1, "environment": ENVIRONMENT, "production_received": False,
              "research_adoption": "not_inferred", "git_target_status": "unchanged",
              "receiver_commit": commit, "target_snapshot_id": snapshot["snapshot_id"], "packages": []}
    env = dict(os.environ)
    env.pop("INRESEARCH_RUNTIME_ROOT", None)
    env["PYTHONPATH"] = str(upstream / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory(prefix="fetchspec-receiver-validation-") as temporary:
        temporary = Path(temporary)
        receiver = temporary / "checkout"
        data = temporary / "receiver-data"
        (receiver / "framework").mkdir(parents=True)
        (receiver / "framework/tco_targets.json").write_bytes((upstream / SOURCE_FILES[1]).read_bytes())
        for index, package_path in enumerate(packages):
            package = Path(package_path).expanduser().resolve()
            entry = {"package": str(package), "environment": ENVIRONMENT, "production_received": False}
            try:
                manifest = json.loads((package / "manifest.json").read_text())
                entry.update(delivery_id=manifest["delivery_id"], company_id=manifest["company_id"])
                command = [sys.executable, "-m", "inresearch.materials.fetchspec_receive", str(package),
                           "--repo-root", str(receiver), "--data-root", str(data)]
                first = subprocess.run(command, cwd=temporary, env=env, capture_output=True, timeout=180)
                if first.returncode:
                    raise ValueError("local receiver rejected package: " + first.stdout.decode(errors="replace")[:500])
                acknowledgement = json.loads(first.stdout)
                replay = subprocess.run(command, cwd=temporary, env=env, capture_output=True, timeout=180)
                if replay.returncode or json.loads(replay.stdout) != acknowledgement:
                    raise ValueError("local receiver replay was not idempotent")
                receipt_path = temporary / ("receipt-" + str(index) + ".json")
                receipt_path.write_bytes(first.stdout)
                imported = import_receipt(root, receipt_path, environment=ENVIRONMENT,
                    context={"receiver_commit": commit, "receiver_execution": "system_temporary_directory",
                             "authority_source": str(upstream), "protocol": "receive_then_identical_replay"})
                tables = set()
                products = set()
                for item in manifest["items"]:
                    for evidence in item.get("product_evidence", []):
                        products.add(evidence["product_id"])
                        for table in evidence.get("specification_tables", []):
                            tables.add((evidence["product_id"], json.dumps(table, ensure_ascii=False, sort_keys=True)))
                entry.update(status="validated", receiver_status=acknowledgement["status"],
                             idempotent=True, received_items=acknowledgement["received_items"],
                             target_ids=imported["target_ids"], part_ids=imported["part_ids"],
                             manifest_sha256=imported["manifest_sha256"], receipt_sha256=imported["receipt_sha256"],
                             structured_products=len(products), specification_tables=len(tables),
                             receipt_import_replayed=imported["replayed"], receipt=acknowledgement)
            except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
                entry.update(status="failed", error=str(exc))
            report["packages"].append(entry)
        report["temporary_originals"] = len(list((data / "originals").glob("*/*/*")))
    report["validated"] = sum(row["status"] == "validated" for row in report["packages"])
    report["failed"] = len(report["packages"]) - report["validated"]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--package", action="append", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.report.expanduser().resolve().is_relative_to(args.upstream.expanduser().resolve()):
            raise ValueError("report must be outside the inresearch checkout")
        report = verify(args.upstream, args.root, args.package)
        atomic_json(args.report, report)
        print(json.dumps({key: report[key] for key in ("environment", "production_received", "validated", "failed")}, ensure_ascii=False))
        return 1 if report["failed"] else 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"environment": ENVIRONMENT, "production_received": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
