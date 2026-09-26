"""Build an inresearch delivery package (delivery contract v1) from a company ledger.

The package is what a supplier hands to the inresearch receiving layer:

  deliveries/<delivery_id>/
    manifest.json   delivery envelope + one item per content SHA
    SHA256SUMS      `shasum -a 256 -c SHA256SUMS` from inside the package
    files/<2 hex>/<sha>.<kind>

Items are content identities (one per SHA).  Every file is re-hashed while
packaging; a mismatch is reported and the item left out, never shipped.  Each
delivered SHA is recorded in the ledger so the next package only carries new
content.  Delivery is not acceptance: receipts and research adoption belong to
inresearch.
"""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .company import CompanyLedger, adapter_for
from .inventory import atomic_bytes, utc_now

CONTRACT_VERSION = "1.0"
PROVIDER_ID = "fetchspec"
MIME = {
    "pdf": "application/pdf", "html": "text/html", "csv": "text/csv", "rtf": "application/rtf",
    "doc": "application/msword", "xls": "application/vnd.ms-excel", "ppt": "application/vnd.ms-powerpoint",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odt": "application/vnd.oasis.opendocument.text", "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "odp": "application/vnd.oasis.opendocument.presentation",
}


def collector_revision():
    repo = Path(__file__).resolve().parents[2]
    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", "src", "profiles"],
                               capture_output=True, text=True, check=True).stdout.strip()
        return head + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def place(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        # Different volume (e.g. blobs on an external disk): copy instead.
        shutil.copy2(src, dst)


def version_relation(sequence, sha):
    """sequence: distinct SHAs a source URL returned, oldest first."""
    index = sequence.index(sha) if sha in sequence else 0
    if index == 0:
        return {"type": "original"}
    return {"type": "new_version", "supersedes_sha256": sequence[index - 1]}


def candidates(ledger, adapter):
    """Yield (sha, kind, relative path, source rows) for every stored content."""
    db = ledger.db
    for blob in db.execute("SELECT sha,kind,bytes,path,first_seen FROM blobs WHERE kind!='html' ORDER BY first_seen,sha").fetchall():
        rows = db.execute("""SELECT r.id,r.url,r.categories,r.source_role,o.observed_at,o.final_url,o.metadata
                             FROM observations o JOIN requests r ON r.id=o.request
                             WHERE o.sha=? ORDER BY o.observed_at""", (blob["sha"],)).fetchall()
        yield blob["sha"], blob["kind"], blob["path"], blob["first_seen"], rows
    archive_hosts = set(ledger.profile.get("save_pages_hosts", []))
    if archive_hosts:
        for page in db.execute("""SELECT p.request,p.sha,p.path,p.title,p.observed_at,r.url,r.categories,r.source_role
                                  FROM pages p JOIN requests r ON r.id=p.request ORDER BY p.observed_at""").fetchall():
            if urlsplit(page["url"]).hostname not in archive_hosts:
                continue
            row = {"id": page["request"], "url": page["url"], "categories": page["categories"],
                   "source_role": page["source_role"], "observed_at": page["observed_at"],
                   "final_url": page["url"], "metadata": json.dumps({"title": page["title"]})}
            yield page["sha"], "html", page["path"], page["observed_at"], [row]


def sha_sequence(ledger, request_id, kind):
    if kind == "html":
        rows = ledger.db.execute("SELECT sha FROM pages WHERE request=? ORDER BY observed_at", (request_id,))
    else:
        rows = ledger.db.execute("SELECT sha FROM observations WHERE request=? AND sha IS NOT NULL ORDER BY observed_at", (request_id,))
    seen = []
    for (sha,) in rows:
        if sha not in seen:
            seen.append(sha)
    return seen


def build_delivery(profile, root, delivery_id=None, task_id=None, include_delivered=False, with_files=True):
    root = Path(root)
    ledger = CompanyLedger(root, profile)
    adapter = adapter_for(profile)
    if hasattr(adapter, "archive_spaces"):
        adapter.archive_spaces = {(r[0], r[1]) for r in ledger.db.execute("SELECT host,space FROM space_archive WHERE archived=1")}
    ledger.db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
        delivery_id TEXT, sha TEXT, delivered_at TEXT, PRIMARY KEY(delivery_id, sha))""")
    delivered = {r[0] for r in ledger.db.execute("SELECT sha FROM deliveries")}
    delivery_id = delivery_id or f"{PROVIDER_ID}-{profile['company_id']}-{utc_now()[:19].replace(':', '').replace('-', '')}"
    package = root / "deliveries" / delivery_id
    if package.exists():
        raise ValueError(f"delivery package already exists: {package}")
    items, sums, report = [], [], {"already_delivered": 0, "out_of_current_scope": 0, "missing_file": [], "sha_mismatch": []}
    seen = set()
    for sha, kind, rel_path, first_seen, rows in candidates(ledger, adapter):
        if sha in seen or not rows:
            continue
        seen.add(sha)
        if sha in delivered and not include_delivered:
            report["already_delivered"] += 1
            continue
        # Deliver only what today's language/scope policy would still collect.
        in_scope = [row for row in rows if adapter.in_scope(row["url"])]
        if not in_scope:
            report["out_of_current_scope"] += 1
            continue
        src = root / rel_path
        if not src.exists():
            report["missing_file"].append(sha)
            continue
        if sha256_file(src) != sha:
            report["sha_mismatch"].append(sha)
            continue
        primary = in_scope[0]
        meta = json.loads(primary["metadata"] or "{}")
        content_type = (meta.get("content_type") or "").split(";")[0].strip() or MIME.get(kind, "application/octet-stream")
        target = f"files/{sha[:2]}/{sha}.{kind}"
        urls = sorted({row["url"] for row in in_scope})
        categories = sorted({c for row in in_scope for c in json.loads(row["categories"] or "[]")})
        item = {
            "source_item_id": f"{profile['company_id']}:{primary['id']}",
            "source": {"publisher": profile.get("company_en", profile["company_id"]), "url": primary["url"],
                       "final_url": primary["final_url"] or primary["url"], "also_seen_at": [u for u in urls if u != primary["url"]],
                       "discovery_role": primary["source_role"], "categories": categories},
            "retrieved_at": primary["observed_at"] or first_seen,
            "sha256": sha,
            "bytes": src.stat().st_size,
            "content_type": content_type,
            "format": kind,
            "completeness": {"state": "complete",
                             "basis": "full response read; declared Content-Length matched; format identified from bytes"
                                      if kind != "html" else "full HTML page as served; linked assets not included"},
            "access_scope": {"state": "public", "basis": "unauthenticated request permitted by robots.txt; no login, gate or form submission"},
            "version_relation": version_relation(sha_sequence(ledger, primary["id"], kind), sha),
            "path": target,
        }
        if kind == "html" and meta.get("title"):
            item["title"] = meta["title"]
        items.append(item)
        sums.append(f"{sha}  {target}\n")
        if with_files:
            place(src, package / target)
    envelope = {
        "contract_version": CONTRACT_VERSION,
        "provider_id": PROVIDER_ID,
        "delivery_id": delivery_id,
        "task_id_or_discovery": task_id or "discovery",
        "collector_revision": collector_revision(),
        "company_id": profile["company_id"],
        "created_at": utc_now(),
        "files_included": with_files,
        "known_gaps": profile.get("known_gaps", []),
        "summary": {"items": len(items), "bytes": sum(i["bytes"] for i in items),
                    "by_format": {k: sum(1 for i in items if i["format"] == k) for k in sorted({i["format"] for i in items})},
                    **{k: v for k, v in report.items()}},
        "items": items,
    }
    atomic_bytes(package / "manifest.json", json.dumps(envelope, ensure_ascii=False, indent=2).encode())
    atomic_bytes(package / "SHA256SUMS", "".join(sums).encode())
    now = utc_now()
    ledger.db.executemany("INSERT OR IGNORE INTO deliveries VALUES(?,?,?)", [(delivery_id, i["sha256"], now) for i in items])
    ledger.db.commit()
    ledger.db.close()
    return {"delivery_id": delivery_id, "package": str(package), **envelope["summary"]}
