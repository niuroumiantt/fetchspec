from datetime import datetime, timezone, date
import hashlib
import json
import os
from pathlib import Path

from .catalog import ROOT
from .naming import catalog_relpath, doc_type, guess_model, parse_library_path

LAYOUT_VERSION = 1
SPARK_PRODUCT_LIBRARY = "~/.local/share/inresearch.ai/product/library/"
SPARK_ACQUISITION_BLOBS = "~/.local/share/inresearch.ai/acquisition/blobs/"


def now():
    return datetime.now(timezone.utc).isoformat()


def sha256(body):
    return hashlib.sha256(body).hexdigest()


def default_data_root():
    override = os.environ.get("FETCHSPEC_DATA_ROOT")
    if override:
        return Path(override).expanduser()
    local = ROOT / "config" / "archive.local.json"
    if local.exists():
        data = json.loads(local.read_text(encoding="utf-8"))
        root = data.get("data_root")
        if root:
            return Path(root).expanduser()
    return Path.home() / ".local" / "share" / "fetchspec"


def layout_document():
    return {
        "layout_version": LAYOUT_VERSION,
        "role": "local-pre-spark-archive",
        "binaries_in_git": False,
        "identity": "sha256 of bytes; company_id + product_line match inresearch data/products.json",
        "trees": {
            "blobs/": "immutable content-addressed originals (same shape as inresearch acquisition/blobs)",
            "library/": "human/catalog tree; same shape as inresearch product/library",
            "ledger/catalog.json": "all captured items; merge on Spark into product_library_index.json (doc_id assigned there)",
            "ledger/runs/": "per-crawl receipts; not research authority",
        },
        "spark_when_ready": {
            "library/": SPARK_PRODUCT_LIBRARY,
            "blobs/": SPARK_ACQUISITION_BLOBS,
            "ledger/catalog.json": "merge records; do not copy this file over an existing index",
        },
        "move": [
            "rsync -a --partial library/  spark:.local/share/inresearch.ai/product/library/",
            "rsync -a --partial blobs/    spark:.local/share/inresearch.ai/acquisition/blobs/",
        ],
    }


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.blobs = self.root / "blobs"
        self.library = self.root / "library"
        self.ledger = self.root / "ledger"
        self.runs = self.ledger / "runs"
        for path in (self.blobs, self.library, self.ledger, self.runs):
            path.mkdir(parents=True, exist_ok=True)
        layout = self.root / "LAYOUT.json"
        if not layout.exists():
            layout.write_text(json.dumps(layout_document(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._catalog_path = self.ledger / "catalog.json"
        self.catalog = self._load_catalog()

    def _load_catalog(self):
        if self._catalog_path.exists():
            return json.loads(self._catalog_path.read_text(encoding="utf-8"))
        return {
            "layout_version": LAYOUT_VERSION,
            "note": "Local fetchspec archive. Binaries are not in git. Spark assigns doc_id on merge.",
            "records": [],
        }

    def _save_catalog(self):
        tmp = self._catalog_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self._catalog_path)

    def _link_or_copy(self, blob, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            return
        try:
            os.link(blob, dest)
        except OSError:
            dest.write_bytes(blob.read_bytes())

    def capture(self, body, suffix, kind, url, requested, status, content_type, rule, line):
        digest = sha256(body)
        rel_blob = f"blobs/{digest[:2]}/{digest}{suffix}"
        blob = self.root / rel_blob
        blob.parent.mkdir(parents=True, exist_ok=True)
        if not blob.exists():
            blob.write_bytes(body)
        existing = next((r for r in self.catalog["records"] if r.get("sha256") == digest), None)
        if existing:
            return existing
        sheet, company_en, product_line = parse_library_path(line.get("library_path") or "")
        if product_line == "_":
            product_line = line.get("product_line") or "_"
        if company_en == "_":
            company_en = rule.get("company_en") or rule["company_id"]
        model = guess_model(url, line)
        dtype = doc_type(kind, url)
        collected = date.today().isoformat()
        rel_lib = catalog_relpath(
            rule["company_id"], sheet, company_en, product_line, model,
            dtype, collected, digest, suffix,
        )
        dest = self.root / rel_lib
        if dest.exists():
            rel_lib = catalog_relpath(
                rule["company_id"], sheet, company_en, product_line, model,
                dtype, collected, digest + "x", suffix,
            )
            dest = self.root / rel_lib
        self._link_or_copy(blob, dest)
        record = {
            "sha256": digest,
            "bytes": len(body),
            "kind": kind,
            "doc_type": dtype,
            "company_id": rule["company_id"],
            "company_en": company_en,
            "product_line": line.get("product_line") or product_line,
            "sheet": sheet,
            "model": model,
            "bom_parts": list(line.get("bom_parts") or []),
            "library_path": line.get("library_path") or "",
            "file_path": rel_lib,
            "blob": rel_blob,
            "source_url": url,
            "requested_url": requested,
            "http_status": status,
            "content_type": content_type,
            "collected_date": collected,
            "status": "downloaded",
            "doc_id": None,
            "notes": "local fetchspec capture; not C3 adopted; Spark merge assigns doc_id",
        }
        self.catalog["records"].append(record)
        self._save_catalog()
        return record

    def write_run(self, run_id, payload):
        path = self.runs / f"{run_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path
