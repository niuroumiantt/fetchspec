"""Fetchspec v2 packages and receiver acknowledgements, separate from adoption.

A package is immutable and records demand bindings. SQLite records packaging and
validated receipts separately; neither event marks a research target delivered.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from urllib.parse import parse_qsl, unquote, urlsplit
import zipfile

from .adapters import adapter_for
from .deliver import MIME, collector_revision as current_revision, sha256_file
from .inventory import FORMATS, atomic_json, utc_now
from .targets import SHA, canonical_bytes, digest, load_snapshot, validate_target_ids

SUPPORTED_FORMATS = FORMATS | {"html", "json"}  # json: official data components named by a product page
READER_FORMATS = {"pdf", "html", "csv", "docx", "pptx", "xlsx", "xls", "ppt"}
DELIVERY_ID = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
# inresearch supply_contract generated_target_contract.parameter_observation_fields
OBSERVATION_FIELDS = ("company_id", "product_id", "target_id", "part_id", "parameter_name", "value",
                      "unit", "condition", "source_url", "source_sha256", "observed_at")


def _database(state_root):
    root = Path(state_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "delivery-ledger.sqlite", timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS packages (
          delivery_id TEXT PRIMARY KEY, manifest_sha256 TEXT NOT NULL,
          intent_sha256 TEXT NOT NULL, snapshot_id TEXT NOT NULL,
          package_path TEXT NOT NULL, created_at TEXT NOT NULL)""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(receipts)")}
        if columns and "environment" not in columns:
            db.execute("ALTER TABLE receipts RENAME TO receipts_legacy")
        db.execute("""CREATE TABLE IF NOT EXISTS receipts (
          delivery_id TEXT REFERENCES packages(delivery_id), environment TEXT,
          receipt_sha256 TEXT NOT NULL, receipt_json TEXT NOT NULL,
          imported_at TEXT NOT NULL, context_json TEXT NOT NULL,
          PRIMARY KEY(delivery_id,environment))""")
        if columns and "environment" not in columns:
            db.execute("""INSERT INTO receipts
              SELECT delivery_id,'receiver',receipt_sha256,receipt_json,imported_at,'{}'
              FROM receipts_legacy""")
            db.execute("DROP TABLE receipts_legacy")
    return db


def _environment(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValueError("invalid receiver environment")
    return value


def _environment_context(environment):
    if environment == "local_receiver_validation":
        return {"environment": environment, "receipt_scope": "local_validation_only", "production_received": False}
    return {"environment": environment, "receipt_scope": "unspecified" if environment == "receiver" else "operator_declared"}


def _snapshot(state_root, snapshot=None):
    # Demand used for packages is always backed by a validated saved snapshot.
    stored = load_snapshot(state_root)
    if snapshot is not None and stored != snapshot:
        raise ValueError("target snapshot is stale or differs from current persisted authority")
    return stored


def _ids(value, name, *, required=False):
    if (not isinstance(value, list) or (required and not value)
            or any(not isinstance(v, str) or not v or len(v) > 500 for v in value)
            or len(set(value)) != len(value)):
        raise ValueError("invalid " + name)
    return sorted(value)


def _parts(rows):
    return sorted({row["part_id"] for row in rows if row.get("part_id")})


def _validate_format(path, fmt):
    """Match receiver format admission before declaring a complete package."""
    with path.open("rb") as stream:
        head = stream.read(4096)
    valid = False
    if fmt == "pdf":
        valid = head.lstrip().startswith(b"%PDF-")
    elif fmt == "html":
        valid = b"<" in head and b"\x00" not in head
    elif fmt == "csv":
        try:
            head.decode("utf-8-sig")
            valid = b"\x00" not in head
        except UnicodeError:
            pass
    elif fmt == "rtf":
        valid = head.lstrip().startswith(b"{\\rtf")
    elif fmt == "json":
        # Official data component named by a product page (e.g. Micron part specs): a UTF-8 JSON object.
        try:
            valid = path.stat().st_size <= 16 * 1024 * 1024 and isinstance(json.loads(path.read_bytes().decode("utf-8")), dict)
        except (OSError, UnicodeError, ValueError):
            pass
    elif fmt in {"doc", "xls", "ppt"}:
        valid = head.startswith(bytes.fromhex("d0cf11e0a1b11ae1"))
    else:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
            if fmt in {"docx", "docm", "dot", "dotm", "dotx"}:
                valid = "word/document.xml" in names
            elif fmt in {"xlsx", "xlsb", "xlsm", "xlt", "xltm", "xltx"}:
                valid = bool({"xl/workbook.xml", "xl/workbook.bin"} & names)
            elif fmt in {"pptx", "pptm", "pps", "ppsm", "ppsx", "pot", "potm", "potx"}:
                valid = "ppt/presentation.xml" in names
            elif fmt in {"odt", "ods", "odp"}:
                valid = bool({"mimetype", "content.xml"} & names)
        except (OSError, zipfile.BadZipFile):
            pass
    if not valid:
        raise ValueError("declared format does not match bytes")


NON_TARGET_LANGUAGES = {"ja", "jp", "japanese", "ko", "kr", "korean", "de", "german", "fr", "french",
                        "es", "spanish", "it", "italian", "pt", "portuguese", "ru", "russian", "ar",
                        "cs", "da", "fi", "he", "id", "nb", "nl", "no", "pl", "ro", "sv", "th", "tr", "uk", "vi"}


def _language_marker(value, *, explicit=False):
    marker = value.casefold().replace("_", "-")
    if marker in {"en", "english"} or re.fullmatch(r"en-[a-z]{2,4}", marker):
        return "en"
    if marker in {"zh", "chinese", "scn", "tcn", "chs", "cht"} or re.fullmatch(r"zh-(?:cn|tw|hk|hans|hant)", marker):
        return "zh"
    if explicit or marker.split("-", 1)[0] in NON_TARGET_LANGUAGES:
        raise ValueError("source URL declares a non-English/non-Chinese language")
    return None


def _official_source_url(adapter, value, declared):
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise ValueError("invalid official source URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.hostname not in adapter.profile["allowed_hosts"]
            or not adapter.normalize(value)):
        raise ValueError("source URL is outside the registered company's public official hosts/languages")
    languages = {"zh"} if parsed.hostname.endswith(".cn") else set()
    path = unquote(parsed.path)
    segments = [segment for segment in path.split("/") if segment]
    for segment in segments:
        marker = _language_marker(segment)
        if marker:
            languages.add(marker)
    filename = Path(path).stem
    match = re.search(r"(?:^|[-_.])([a-z]{2}(?:[-_](?:[a-z]{2}|hans|hant))?|scn|tcn|english|chinese)$", filename, re.I)
    if match:
        marker = _language_marker(match.group(1))
        if marker:
            languages.add(marker)
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.casefold() in {"lang", "language", "locale", "hl"}:
            languages.add(_language_marker(value, explicit=True))
    expected = "en" if declared == "en_or_unmarked" else declared
    if languages and languages != {expected}:
        raise ValueError("source URL language conflicts with declared source language")
    return value


def _source(value, adapter):
    if not isinstance(value, dict):
        raise ValueError("source metadata required")
    result = json.loads(canonical_bytes(value))
    language = result.get("language")
    if language not in {"en", "zh", "en_or_unmarked"}:
        raise ValueError("source requires English/Chinese language")
    _official_source_url(adapter, result.get("url"), language)
    if "final_url" in result:
        _official_source_url(adapter, result["final_url"], language)
    if "also_seen_at" in result:
        result["also_seen_at"] = _ids(result["also_seen_at"], "source aliases")
        for alias in result["also_seen_at"]:
            _official_source_url(adapter, alias, language)
    result["categories"] = _ids(result.get("categories", []), "source categories")
    if any(len(value) > 160 for value in result["categories"]):
        raise ValueError("source category exceeds receiver limit")
    return result


def _prepare(snapshot, company_id, raw_items):
    adapter = adapter_for(company_id)
    if not isinstance(raw_items, (list, tuple)) or not raw_items or len(raw_items) > 10000:
        raise ValueError("a nonempty, bounded delivery item list is required")
    groups = {}
    source_keys = {(row.get("source", {}).get("url"), row.get("sha256")) for row in raw_items if isinstance(row, dict) and isinstance(row.get("source"), dict)}
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("delivery item must be an object")
        sha, fmt = raw.get("sha256"), raw.get("format")
        if not isinstance(sha, str) or not SHA.fullmatch(sha) or not isinstance(fmt, str) or fmt not in SUPPORTED_FORMATS:
            raise ValueError("invalid item content identity or format")
        blob = Path(raw["blob_path"]).expanduser().resolve(strict=True)
        if not blob.is_file() or sha256_file(blob) != sha:
            raise ValueError("source blob SHA mismatch")
        _validate_format(blob, fmt)
        rows = validate_target_ids(snapshot, raw.get("target_ids"))
        source = _source(raw.get("source"), adapter)
        if not isinstance(raw.get("source_item_id"), str) or not raw["source_item_id"]:
            raise ValueError("source_item_id required")
        if not isinstance(raw.get("retrieved_at"), str) or not raw["retrieved_at"]:
            raise ValueError("retrieved_at required")
        relation = raw.get("version_relation", {"type": "original"})
        if (not isinstance(relation, dict) or relation.get("type") not in {"original", "new_version"}
                or (relation["type"] == "new_version" and not SHA.fullmatch(str(relation.get("supersedes_sha256", ""))))
                or relation.get("supersedes_sha256") == sha):
            raise ValueError("invalid version relation")
        access = raw.get("access_scope", {"state": "public"})
        completeness = raw.get("completeness", {"state": "complete", "basis": "SHA verified local source snapshot"})
        if not isinstance(access, dict) or access.get("state") != "public" or not isinstance(completeness, dict):
            raise ValueError("invalid access_scope/completeness")
        observation = {"source_item_id": raw["source_item_id"], "source": source,
                       "retrieved_at": raw["retrieved_at"], "target_ids": sorted(raw["target_ids"]),
                       "part_ids": _parts(rows), "product_ids": _ids(raw.get("product_ids", []), "product_ids"),
                       "version_relation": relation}
        product_evidence = []
        evidence_values = raw.get("product_evidence", [])
        if not isinstance(evidence_values, list):
            raise ValueError("product_evidence must be a list")
        for original in evidence_values:
            if not isinstance(original, dict) or original.get("product_id") not in observation["product_ids"]:
                raise ValueError("product evidence must name a product bound to this source")
            evidence = json.loads(canonical_bytes(original))
            evidence_targets = evidence.get("target_ids", observation["target_ids"])
            selected = validate_target_ids(snapshot, evidence_targets)
            if set(evidence_targets) - set(observation["target_ids"]):
                raise ValueError("product evidence target is outside source binding")
            evidence["target_ids"] = sorted(evidence_targets)
            evidence["part_ids"] = _parts(selected)
            tables = evidence.get("specification_tables", [])
            if not isinstance(tables, list):
                raise ValueError("specification_tables must be a list")
            for table in tables:
                if not isinstance(table, dict) or not isinstance(table.get("rows"), list):
                    raise ValueError("original specification table rows are required")
                refs = table.get("source_refs", [])
                if not isinstance(refs, list) or any(not isinstance(ref, dict) or (ref.get("url"), ref.get("sha256")) not in source_keys for ref in refs):
                    raise ValueError("specification source reference is not included in package")
            parameters = evidence.get("parameter_observations", [])
            if not isinstance(parameters, list):
                raise ValueError("parameter_observations must be a list")
            for parameter in parameters:
                if (not isinstance(parameter, dict) or any(key not in parameter for key in OBSERVATION_FIELDS)
                        or parameter["company_id"] != company_id or parameter["product_id"] != evidence.get("product_id")
                        or parameter["target_id"] not in evidence["target_ids"]
                        or not isinstance(parameter["value"], str) or not isinstance(parameter["parameter_name"], str)
                        or (parameter["source_url"], parameter["source_sha256"]) not in source_keys):
                    raise ValueError("parameter observation must name a bound target, this product and an included source")
            product_evidence.append(evidence)
        item = {**observation, "product_evidence": product_evidence, "sha256": sha, "format": fmt, "bytes": blob.stat().st_size,
                "content_type": raw.get("content_type") or MIME.get(fmt, "application/octet-stream"),
                "completeness": completeness, "access_scope": access,
                "path": f"files/{sha[:2]}/{sha}.{fmt}"}
        if raw.get("title"):
            item["title"] = str(raw["title"])
        groups.setdefault(sha, []).append((item, observation, blob))
    prepared, blobs = [], {}
    for sha, values in sorted(groups.items()):
        if len({item["format"] for item, _, _ in values}) != 1:
            raise ValueError("same bytes declared in conflicting formats")
        # Stable primary source and complete observations, even across renamed URLs.
        values.sort(key=lambda value: canonical_bytes(value[1]))
        item = dict(values[0][0])
        observations = {canonical_bytes(obs): obs for _, obs, _ in values}
        item["source_observations"] = [observations[key] for key in sorted(observations)]
        item["target_ids"] = sorted({tid for _, obs, _ in values for tid in obs["target_ids"]})
        item["part_ids"] = _parts(validate_target_ids(snapshot, item["target_ids"]))
        item["product_ids"] = sorted({pid for _, obs, _ in values for pid in obs["product_ids"]})
        evidence_by_content = {}
        for candidate, _, _ in values:
            for evidence in candidate["product_evidence"]:
                key = canonical_bytes({k: v for k, v in evidence.items() if k not in {"target_ids", "part_ids"}})
                if key not in evidence_by_content:
                    evidence_by_content[key] = dict(evidence)
                else:
                    kept = evidence_by_content[key]
                    kept["target_ids"] = sorted(set(kept["target_ids"]) | set(evidence["target_ids"]))
                    kept["part_ids"] = _parts(validate_target_ids(snapshot, kept["target_ids"]))
        item["product_evidence"] = [evidence_by_content[key] for key in sorted(evidence_by_content)]
        item["source"] = dict(item["source"])
        item["source"]["categories"] = sorted({category for _, obs, _ in values for category in obs["source"]["categories"]})
        primary_language = "en" if item["source"]["language"] == "en_or_unmarked" else item["source"]["language"]
        aliases = set()
        for _, obs, _ in values:
            source_language = "en" if obs["source"]["language"] == "en_or_unmarked" else obs["source"]["language"]
            if source_language == primary_language:
                aliases.add(obs["source"]["url"])
                aliases.update(obs["source"].get("also_seen_at", []))
        item["source"]["also_seen_at"] = sorted(aliases - {item["source"]["url"]})
        prepared.append(item)
        blobs[sha] = values[0][2]
    if sum(item["bytes"] for item in prepared) > 20 * 1024**3:
        raise ValueError("delivery exceeds receiver byte limit")
    return prepared, blobs


def _verify_package(package, expected_sha=None):
    manifest_path = package / "manifest.json"
    if package.is_symlink() or manifest_path.is_symlink():
        raise ValueError("unsafe package path")
    body = manifest_path.read_bytes()
    sha = digest(body)
    if expected_sha is not None and expected_sha != sha:
        raise ValueError("package manifest SHA mismatch")
    manifest = json.loads(body)
    if manifest.get("contract_version") != "2.0" or manifest.get("provider_id") != "fetchspec":
        raise ValueError("unexpected stored package contract")
    sums = []
    for item in manifest["items"]:
        expected_path = f'files/{item["sha256"][:2]}/{item["sha256"]}.{item["format"]}'
        if item["path"] != expected_path or not SHA.fullmatch(item["sha256"]):
            raise ValueError("invalid stored package content path")
        path = package / item["path"]
        if path.is_symlink() or not path.resolve().is_relative_to(package.resolve()):
            raise ValueError("unsafe stored package content")
        if path.stat().st_size != item["bytes"] or sha256_file(path) != item["sha256"]:
            raise ValueError("stored package content changed")
        sums.append(f'{item["sha256"]}  {item["path"]}\n')
    if (package / "SHA256SUMS").is_symlink() or (package / "SHA256SUMS").read_text() != "".join(sums):
        raise ValueError("stored package checksum manifest changed")
    return manifest, sha


def build_package(state_root, snapshot, company_id, items, *, delivery_id=None, collector_revision=None):
    snapshot = _snapshot(state_root, snapshot)
    if not isinstance(company_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", company_id):
        raise ValueError("invalid company_id")
    prepared, blobs = _prepare(snapshot, company_id, items)
    target_ids = sorted({tid for item in prepared for tid in item["target_ids"]})
    validate_target_ids(snapshot, target_ids)
    revision = collector_revision or current_revision()
    intent = {"company_id": company_id, "target_snapshot_id": snapshot["snapshot_id"],
              "collector_revision": revision, "items": prepared}
    intent_sha = digest(canonical_bytes(intent))
    delivery_id = delivery_id or f"fetchspec-{company_id}-{intent_sha[:24]}"
    if not isinstance(delivery_id, str) or not DELIVERY_ID.fullmatch(delivery_id) or delivery_id in {".", ".."}:
        raise ValueError("invalid delivery_id")
    package = Path(state_root).expanduser().resolve() / "deliveries" / delivery_id
    with closing(_database(state_root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT * FROM packages WHERE delivery_id=?", (delivery_id,)).fetchone()
        replayed = package.exists()
        if prior and prior["intent_sha256"] != intent_sha:
            raise ValueError("delivery_id reused with different package intent")
        if replayed:
            manifest, manifest_sha = _verify_package(package, prior["manifest_sha256"] if prior else None)
            stored_intent = {key: manifest.get(key) for key in ("company_id", "target_snapshot_id", "collector_revision", "items")}
            if (manifest.get("intent_sha256") != intent_sha or digest(canonical_bytes(stored_intent)) != intent_sha
                    or manifest.get("delivery_id") != delivery_id or manifest.get("target_ids") != target_ids
                    or manifest.get("part_ids") != _parts(validate_target_ids(snapshot, target_ids))):
                raise ValueError("delivery_id reused with different package content")
        else:
            if prior:
                raise ValueError("registered package is missing; restore it before retrying")
            manifest = {"contract_version": "2.0", "provider_id": "fetchspec", "delivery_id": delivery_id,
                        "company_id": company_id, "collector_revision": revision, "created_at": utc_now(),
                        "task_id_or_discovery": "discovery", "target_ids": target_ids,
                        "part_ids": _parts(validate_target_ids(snapshot, target_ids)),
                        "collection_trigger": "generated_targets", "target_snapshot_id": snapshot["snapshot_id"],
                        "upstream": snapshot["upstream"], "intent_sha256": intent_sha,
                        "files_included": True, "acceptance": "candidate", "items": prepared}
            observation_count = sum(len(e.get("parameter_observations", [])) for i in prepared for e in i["product_evidence"])
            if observation_count:
                manifest["parameter_observations"] = {"count": observation_count, "fields": list(OBSERVATION_FIELDS),
                                                      "location": "items[].product_evidence[].parameter_observations",
                                                      "value_policy": "original vendor cell text; reviewed unit/condition; no conversion"}
            package.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".package-", dir=package.parent) as temp:
                stage = Path(temp) / "package"
                stage.mkdir()
                for item in prepared:
                    destination = stage / item["path"]
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(blobs[item["sha256"]], destination)
                    with destination.open("rb") as stream:
                        os.fsync(stream.fileno())
                    if sha256_file(destination) != item["sha256"]:
                        raise ValueError("source changed while packaging")
                atomic_json(stage / "manifest.json", manifest)
                sums = "".join(f'{item["sha256"]}  {item["path"]}\n' for item in prepared)
                with (stage / "SHA256SUMS").open("w") as stream:
                    stream.write(sums); stream.flush(); os.fsync(stream.fileno())
                os.rename(stage, package)
            manifest, manifest_sha = _verify_package(package)
        db.execute("INSERT OR IGNORE INTO packages VALUES(?,?,?,?,?,?)",
                   (delivery_id, manifest_sha, intent_sha, snapshot["snapshot_id"], str(package), manifest["created_at"]))
    return {"delivery_id": delivery_id, "package": str(package), "manifest_sha256": manifest_sha,
            "items": len(prepared), "target_ids": target_ids, "replayed": replayed, "status": "packaged"}


def _validate_receipt(receipt, manifest, manifest_sha, snapshot):
    if (not isinstance(receipt, dict) or receipt.get("delivery_id") != manifest["delivery_id"]
            or receipt.get("manifest_sha256") != manifest_sha or receipt.get("company_id") != manifest["company_id"]
            or receipt.get("status") not in {"received", "needs_supplement"}
            or receipt.get("task_id_or_discovery") != manifest["task_id_or_discovery"]):
        raise ValueError("receipt identity/status does not acknowledge this package")
    context = receipt.get("research_context")
    if not isinstance(context, dict):
        raise ValueError("receipt research_context required")
    expected_targets = manifest["target_ids"]
    current_parts = _parts(validate_target_ids(snapshot, expected_targets))
    if (_ids(context.get("target_ids"), "receipt targets", required=True) != expected_targets
            or _ids(context.get("part_ids"), "receipt parts") != current_parts
            or manifest["part_ids"] != current_parts):
        raise ValueError("receipt target/part binding mismatch or upstream mapping changed")
    received = receipt.get("items")
    if (not isinstance(received, list) or type(receipt.get("received_items")) is not int
            or receipt["received_items"] != len(manifest["items"]) or len(received) != len(manifest["items"])):
        raise ValueError("receipt item count mismatch")
    expected = {item["sha256"]: item for item in manifest["items"]}
    seen, handoffs, supplemental = set(), 0, False
    for item in received:
        if not isinstance(item, dict) or item.get("sha256") not in expected or item["sha256"] in seen:
            raise ValueError("receipt SHA set mismatch")
        seen.add(item["sha256"])
        original = expected[item["sha256"]]
        validate_target_ids(snapshot, original["target_ids"])
        readable = original["format"] in READER_FORMATS
        status = "received" if readable else "needs_supplement"
        handoff = item.get("reader_handoff")
        if (item.get("format") != original["format"]
                or _ids(item.get("target_ids"), "item receipt targets", required=True) != original["target_ids"]
                or item.get("status") != status
                or handoff not in ({"held", "eligible"} if readable else {"extractor_required"})):
            raise ValueError("receipt item binding/status mismatch")
        handoffs += handoff == "eligible"
        supplemental |= not readable
    if (receipt.get("status") != ("needs_supplement" if supplemental else "received")
            or type(receipt.get("reader_handoff_count")) is not int or receipt["reader_handoff_count"] != handoffs):
        raise ValueError("receipt summary does not match item acknowledgements")


def import_receipt(state_root, receipt, *, snapshot=None, environment="receiver", context=None):
    """Record raw acknowledgement bytes in an explicitly scoped receiver namespace.

    ``receiver`` preserves compatibility but does not assert a production origin.
    Environmental metadata is external to the official receipt and cannot change it.
    """
    snapshot = _snapshot(state_root, snapshot)
    environment = _environment(environment)
    context = {} if context is None else context
    if not isinstance(context, dict):
        raise ValueError("receipt context must be an object")
    context_body = canonical_bytes(context).decode()
    if isinstance(receipt, (str, Path)):
        raw_body = Path(receipt).read_bytes()
        receipt = json.loads(raw_body)
    else:
        raw_body = canonical_bytes(receipt)
    if not isinstance(receipt, dict):
        raise ValueError("receipt must be an object")
    body = canonical_bytes(receipt)
    input_sha = digest(raw_body)
    receipt_sha = input_sha
    with closing(_database(state_root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        package = db.execute("SELECT * FROM packages WHERE delivery_id=?", (receipt.get("delivery_id"),)).fetchone()
        if package is None:
            raise ValueError("unknown receipt delivery_id")
        manifest, manifest_sha = _verify_package(Path(package["package_path"]), package["manifest_sha256"])
        _validate_receipt(receipt, manifest, manifest_sha, snapshot)
        prior = db.execute("SELECT * FROM receipts WHERE delivery_id=? AND environment=?",
                           (manifest["delivery_id"], environment)).fetchone()
        if prior:
            if digest(prior["receipt_json"].encode("utf-8")) != prior["receipt_sha256"]:
                raise ValueError("stored raw receipt SHA mismatch")
            if canonical_bytes(json.loads(prior["receipt_json"].encode("utf-8"))) != body:
                raise ValueError("conflicting receipt for existing delivery/environment; review required")
            receipt_sha = prior["receipt_sha256"]
        db.execute("INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?,?)",
                   (manifest["delivery_id"], environment, input_sha, raw_body.decode("utf-8"), utc_now(), context_body))
    return {"delivery_id": manifest["delivery_id"], "receipt_sha256": receipt_sha,
            "input_receipt_sha256": input_sha, "manifest_sha256": manifest_sha,
            "status": "receipt_validated", "replayed": bool(prior),
            "target_ids": manifest["target_ids"], "part_ids": manifest["part_ids"],
            "research_adoption": "not_inferred", "git_target_status": "unchanged",
            **_environment_context(environment)}


def _received_delivery(state_root, delivery_id, snapshot, environment):
    """Return (row, manifest, manifest_sha, receipt) only for a re-verified receipt."""
    with closing(_database(state_root)) as db:
        row = db.execute("SELECT p.*,r.receipt_sha256,r.receipt_json,r.context_json,r.imported_at FROM packages p JOIN receipts r USING(delivery_id) WHERE delivery_id=? AND environment=?", (delivery_id, environment)).fetchone()
    if row is None:
        raise ValueError("a validated receipt is required for an author proposal")
    manifest, manifest_sha = _verify_package(Path(row["package_path"]), row["manifest_sha256"])
    if digest(row["receipt_json"].encode("utf-8")) != row["receipt_sha256"]:
        raise ValueError("stored raw receipt SHA mismatch")
    receipt = json.loads(row["receipt_json"].encode("utf-8"))
    _validate_receipt(receipt, manifest, manifest_sha, snapshot)
    return row, manifest, manifest_sha, receipt


UPSTREAM_IMPORT = "python3 manage.py deliveries import --assignments <this file>"


def export_author_proposal(state_root, delivery_id, *, snapshot=None, output_path=None, environment="receiver"):
    """Export a reviewable proposal; never write an inresearch author checkout."""
    snapshot = _snapshot(state_root, snapshot)
    environment = _environment(environment)
    row, manifest, manifest_sha, receipt = _received_delivery(state_root, delivery_id, snapshot, environment)
    records = []
    for item in manifest["items"]:
        for target in validate_target_ids(snapshot, item["target_ids"]):
            evidence = [entry for entry in item["product_evidence"] if target["id"] in entry["target_ids"]]
            products = sorted({entry["product_id"] for entry in evidence}) if item["product_evidence"] else item["product_ids"]
            records.append({"target_id": target["id"], "part_id": target["part_id"],
                            "variable_class": target["variable_class"], "company_id": manifest["company_id"],
                            "source_item_id": item["source_item_id"], "product_ids": products, "product_evidence": evidence,
                            "source_sha256": item["sha256"], "source_url": item["source"]["url"],
                            "language": item["source"]["language"], "format": item["format"],
                            "version_relation": item["version_relation"],
                            "source_observations": item["source_observations"], "acceptance": "candidate"})
    result = {"schema_version": 1, "kind": "fetchspec_author_registration_proposal",
              "status": "validation_only" if environment == "local_receiver_validation" else "proposed",
              **_environment_context(environment), "receiver_context": json.loads(row["context_json"]),
              "delivery_id": delivery_id, "manifest_sha256": manifest_sha,
              "receipt_sha256": row["receipt_sha256"], "target_snapshot_id": snapshot["snapshot_id"],
              "package_target_snapshot_id": manifest["target_snapshot_id"], "records": records,
              "suggested_carrier": "data/event_cards.json via deliveries import (per target_id)",
              "upstream_import": "fetchspec.pipeline assignments --delivery-id " + delivery_id + " --output <file>; then " + UPSTREAM_IMPORT,
              "review_required": True, "research_adoption": "not_inferred", "git_target_status": "unchanged",
              "limitations": ["Only the author checkout can change target status; this proposal never writes it.",
                              "Use the event-card carrier: data/product_docs_plan.csv marks every spec and operation target of a bom_part at once."]}
    if output_path is not None:
        atomic_json(Path(output_path), result)
    return result


def _primary_item(items, target_id):
    """Choose one stable, public pointer per target: the product page before attachments."""
    candidates = [item for item in items if target_id in item["target_ids"]]
    candidates.sort(key=lambda item: (item["format"] != "html", not item["product_evidence"], item["source"]["url"], item["sha256"]))
    return candidates[0], candidates


def export_assignments(state_root, delivery_id, *, snapshot=None, output_path=None, environment="receiver",
                       by="fetchspec", allow_validation=False):
    """Write the runtime ``assignments.json`` shape consumed by inresearch ``deliveries import``.

    One record per bound target: target_id + a public official evidence URL, plus the
    delivery/receipt identities in the note and the target's reviewed parameter observations
    (``fetchspec.observations``: original text, unit, condition, source URL and SHA). Receipts from local receiver validation are
    refused unless explicitly allowed for rehearsal, and are then marked as such.
    """
    snapshot = _snapshot(state_root, snapshot)
    environment = _environment(environment)
    if not isinstance(by, str) or not re.fullmatch(r"[A-Za-z0-9._@-]{1,80}", by):
        raise ValueError("invalid --by identity")
    local_only = environment == "local_receiver_validation"
    if local_only and not allow_validation:
        raise ValueError("local receiver validation receipts do not prove production receipt; pass --allow-validation only for a rehearsal")
    row, manifest, manifest_sha, receipt = _received_delivery(state_root, delivery_id, snapshot, environment)
    received_at = str(row["imported_at"])[:10]
    records = []
    for target in validate_target_ids(snapshot, manifest["target_ids"]):
        primary, related = _primary_item(manifest["items"], target["id"])
        url = primary["source"]["url"]
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("evidence pointer must be a public https URL")
        products = sorted({entry["product_id"] for item in related for entry in item["product_evidence"]
                           if target["id"] in entry["target_ids"]} or {pid for item in related for pid in item["product_ids"]})
        observations = sorted({(entry["product_id"], o["parameter_name"], o["value"], o["unit"], o["condition"],
                                o["source_url"], o["source_sha256"], o["observed_at"])
                               for item in related for entry in item["product_evidence"]
                               for o in entry.get("parameter_observations", []) if o["target_id"] == target["id"]})
        note = (("REHEARSAL local_receiver_validation; " if local_only else "")
                + f"fetchspec {manifest['delivery_id']} manifest {manifest_sha[:16]} receipt {row['receipt_sha256'][:16]} "
                + f"env {environment}; products {','.join(products)}; sha256 {','.join(sorted(i['sha256'][:16] for i in related))}")
        records.append({"target_id": target["id"], "assignee": by, "status": "已交付",
                        "delivery": {"evidence_path": url, "at": received_at, "by": by, "note": note[:500]},
                        "fetchspec": {"delivery_id": manifest["delivery_id"], "manifest_sha256": manifest_sha,
                                      "parameter_observations": len(observations),
                                      # the reviewed values themselves, so the author import can show them next to the pointer
                                      "observations": [dict(zip(("product_id", "parameter_name", "value", "unit", "condition",
                                                                 "source_url", "source_sha256", "observed_at"), o)) for o in observations],
                                      "receipt_sha256": row["receipt_sha256"], "environment": environment,
                                      "part_id": target["part_id"], "product_ids": products,
                                      "source_sha256": sorted(i["sha256"] for i in related), "acceptance": "candidate"}})
    result = {"version": 1, "updated": received_at, "records": records,
              "source": {"kind": "fetchspec_assignments_export", "delivery_id": delivery_id,
                         "target_snapshot_id": snapshot["snapshot_id"], "upstream_commit": snapshot["upstream"]["commit"],
                         **_environment_context(environment), "rehearsal": local_only,
                         "import_with": UPSTREAM_IMPORT, "git_target_status": "unchanged_until_author_import",
                         "research_adoption": "not_inferred"}}
    if output_path is not None:
        atomic_json(Path(output_path), result)
    return result
