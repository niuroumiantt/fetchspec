#!/usr/bin/env python3
"""仓库架构页（reporg）生成器。

约定（inews.today docs/reporg.md）：每个仓库一页「仓库架构」，回答七个问题：定位、需求、通道与来源、
环节、交付、实时计数、还没做完的。Fetchspec 的页托管在 inresearch.ai/admin/fetchspecrepo.html。

页面从仓库自己的事实生成，手写的只有下面 NARRATIVE 里的几段说明：
  - 需求：inresearch framework/tco_targets.json 里 team == "fetchspec" 的行（--upstream 指向作者 checkout）；
  - 来源：profiles/*.json（每家厂商的官方范围、入口、已知缺口）与 rules/*.json；
  - 环节：src/fetchspec/pipeline.py 的子命令与 help，按四段归类；
  - 图：docs/architecture.svg，原样嵌入。
实时计数（在途、已绑定、已打包、已回执）在采集机的数据根里，不进 Git；页面写明去哪看。

    python3 scripts/reporg.py --upstream ~/code/inresearch.ai          # 写 public/admin/reporg.html
    python3 scripts/reporg.py --upstream ~/code/inresearch.ai --check  # 与仓库里的一致才退出 0
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "public" / "admin" / "reporg.html"
TEAM = "fetchspec"

# 子命令 → 四段。新增子命令不在这里就生成失败（测试也会挡）：每条命令都要归到一段。
STAGES = {
    "① 需求": ["sync-targets", "plan", "coverage"],
    "② 爬取": ["map-sync", "map-ack", "collect", "collect-seeds"],
    "③ 整理": ["migrate", "list", "export", "bind", "map-field"],
    "④ 输出": ["package", "receipt", "assignments", "author-proposal", "catalog"],
    "运维": ["status"],
}

# 手写的只有这些；其余都从仓库文件里读。
NARRATIVE = {
    "position": "六个采集队里的「产品与技术资料」队：按 inresearch 的目标行，从厂商官网取回官方产品规格，保留原件与原表，"
                "整理成可比较的参数，打包交付并拿回回执。收到不等于采用，研究事实由 inresearch 判断。",
    "not_ours": "翻译、研究结论、目标表状态、全站 URL 清单；新闻、财报、研报、报价、统计归其他队（inews、fetchfilings、"
                "fetchreports、fetchquotes、fetchstat）。",
    "hosts": [("macmini", "采集执行机（目标表 host）"), ("M5", "开发、NVIDIA 与计算芯片批次"), ("AWS", "inresearch 接收与展示"),
              ("Spark", "永久归档与深读")],
    "delivery": [
        ("包", "v2.0：manifest.json + SHA256SUMS + files/"),
        ("每项带", "target_ids、part_ids、来源与语言、版本关系、产品身份、厂商原表、人审过的参数观测（值为厂商原文）"),
        ("接收", "inresearch `manage.py fetchspec-receive` 逐文件验 SHA256 与契约，回执 JSON"),
        ("登记", "`assignments` 生成每目标行一条的登记；作者 checkout 跑 `deliveries import`，走 PR 与审阅"),
        ("不写", "Fetchspec 不写 inresearch 的 Git，不改目标表状态"),
    ],
    "open": [
        ("回流", "已向 inresearch 申请按目标行的回流接口（docs/upstream/backflow-request.md）；"
                 "fetchspec 侧 `plan --backflow` 已能读，接口上线前只能看自己的 coverage。"),
        ("实时计数", "在途、已绑定、已打包、已回执在采集机的数据根里，本页不含；在 macmini 上跑 `coverage` 与 `status`。"),
    ],
}


def parser_commands(pipeline_py: Path) -> dict[str, str]:
    """pipeline.py 里的子命令与 help（按出现顺序）。"""
    text = pipeline_py.read_text(encoding="utf-8")
    found = re.findall(r"add_parser\('([a-z-]+)'(?:,\s*help='([^']*)')?", text)
    return {name: help_ for name, help_ in found}


def load_profiles(root: Path) -> list[dict]:
    out = []
    for path in sorted((root / "profiles").glob("*.json")):
        p = json.loads(path.read_text(encoding="utf-8"))
        out.append({
            "company": p.get("company_en") or p.get("company_id") or path.stem,
            "id": p.get("company_id") or path.stem,
            "status": p.get("status", ""),
            "hosts": p.get("allowed_hosts", []),
            "entrypoints": len(p.get("discovery_entrypoints", [])) + len(p.get("sitemaps", [])),
            "formats": p.get("document_formats", []),
            "target_parts": p.get("target_parts", []),
            "gaps": p.get("known_gaps", []),
        })
    return out


def load_rules(root: Path) -> list[str]:
    return sorted(path.stem for path in (root / "rules").glob("*.json"))


TARGETS_PATH = "framework/tco_targets.json"


def _git_last_change(path: Path, relative: str) -> str | None:
    """最后一次改动该文件的提交，而不是 HEAD：inresearch 每次无关提交都不应让本页「过期」。"""
    try:
        commit = subprocess.run(["git", "-C", str(path), "log", "-1", "--format=%H", "--", relative],
                                capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return commit or None


def load_targets(upstream: Path | None) -> dict | None:
    """team == fetchspec 的目标行：按类别（spec / operation / 因子）与部件计数，带来源 SHA。"""
    if upstream is None:
        return None
    path = upstream / TARGETS_PATH
    raw = path.read_bytes()
    doc = json.loads(raw)
    rows = doc.get("rows") or doc.get("targets") or doc
    mine = [r for r in rows if isinstance(r, dict) and r.get("team") == TEAM]
    kinds = Counter()
    parts = Counter()
    for r in mine:
        rid = str(r.get("id") or r.get("target_id") or "")
        if rid.startswith("F."):
            kinds["因子 F.*"] += 1
        elif rid.endswith(".spec"):
            kinds["构成 P.*.spec"] += 1
        elif rid.endswith(".operation"):
            kinds["运行 P.*.operation"] += 1
        else:
            kinds["其他"] += 1
        m = re.match(r"P\.([^.]+)\.", rid)
        if m:
            parts[m.group(1)] += 1
    teams = Counter(r.get("team") for r in rows if isinstance(r, dict))
    return {"rows": len(mine), "kinds": dict(kinds), "parts": len(parts), "teams": dict(sorted(teams.items(), key=lambda kv: -kv[1])),
            "commit": _git_last_change(upstream, TARGETS_PATH), "sha256": hashlib.sha256(raw).hexdigest()[:16]}


def build(root: Path = ROOT, upstream: Path | None = None) -> str:
    commands = parser_commands(root / "src" / "fetchspec" / "pipeline.py")
    staged = [c for names in STAGES.values() for c in names]
    missing = [c for c in commands if c not in staged]
    if missing:
        raise SystemExit(f"子命令没有归到任何一段：{', '.join(missing)}（改 scripts/reporg.py 的 STAGES）")
    profiles = load_profiles(root)
    rules = load_rules(root)
    targets = load_targets(upstream)
    svg_path = root / "docs" / "architecture.svg"
    svg = svg_path.read_text(encoding="utf-8") if svg_path.exists() else ""
    svg = re.sub(r"<\?xml[^>]*\?>", "", svg).strip()
    return render(commands, profiles, rules, targets, svg)


def _table(headers, rows):
    th = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def _code(text):
    return re.sub(r"`([^`]+)`", lambda m: f"<code>{escape(m.group(1))}</code>", escape(text).replace("&#x60;", "`"))


def render(commands, profiles, rules, targets, svg) -> str:
    sections = []
    sections.append(("定位", f"<p>{escape(NARRATIVE['position'])}</p><p class=\"note\">不给什么：{escape(NARRATIVE['not_ours'])}</p>"))

    if targets:
        kinds = _table(["类别", "行数"], [[escape(k), str(v)] for k, v in sorted(targets["kinds"].items())])
        teams = " · ".join(f"{escape(t)} {n}" for t, n in targets["teams"].items())
        src = f"inresearch <code>framework/tco_targets.json</code> · 最后改动 commit <code>{escape((targets['commit'] or '未知')[:12])}</code> · sha256 <code>{targets['sha256']}</code>"
        demand = (f"<p>领 <b>{targets['rows']}</b> 行（涉及 {targets['parts']} 个部件），只领构成与运行两类变量。</p>{kinds}"
                  f"<p class=\"note\">六个队的目标行：{teams}</p><p class=\"note\">来源：{src}</p>")
    else:
        demand = "<p class=\"note\">生成时没有给 <code>--upstream</code>，目标行计数缺省。</p>"
    sections.append(("需求", demand))

    prow = [[f"<b>{escape(p['company'])}</b><br><span class=\"note\">{escape(p['status'])}</span>",
             "<br>".join(f"<code>{escape(h)}</code>" for h in p["hosts"]),
             str(p["entrypoints"]),
             escape(" · ".join(p["formats"])),
             escape(" · ".join(p["target_parts"])),
             (f'<a href="#s6">{len(p["gaps"])} 条</a>' if p["gaps"] else "—")] for p in profiles]
    channels = (f"<p>通道只有一种：<b>直连</b>，只取厂商官方站点，按 robots、节流、条件请求与内容 SHA 采集。"
                f"{len(profiles)} 家厂商有适配档案，{len(rules)} 家有采集规则（{escape('、'.join(rules))}）。</p>"
                + _table(["厂商", "允许的主机", "入口数", "文档格式", "目标部件", "已知缺口"], prow)
                + "<p class=\"note\">执行机：" + " · ".join(f"<b>{escape(h)}</b> {escape(w)}" for h, w in NARRATIVE["hosts"]) + "</p>")
    sections.append(("通道与来源", channels))

    srows = []
    for stage, names in STAGES.items():
        for i, name in enumerate(n for n in names if n in commands):
            srows.append([escape(stage) if i == 0 else "", f"<code>{escape(name)}</code>", escape(commands[name] or "")])
    flow = (f'<figure class="svg">{svg}</figure>' if svg else "") + _table(["段", "命令", "做什么"], srows)
    flow += "<p class=\"note\">所有命令：<code>PYTHONPATH=src python3 -m fetchspec.pipeline [--root 数据根] &lt;命令&gt;</code></p>"
    sections.append(("环节", flow))

    sections.append(("交付", _table(["项", "内容"], [[escape(k), _code(v)] for k, v in NARRATIVE["delivery"]])))
    sections.append(("实时计数", "<p>在采集机（macmini）上：</p><pre>PYTHONPATH=src python3 -m fetchspec.pipeline coverage\nPYTHONPATH=src python3 -m fetchspec.pipeline status</pre>"
                                 "<p class=\"note\">数据根 <code>~/.local/share/fetchspec/pipeline/</code>，原件、数据库、目标快照、包与回执都不进 Git。</p>"))
    gaps = [[f"<b>{escape(p['company'])}</b>", escape(str(g))] for p in profiles for g in p["gaps"]]
    open_rows = [[escape(k), _code(v)] for k, v in NARRATIVE["open"]] + gaps
    sections.append(("还没做完的", _table(["项", "说明"], open_rows)))

    toc = "".join(f'<a href="#s{i}">{escape(t)}</a>' for i, (t, _) in enumerate(sections))
    body = "".join(f'<section id="s{i}"><h2>{escape(t)}</h2>{h}</section>' for i, (t, h) in enumerate(sections))
    # 不写 fetchspec 自己的 commit:那样每次提交页面都「过期」,--check 永远不过。
    return PAGE.format(toc=toc, body=body)


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>仓库架构 · fetchspec</title>
<meta name="description" content="Fetchspec 的仓库架构页：定位、需求、来源、环节、交付、实时计数、未完成。由 scripts/reporg.py 生成。">
<style>
:root{{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e5e5ea;--soft:#f5f5f7;--accent:#2f6fde}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{--bg:#111113;--fg:#ececf0;--muted:#9a9aa3;--line:#2a2a30;--soft:#1a1a1e;--accent:#6b9cff}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans SC",sans-serif}}
header{{position:sticky;top:0;z-index:2;display:flex;gap:18px;align-items:center;flex-wrap:wrap;padding:12px 28px;border-bottom:1px solid var(--line);background:var(--bg)}}
header b{{font-size:14px}}header nav{{display:flex;gap:14px;flex-wrap:wrap;font-size:13px}}header nav a{{color:var(--muted);text-decoration:none}}header nav a:hover{{color:var(--accent)}}
main{{max-width:1080px;margin:0 auto;padding:28px 28px 64px}}.eyebrow{{color:var(--muted);font-size:13px;margin:0}}h1{{font-size:28px;margin:6px 0 10px}}
h2{{font-size:20px;margin:36px 0 12px;padding-top:12px;border-top:1px solid var(--line)}}.note{{color:var(--muted);font-size:13px}}
code{{font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--soft);padding:1px 5px;border-radius:4px}}pre{{background:var(--soft);padding:12px 14px;border-radius:8px;overflow:auto;font-size:12.5px}}
.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13.5px;margin:10px 0}}th,td{{text-align:left;vertical-align:top;padding:8px 10px;border-bottom:1px solid var(--line)}}th{{color:var(--muted);font-weight:500;white-space:nowrap}}
figure.svg{{margin:12px 0 18px;padding:12px;border:1px solid var(--line);border-radius:10px;overflow-x:auto}}figure.svg svg{{max-width:100%;height:auto}}
@media (max-width:640px){{header{{padding:10px 16px}}main{{padding:20px 16px 48px}}h1{{font-size:23px}}}}
</style>
</head>
<body>
<header><b>仓库架构 · fetchspec</b><nav>{toc}</nav></header>
<main>
<p class="eyebrow">inresearch.ai/admin/fetchspecrepo.html</p>
<h1>需求 → 爬取 → 整理 → 输出</h1>
<p>这一页从 fetchspec 仓库自己的文件生成（<code>scripts/reporg.py</code>）：子命令、厂商档案与规则、架构图，以及 inresearch 目标表里 team = fetchspec 的行。改了这些，重新生成即可。</p>
{body}
</main>
</body>
</html>
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--upstream", type=Path, help="inresearch.ai 作者 checkout（读 framework/tco_targets.json）")
    ap.add_argument("--output", type=Path, default=OUTPUT)
    ap.add_argument("--check", action="store_true", help="与现有文件一致才退出 0，不写")
    args = ap.parse_args(argv)
    upstream = args.upstream.expanduser().resolve() if args.upstream else None
    html = build(ROOT, upstream)
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != html:
            print(f"{args.output} 过期：重跑 python3 scripts/reporg.py --upstream <inresearch checkout>", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(f"写入 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
