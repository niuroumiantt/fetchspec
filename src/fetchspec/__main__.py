import argparse
import json
import sys
from pathlib import Path

from .catalog import ROOT, load_rule, load_rules
from .crawl import crawl
from .store import default_data_root


def main(argv=None):
    parser = argparse.ArgumentParser(description="Declarative official-site spec fetch for inresearch")
    parser.add_argument("command", choices=["list", "validate", "crawl", "where", "inventory", "company-crawl", "company-status"])
    parser.add_argument("--company", default="supermicro", help="Company inventory profile (not a legacy demo rule)")
    parser.add_argument("--rule", help="Path or rule_id (filename stem)")
    parser.add_argument("--demo", action="store_true", help="Only rules marked demo=true")
    parser.add_argument("--out", default=None, help="Archive root; default ~/.local/share/fetchspec")
    parser.add_argument("--fetch", action="store_true", help="Download bodies; default is dry-run")
    parser.add_argument("--manifest", help="Previously saved company sitemap urls.jsonl")
    parser.add_argument("--max-requests", type=int, default=0, help="Company worker request budget; 0 drains the persistent frontier")
    parser.add_argument("--recheck", action="store_true", help="Requeue known company URLs for conditional checks, retaining versions")
    parser.add_argument("--retry-errors", action="store_true", help="Requeue failed requests whose error is transient (timeouts, connection, TLS, 5xx); 404 and robots/allowlist blocks stay")
    parser.add_argument("--force", action="store_true", help="With --recheck, omit conditional headers for a full content audit")
    parser.add_argument("--summary", action="store_true", help="company-status: short operator summary with pace and ETA")
    parser.add_argument("--json", action="store_true", help="company-status --summary: emit JSON instead of text")
    args = parser.parse_args(argv)

    if args.command in {"company-crawl", "company-status"}:
        from .company import CompanyLedger, format_summary, progress_summary, run_company
        from .inventory import load_profile, utc_now
        profile = load_profile(args.company)
        root = Path(args.out or default_data_root()).expanduser()
        if args.command == "company-status":
            ledger = CompanyLedger(root, profile)
            if args.summary:
                summary = progress_summary(ledger)
                print(json.dumps(summary, ensure_ascii=False, indent=2) if args.json else format_summary(summary))
            else:
                print(json.dumps(ledger.report(), ensure_ascii=False, indent=2))
            ledger.db.close()
            return 0
        if not args.fetch:
            parser.error("company-crawl requires --fetch; use inventory for metadata-only discovery")
        if args.force and not args.recheck:
            parser.error("--force requires --recheck")
        if args.retry_errors and args.recheck:
            parser.error("--retry-errors and --recheck are exclusive; --recheck already requeues everything")
        if args.max_requests < 0:
            parser.error("--max-requests must be nonnegative")
        result = run_company(profile, root, manifest=args.manifest, max_requests=args.max_requests,
                             recheck=args.recheck, force=args.force, retry_errors=args.retry_errors,
                             progress=lambda row: print(json.dumps({"ts": utc_now(), **row}, ensure_ascii=False), flush=True))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        # An operator STOP is a clean exit so launchd KeepAlive does not relaunch it.
        return 0 if result["run"]["status"] in {"frontier_exhausted_with_gaps", "paused_stop_file", "paused_low_yield"} else 2

    if args.command == "inventory":
        from .inventory import load_profile, run_inventory
        if args.fetch or args.rule or args.demo:
            parser.error("inventory only fetches sitemap metadata; use --company, not --fetch/--rule/--demo")
        result = run_inventory(load_profile(args.company), Path(args.out or default_data_root()).expanduser(),
                               progress=lambda row: print(json.dumps({"sitemap": row["role"], "entries": row["entries"]}), flush=True))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["errors"] else 0

    if args.command == "where":
        print(default_data_root())
        return 0

    if args.command == "list":
        rules = load_rules(demo_only=args.demo)
        print(json.dumps([
            {
                "rule_id": r["rule_id"],
                "company_id": r["company_id"],
                "ecosystems": r["ecosystems"],
                "demo": r["demo"],
                "hosts": r["allowed_hosts"],
                "start_urls": r["start_urls"],
            }
            for r in rules
        ], ensure_ascii=False, indent=2))
        return 0

    if args.command == "validate":
        rules = load_rules(demo_only=args.demo) if not args.rule else [load_one(args.rule)]
        print(json.dumps({"ok": True, "rules": [r["rule_id"] for r in rules]}, indent=2))
        return 0

    rules = load_rules(demo_only=True if args.demo and not args.rule else False)
    if args.rule:
        rules = [load_one(args.rule)]
    elif args.demo:
        rules = load_rules(demo_only=True)
    out = args.out or str(default_data_root())
    results = []
    for rule in rules:
        results.append(crawl(rule, out, dry_run=not args.fetch))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(not r["errors"] for r in results) else 1


def load_one(name):
    path = Path(name)
    if path.exists():
        return load_rule(path)
    match = ROOT / "rules" / f"{name}.json"
    if match.exists():
        return load_rule(match)
    for rule in load_rules():
        if rule["rule_id"] == name or rule["company_id"] == name:
            return rule
    raise SystemExit(f"unknown rule: {name}")


if __name__ == "__main__":
    sys.exit(main())
