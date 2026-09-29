"""Build a delivery package (inresearch supply delivery contract v1).

A package is a self-contained directory under <data root>/deliveries/:

    manifest.json    delivery envelope (provider, delivery id, task/discovery, collector revision, counts)
    items.jsonl      one item per unique document content (SHA-256), with every observed source
    SHA256SUMS       `shasum -a 256 -c` compatible list of blobs/
    blobs/<2 hex>/<sha>.<kind>   originals, hard-linked from the archive when possible
    library/<company>/...        relative symlinks into blobs/ for browsing

Contents already listed in an earlier package for the same company are not
repeated, so packages are incremental. Building a package never changes the
acquisition ledger, and delivery is not acceptance: receipt states
(received / needs_supplement / accepted / rejected) belong to inresearch.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

from .inventory import ROOT, atomic_bytes, atomic_json, utc_now

CONTRACT_VERSION = "1.0"
PROVIDER_ID = "fetchspec"
MIME = {
    "pdf": "application/pdf",
    "doc": "application/msword", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint", "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "csv": "text/csv", "rtf": "application/rtf",
}


def collector_revision():
    try:
        sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", "src", "profiles"],
                               capture_output=True, text=True, check=True).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def delivered_shas(deliveries, company):
    shas = set()
    for manifest in sorted(Path(deliveries).glob("*/manifest.json")):
        envelope = json.loads(manifest.read_text())
        if envelope.get("company_id") != company:
            continue
        for line in (manifest.parent / "items.jsonl").read_text().splitlines():
            shas.add(json.loads(line)["sha256"])
    return shas


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def link_or_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def build_items(db, company, skip):
    items = []
    for blob in db.execute("SELECT sha,kind,bytes,path,first_seen FROM blobs ORDER BY first_seen,sha"):
        if blob["sha"] in skip:
            continue
        observations = db.execute(
            """SELECT r.id,r.url,r.categories,o.observed_at,o.final_url,o.metadata FROM observations o
               JOIN requests r ON r.id=o.request WHERE o.sha=? AND o.status=200 ORDER BY o.observed_at""",
            (blob["sha"],)).fetchall()
        sources, seen, categories = [], set(), set()
        for row in observations:
            categories.update(json.loads(row["categories"] or "[]"))
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            meta = json.loads(row["metadata"] or "{}")
            sources.append({"request_id": row["id"], "url": row["url"], "final_url": row["final_url"],
                            "first_retrieved_at": row["observed_at"], "etag": meta.get("etag"),
                            "last_modified": meta.get("last_modified")})
        if not sources:
            # Legacy blobs imported without a 200 observation: keep the ledger's request record.
            for row in db.execute("SELECT id,url,categories,first_seen FROM requests WHERE latest_sha=?", (blob["sha"],)):
                categories.update(json.loads(row["categories"] or "[]"))
                sources.append({"request_id": row["id"], "url": row["url"], "final_url": row["url"],
                                "first_retrieved_at": row["first_seen"], "etag": None, "last_modified": None})
        prior = set()
        for source in sources:
            for (older,) in db.execute("SELECT DISTINCT sha FROM observations WHERE request=? AND sha IS NOT NULL AND sha!=? AND observed_at<?",
                                       (source["request_id"], blob["sha"], source["first_retrieved_at"])):
                prior.add(older)
        views = [r[0] for r in db.execute("SELECT path FROM views WHERE sha=? ORDER BY path", (blob["sha"],))]
        items.append({
            "source_item_id": f"{company}:{blob['sha']}",
            "source": {"company_id": company, "primary_url": sources[0]["url"] if sources else None,
                       "urls": sources, "vendor_categories": sorted(categories)},
            "retrieved_at": sources[0]["first_retrieved_at"] if sources else blob["first_seen"],
            "sha256": blob["sha"],
            "content_type": MIME.get(blob["kind"], "application/octet-stream"),
            "format": blob["kind"],
            "bytes": blob["bytes"],
            "completeness": "complete_bytes_verified",
            "access_scope": "public_web_no_login",
            "version_relation": {"type": "revision", "supersedes": sorted(prior)} if prior else {"type": "new"},
            "archive_path": blob["path"],
            "library_views": views,
        })
    return items


def build_delivery(profile, root, task_id=None, delivery_id=None, verify=True):
    company = profile["company_id"]
    root = Path(root)
    ledger_path = root / "ledger" / "companies" / company / "crawl.sqlite"
    db = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    deliveries = root / "deliveries"
    skip = delivered_shas(deliveries, company) if deliveries.exists() else set()
    items = build_items(db, company, skip)
    db.close()
    if not items:
        return {"status": "nothing_new", "company_id": company, "already_delivered": len(skip)}

    delivery_id = delivery_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + company
    package = deliveries / delivery_id
    if package.exists():
        raise FileExistsError(f"delivery already exists: {package}")
    staging = deliveries / (".building-" + delivery_id)
    if staging.exists():
        shutil.rmtree(staging)
    sums = []
    for item in items:
        source = root / item["archive_path"]
        if verify and file_sha256(source) != item["sha256"]:
            raise ValueError(f"archive blob failed SHA-256 check: {source}")
        rel = Path("blobs") / item["sha256"][:2] / Path(item["archive_path"]).name
        link_or_copy(source, staging / rel)
        item["package_path"] = str(rel)
        sums.append(f"{item['sha256']}  {rel}\n")
        for view in item.pop("library_views"):
            target = staging / view
            if not target.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(os.path.relpath(staging / rel, target.parent))
        item.pop("archive_path")

    envelope = {
        "contract_version": CONTRACT_VERSION,
        "provider_id": PROVIDER_ID,
        "delivery_id": delivery_id,
        "task_id_or_discovery": {"task_id": task_id} if task_id else {"discovery": "proactive_company_crawl"},
        "collector_revision": collector_revision(),
        "company_id": company,
        "profile_status": profile.get("status"),
        "created_at": utc_now(),
        "item_count": len(items),
        "total_bytes": sum(item["bytes"] for item in items),
        "formats": dict(sorted({i["format"]: sum(1 for j in items if j["format"] == i["format"]) for i in items}.items())),
        "previously_delivered_items": len(skip),
        "items_file": "items.jsonl",
        "checksums_file": "SHA256SUMS",
        "known_gaps": profile.get("known_gaps", []),
        "notes": "Delivery is not acceptance or research adoption; inresearch records receipt states per item.",
    }
    atomic_bytes(staging / "items.jsonl", "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items).encode())
    atomic_bytes(staging / "SHA256SUMS", "".join(sums).encode())
    atomic_json(staging / "manifest.json", envelope)
    staging.rename(package)
    return {"status": "built", "package": str(package), **{k: envelope[k] for k in ("delivery_id", "item_count", "total_bytes", "formats", "previously_delivered_items")}}
