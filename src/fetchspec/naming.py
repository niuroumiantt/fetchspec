"""Path names aligned with inresearch.materials.library (safe / norm2 / destination)."""

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


def safe(component):
    return re.sub(r"[/:\x00]", "-", (component or "").strip()) or "_"


def norm2(s):
    return re.sub(r"^-|-$", "", re.sub(r"[^a-z0-9]+", "-", (s or "").lower()))


def parse_library_path(library_path):
    parts = [p for p in (library_path or "").strip("/").split("/") if p]
    if parts and parts[0] == "library":
        parts = parts[1:]
    sheet = parts[0] if parts else "_"
    company_en = parts[1] if len(parts) > 1 else "_"
    product_line = parts[2] if len(parts) > 2 else "_"
    return sheet, company_en, product_line


def doc_type(kind, url):
    blob = (url or "").lower()
    if kind == "html":
        return "WEB"
    if "brief" in blob:
        return "PB"
    if "brochure" in blob or "/br-" in blob:
        return "BR"
    if "whitepaper" in blob or "white-paper" in blob or "/wp-" in blob:
        return "WP"
    if "manual" in blob or "user-guide" in blob:
        return "UM"
    return "DS"


def guess_model(url, line):
    blob = unquote(urlsplit(url).path).lower()
    hints = [h.lower() for h in (line.get("path_hints") or [])]
    ranked = sorted((h for h in hints if h in blob and re.search(r"[a-z0-9]", h)), key=len, reverse=True)
    for hint in ranked:
        if hint not in {"gpu", "cpu", "datasheet", "pdf", "html", "en-us", "data-center"}:
            return hint
    stem = Path(urlsplit(url).path.rstrip("/")).stem
    if stem and re.search(r"[a-z0-9]{2}", stem, re.I) and len(stem) < 40:
        return stem
    return "line"


def catalog_relpath(company_id, sheet, company_en, product_line, model, doc_type_code, collected_date, digest, suffix):
    directory = Path("library") / safe(sheet) / safe(company_en) / safe(product_line) / safe(model)
    name = "__".join([
        company_id,
        norm2(model) or "line",
        doc_type_code,
        "vNA",
        collected_date,
        "en",
        digest[:8],
    ]) + suffix
    return (directory / name).as_posix()
