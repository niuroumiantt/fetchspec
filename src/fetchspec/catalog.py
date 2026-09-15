from pathlib import Path
import json
import re

ROOT = Path(__file__).resolve().parents[2]
RULES_DIR = ROOT / "rules"
SCHEMA_PATH = ROOT / "schema" / "rule.schema.json"

REQUIRED = (
    "rule_id", "company_id", "company_en", "ecosystems",
    "allowed_hosts", "start_urls", "path_include", "product_lines",
)


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def compile_rule(raw):
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise ValueError("missing_fields:" + ",".join(missing))
    if not raw["allowed_hosts"] or not raw["start_urls"] or not raw["product_lines"]:
        raise ValueError("empty_required_list")
    limits = {
        "delay_seconds": 1.5,
        "max_pages": 10,
        "max_assets": 6,
        "max_depth": 2,
        "max_bytes": 20 * 1024 * 1024,
    }
    limits.update(raw.get("limits") or {})
    if limits["delay_seconds"] < 0.5:
        raise ValueError("delay_too_small")
    return {
        **raw,
        "path_exclude": list(raw.get("path_exclude") or []),
        "link_text_include": list(raw.get("link_text_include") or []),
        "asset_types": list(raw.get("asset_types") or ["pdf", "html"]),
        "save_html": bool(raw.get("save_html", True)),
        "demo": bool(raw.get("demo", False)),
        "limits": limits,
        "_include": [re.compile(p, re.I) for p in raw["path_include"]],
        "_exclude": [re.compile(p, re.I) for p in (raw.get("path_exclude") or [])],
    }


def load_rule(path):
    return compile_rule(load_json(path))


def load_rules(directory=None, demo_only=False):
    directory = Path(directory or RULES_DIR)
    rules = []
    for path in sorted(directory.glob("*.json")):
        rule = load_rule(path)
        rule["_path"] = str(path)
        if demo_only and not rule["demo"]:
            continue
        rules.append(rule)
    if not rules:
        raise ValueError("no_rules")
    return rules
