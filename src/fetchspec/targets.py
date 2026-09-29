"""Read-only inresearch demand sync with immutable, verifiable local snapshots.

Targets describe collection demand, never research facts. The complete upstream
contract and target document are retained; only team=fetchspec rows are selected.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .inventory import atomic_json, utc_now

SOURCE_FILES = ("framework/supply_contract.json", "framework/tco_targets.json")
SHA = re.compile(r"^[a-f0-9]{64}$")
IDENTITY_FIELDS = {"id", "part_id", "factor_ids", "variable_class", "data_class"}
EXECUTION_FIELDS = {"disclosure_type", "publisher_category", "instances", "mechanism", "host", "calendar", "next_due"}
STATUSES = {"sourced", "assumed", "delivered", "needed"}


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def _strings(value, name, *, empty=True):
    if (not isinstance(value, list) or (not empty and not value)
            or any(not isinstance(item, str) or not item or len(item) > 500 for item in value)
            or len(value) != len(set(value))):
        raise ValueError("invalid " + name)
    return value


def validate_documents(contract, document):
    """Reject old/partial shapes before they can become executable demand."""
    version = contract.get("version") if isinstance(contract, dict) else None
    match = re.fullmatch(r"1\.(\d+)", version) if isinstance(version, str) else None
    if not match or int(match.group(1)) < 5:
        raise ValueError(f"unsupported supply contract {version!r}; expected 1.5 or later 1.x")
    generated = contract.get("generated_target_contract")
    if (not isinstance(generated, dict) or generated.get("version") != "2.0"
            or generated.get("source") != "framework/tco_targets.json"
            or generated.get("selection") != "team == fetchspec"):
        raise ValueError("unsupported generated target contract")
    if not IDENTITY_FIELDS <= set(_strings(generated.get("identity_fields"), "identity_fields", empty=False)):
        raise ValueError("incomplete generated target identity contract")
    if not EXECUTION_FIELDS <= set(_strings(generated.get("execution_fields"), "execution_fields", empty=False)):
        raise ValueError("incomplete generated target execution contract")
    providers = contract.get("providers")
    if not isinstance(providers, list) or not any(isinstance(p, dict) and p.get("id") == "fetchspec" for p in providers):
        raise ValueError("Fetchspec provider missing from supply contract")
    if (not isinstance(document, dict) or not isinstance(document.get("version"), str)
            or not re.fullmatch(r"2\.\d+(?:\.\d+)?", document["version"])
            or not isinstance(document.get("generated_from"), dict)
            or not isinstance(document.get("statuses"), dict)
            or set(document["statuses"]) != STATUSES
            or not isinstance(document.get("targets"), list) or not document["targets"]):
        raise ValueError("unsupported or incomplete target document; expected current 2.x shape")
    seen, selected = set(), []
    for row in document["targets"]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not row["id"] or len(row["id"]) > 200 or row["id"] in seen
                or not isinstance(row.get("team"), str) or not row["team"]):
            raise ValueError("invalid or duplicate target identity")
        seen.add(row["id"])
        if row["team"] != "fetchspec":
            continue
        if not IDENTITY_FIELDS | EXECUTION_FIELDS <= row.keys():
            raise ValueError("target shape is missing current identity/execution fields")
        if (row["part_id"] is not None and (not isinstance(row["part_id"], str) or not row["part_id"])):
            raise ValueError("invalid target part_id")
        if (type(row["variable_class"]) is not int or row["variable_class"] not in range(1, 6)
                or row["data_class"] not in {"reference", "observation", "material"}
                or row.get("status") not in STATUSES):
            raise ValueError("invalid target variable class/data class/status")
        _strings(row["factor_ids"], "factor_ids")
        _strings(row["instances"], "instances")
        for key in EXECUTION_FIELDS - {"instances", "next_due"}:
            if not isinstance(row[key], str) or not row[key]:
                raise ValueError("invalid target " + key)
        if row["next_due"] is not None and not isinstance(row["next_due"], str):
            raise ValueError("invalid target next_due")
        selected.append(row)
    if not selected:
        raise ValueError("no Fetchspec targets in upstream snapshot")
    return selected


def _git(root, *args, required=True):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if result.returncode:
        if not required:
            return None
        raise ValueError("upstream must be a readable Git checkout")
    return result.stdout.strip()


def _identity(upstream):
    return digest(canonical_bytes({"commit": upstream["commit"], "files": upstream["files"]}))


def _hydrate(metadata, bodies):
    if (metadata.get("schema_version") != 1 or not SHA.fullmatch(str(metadata.get("snapshot_id", "")))
            or not isinstance(metadata.get("upstream"), dict)):
        raise ValueError("invalid target snapshot metadata")
    upstream = metadata["upstream"]
    if (not re.fullmatch(r"[a-f0-9]{40,64}", str(upstream.get("commit", "")))
            or upstream.get("dirty") is not False or set(upstream.get("files", {})) != set(SOURCE_FILES)
            or metadata["snapshot_id"] != _identity(upstream)):
        raise ValueError("target snapshot identity mismatch")
    for relative, body in bodies.items():
        expected = upstream["files"][relative]
        if expected != {"sha256": digest(body), "bytes": len(body)}:
            raise ValueError("upstream snapshot digest mismatch: " + relative)
    contract = json.loads(bodies[SOURCE_FILES[0]])
    document = json.loads(bodies[SOURCE_FILES[1]])
    rows = validate_documents(contract, document)
    return {**metadata, "supply_contract": contract, "target_document": document, "targets": rows}


def validate_snapshot(snapshot):
    """Validate an in-memory snapshot's demand shape (disk loads also check raw hashes)."""
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ValueError("invalid target snapshot")
    if not SHA.fullmatch(str(snapshot.get("snapshot_id", ""))):
        raise ValueError("invalid target snapshot identity")
    rows = validate_documents(snapshot.get("supply_contract"), snapshot.get("target_document"))
    if snapshot.get("targets") != rows:
        raise ValueError("target selection differs from complete upstream document")
    return snapshot


def load_snapshot(state_root, snapshot_id=None):
    base = Path(state_root).expanduser() / "targets"
    if snapshot_id is None:
        pointer = json.loads((base / "current.json").read_text())
        if pointer.get("schema_version") != 1:
            raise ValueError("invalid current target snapshot pointer")
        snapshot_id = pointer.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not SHA.fullmatch(snapshot_id):
        raise ValueError("invalid target snapshot identity")
    directory = base / "snapshots" / snapshot_id
    if directory.is_symlink():
        raise ValueError("unsafe target snapshot directory")
    metadata_path = directory / "metadata.json"
    if metadata_path.is_symlink():
        raise ValueError("unsafe target snapshot metadata")
    metadata = json.loads(metadata_path.read_text())
    if metadata.get("snapshot_id") != snapshot_id:
        raise ValueError("target snapshot path identity mismatch")
    bodies = {}
    for relative in SOURCE_FILES:
        path = directory / Path(relative).name
        if path.is_symlink():
            raise ValueError("unsafe upstream snapshot file")
        bodies[relative] = path.read_bytes()
    return _hydrate(metadata, bodies)


def sync_targets(upstream_root, state_root):
    """Capture clean HEAD inputs without fetching, switching or writing upstream."""
    root = Path(upstream_root).expanduser().resolve(strict=True)
    commit = _git(root, "rev-parse", "HEAD")
    remote = _git(root, "rev-parse", "--verify", "origin/main", required=False)
    if remote and remote != commit:
        raise ValueError("upstream checkout is not current origin/main; synchronize it separately")
    if _git(root, "status", "--porcelain", "--", *SOURCE_FILES):
        raise ValueError("upstream target/contract inputs have uncommitted changes")
    bodies = {relative: (root / relative).read_bytes() for relative in SOURCE_FILES}
    validate_documents(json.loads(bodies[SOURCE_FILES[0]]), json.loads(bodies[SOURCE_FILES[1]]))
    if (_git(root, "rev-parse", "HEAD") != commit
            or _git(root, "status", "--porcelain", "--", *SOURCE_FILES)
            or any((root / relative).read_bytes() != body for relative, body in bodies.items())):
        raise ValueError("upstream changed while target snapshot was read")
    upstream = {"root": str(root), "commit": commit, "dirty": False,
                "files": {p: {"sha256": digest(b), "bytes": len(b)} for p, b in bodies.items()}}
    snapshot_id = _identity(upstream)
    metadata = {"schema_version": 1, "snapshot_id": snapshot_id, "captured_at": utc_now(), "upstream": upstream}
    parent = Path(state_root).expanduser() / "targets" / "snapshots"
    parent.mkdir(parents=True, exist_ok=True)
    directory = parent / snapshot_id
    if not directory.exists():
        with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=parent) as temp:
            stage = Path(temp) / "snapshot"
            stage.mkdir()
            for relative, body in bodies.items():
                with (stage / Path(relative).name).open("wb") as stream:
                    stream.write(body); stream.flush(); os.fsync(stream.fileno())
            atomic_json(stage / "metadata.json", metadata)
            try:
                os.rename(stage, directory)
            except OSError:
                if not directory.exists():
                    raise
    snapshot = load_snapshot(state_root, snapshot_id)
    atomic_json(Path(state_root).expanduser() / "targets" / "current.json", {"schema_version": 1, "snapshot_id": snapshot_id})
    return snapshot


def validate_target_ids(snapshot, target_ids):
    validate_snapshot(snapshot)
    _strings(target_ids, "target_ids", empty=False)
    if len(target_ids) > 500 or any(len(value) > 200 for value in target_ids):
        raise ValueError("too many or overlong target_ids")
    registered = {row["id"]: row for row in snapshot["targets"]}
    if any(target_id not in registered for target_id in target_ids):
        raise ValueError("unknown target or target not owned by Fetchspec")
    return [registered[target_id] for target_id in target_ids]
