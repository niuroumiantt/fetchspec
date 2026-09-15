from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def sha256(body):
    return hashlib.sha256(body).hexdigest()


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.blobs = self.root / "blobs"
        self.library = self.root / "library"
        self.runs = self.root / "runs"
        for path in (self.blobs, self.library, self.runs):
            path.mkdir(parents=True, exist_ok=True)

    def put(self, body, suffix, library_path, filename):
        digest = sha256(body)
        rel = f"blobs/{digest[:2]}/{digest}{suffix}"
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_bytes(body)
        lib_dir = self.root / library_path
        lib_dir.mkdir(parents=True, exist_ok=True)
        pointer = lib_dir / filename
        if pointer.exists() or pointer.is_symlink():
            pointer.unlink()
        try:
            pointer.symlink_to(dest.resolve())
        except OSError:
            pointer.write_bytes(body)
        return digest, rel, str(pointer.relative_to(self.root))

    def write_run(self, run_id, payload):
        path = self.runs / f"{run_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path
