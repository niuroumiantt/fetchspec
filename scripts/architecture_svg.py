#!/usr/bin/env python3
"""Draw docs/architecture.svg: demand -> crawl -> organize -> output (2026-09-30).

    python3 scripts/architecture_svg.py > docs/architecture.svg

Edit the boxes here together with docs/ARCHITECTURE.md and the framework page.
The standalone SVG follows the viewer's light/dark preference.
"""
import html
import sys

W, H = 1600, 712
COLW, GAP = 250, 22
LEFT_X, LEFT_W = 20, 220
STAGE_X = [262 + i * (COLW + GAP) for i in range(4)]
RIGHT_X, RIGHT_W = STAGE_X[3] + COLW + GAP, 230
HEAD_Y, HEAD_H = 104, 58
BOX_Y = [176, 266, 356]
BOX_H = 76
STORE_Y, STORE_H = 452, 76
MID = lambda y: y + BOX_H // 2


def esc(s):
    return html.escape(s, quote=True)


def box(x, y, w, h, title, lines, cls='box', num=None):
    out = [f'<rect class="{cls}" x="{x}" y="{y}" width="{w}" height="{h}" rx="6"/>']
    tx = x + 12
    if num:
        out.append(f'<circle class="num" cx="{x + 18}" cy="{y + 18}" r="11"/>')
        out.append(f'<text class="numt" x="{x + 18}" y="{y + 22}" text-anchor="middle">{esc(num)}</text>')
        tx = x + 36
    out.append(f'<text class="t1" x="{tx}" y="{y + 22}">{esc(title)}</text>')
    for i, line in enumerate(lines):
        out.append(f'<text class="t2" x="{x + 12}" y="{y + 44 + i * 18}">{esc(line)}</text>')
    return '\n'.join(out)


def head(x, w, num, title, sub, cls='head'):
    return box(x, HEAD_Y, w, HEAD_H, title, [sub], cls=cls, num=num)


def line(x1, y1, x2, y2, cls='edge'):
    return f'<line class="{cls}" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" marker-end="url(#ah)"/>'


def path(points, cls='edge'):
    d = 'M' + ' L'.join(f'{x} {y}' for x, y in points)
    return f'<path class="{cls}" d="{d}" fill="none" marker-end="url(#ah)"/>'


def label(x, y, text, anchor='middle', cls='lbl'):
    return f'<text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}">{esc(text)}</text>'


STAGES = [
    ('1', '需求', '目标行 → 采集计划', [
        ('sync-targets', ['只读 Git 快照：commit + SHA', '契约 1.5+ 1.x；越队与漂移拒绝']),
        ('plan · coverage', ['下一步队列：在途 / 可抓 / 复核 / 缺适配器', '按 next_due 与敏感度；附参数提示']),
        ('适配器声明 profiles/*.json', ['target_parts：服务哪些部件', 'instance_aliases：对上目标行实例']),
    ], ['targets/snapshots/<id>/', 'targets/current.json']),
    ('2', '爬取', '官方来源 → 不可变字节', [
        ('map-sync · map-ack', ['官方产品 sitemap 只给候选', 'lastmod 变化待人工确认']),
        ('collect', ['种子 + 请求预算；robots · 节流', 'ETag / Last-Modified + 内容 SHA']),
        ('四个通道', ['产品页 · 文档 · 浏览器 · 人工', '一个来源只属一个通道一台机']),
    ], ['blobs/<sha>（只增不改）', 'acquisition/<公司>/sources.sqlite3']),
    ('3', '整理', '原表原值 → 参数观测', [
        ('原生提取', ['HTML 表 → 原生 PDF → OOXML', '不执行脚本；不用 OCR 猜值']),
        ('候选库', ['公司 → 分类 → 系列 → 型号', '原表 · 单元格 · 版本 · 变化']),
        ('bind · map-field', ['产品 ↔ 目标行，写明理由', '单元格 → 参数：原文 · 单位 · 条件']),
    ], ['products/catalog.sqlite3', 'export → CSV 供人抽查']),
    ('4', '输出', '一个包 · 一张回执 · 一份登记', [
        ('package', ['v2.0 manifest · SHA256SUMS · files/', 'target_ids · 原表 · 参数观测']),
        ('receipt', ['接收端回执按环境入账', 'packaged ≠ received']),
        ('assignments', ['deliveries import 的输入', '每目标行一条 · 官方 URL']),
    ], ['deliveries/<delivery_id>/', 'delivery-ledger.sqlite']),
]


def body():
    o = []
    o.append(f'<text class="title" x="20" y="34">Fetchspec · 需求 → 爬取 → 整理 → 输出（2026-09-30）</text>')
    o.append(f'<text class="sub" x="20" y="56">inresearch.ai 六队之一，厂商规格队。只领 team = fetchspec 的目标行；只收英文中文官方来源；原值不改；目标是需求不是证据。实线已实现，虚线未接或需人工。</text>')

    # left: demand source
    o.append(head(LEFT_X, LEFT_W, None, 'inresearch.ai · 需求源', 'Git 权威，Fetchspec 只读', cls='up'))
    left = [('tco_targets.json', ['team = fetchspec 130 行', '部件 × 数据类别 × 出版方']),
            ('supply_contract.json', ['1.6 · 生成目标契约 2.0', 'parameter_observation_fields']),
            ('part_fetch.json', ['每个部件的出版方实例', '人工登记 → 目标行 instances'])]
    for (t, ls), y in zip(left, BOX_Y):
        o.append(box(LEFT_X, y, LEFT_W, BOX_H, t, ls, cls='box up'))
    o.append(box(LEFT_X, STORE_Y, LEFT_W, STORE_H, '状态四态', ['needed → delivered → sourced', 'delivered 只认 Git 内载体'], cls='store up'))

    # stages
    for x, (num, name, sub, boxes, store) in zip(STAGE_X, STAGES):
        o.append(head(x, COLW, num, name, sub))
        for (t, ls), y in zip(boxes, BOX_Y):
            o.append(box(x, y, COLW, BOX_H, t, ls, cls='box step'))
        o.append(box(x, STORE_Y, COLW, STORE_H, '落盘', store, cls='store'))

    # right: receiving side
    o.append(head(RIGHT_X, RIGHT_W, None, 'inresearch.ai · 接收与采用', '运行库与作者 checkout', cls='up'))
    right = [('fetchspec-receive', ['逐文件 SHA；目标须属 fetchspec', '参数观测：只归档，未入库']),
             ('研究采用', ['Reader 深读 → 证据 → C3', 'delivered → sourced']),
             ('deliveries import', ['作者 checkout → event_cards', '重跑目标表 · PR · 审阅'])]
    for (t, ls), y in zip(right, BOX_Y):
        o.append(box(RIGHT_X, y, RIGHT_W, BOX_H, t, ls, cls='box up'))
    o.append(box(RIGHT_X, STORE_Y, RIGHT_W, STORE_H, '目标行翻状态', ['needed → delivered（逐行）', '补丁待合：docs/upstream/'], cls='store up'))

    # stage-to-stage
    for i in range(3):
        o.append(line(STAGE_X[i] + COLW, HEAD_Y + HEAD_H // 2, STAGE_X[i + 1] - 3, HEAD_Y + HEAD_H // 2, 'edge strong'))
    lx = LEFT_X + LEFT_W
    s1, s2, s3, s4 = STAGE_X
    # demand source -> sync / plan
    o.append(line(lx, MID(BOX_Y[0]), s1 - 3, MID(BOX_Y[0])))
    o.append(path([(lx, MID(BOX_Y[1])), (lx + 11, MID(BOX_Y[1])), (lx + 11, MID(BOX_Y[0]) + 14), (s1 - 3, MID(BOX_Y[0]) + 14)]))
    o.append(path([(lx, MID(BOX_Y[2])), (lx + 11, MID(BOX_Y[2])), (lx + 11, MID(BOX_Y[1]) + 10), (s1 - 3, MID(BOX_Y[1]) + 10)]))
    # within stages
    for x in STAGE_X:
        for y in BOX_Y[:2]:
            o.append(line(x + COLW // 2, y + BOX_H, x + COLW // 2, y + BOX_H + 12))
    # plan -> collect
    o.append(line(s1 + COLW, MID(BOX_Y[1]), s2 - 3, MID(BOX_Y[1])))
    # collect -> extraction
    o.append(path([(s2 + COLW, MID(BOX_Y[1]) + 12), (s2 + COLW + 11, MID(BOX_Y[1]) + 12), (s2 + COLW + 11, MID(BOX_Y[0])), (s3 - 3, MID(BOX_Y[0]))]))
    # bind/map-field -> package
    o.append(path([(s3 + COLW, MID(BOX_Y[2])), (s3 + COLW + 11, MID(BOX_Y[2])), (s3 + COLW + 11, MID(BOX_Y[0])), (s4 - 3, MID(BOX_Y[0]))]))
    # package -> receive
    rx = RIGHT_X
    o.append(line(s4 + COLW, MID(BOX_Y[0]) - 10, rx - 3, MID(BOX_Y[0]) - 10))
    # receive -> receipt (back)
    o.append(path([(rx, MID(BOX_Y[0]) + 12), (rx - 11, MID(BOX_Y[0]) + 12), (rx - 11, MID(BOX_Y[1])), (s4 + COLW + 3, MID(BOX_Y[1]))]))
    # receive -> research (dashed, explicit SHA only)
    o.append(line(rx + RIGHT_W // 2, BOX_Y[0] + BOX_H, rx + RIGHT_W // 2, BOX_Y[1] - 3, 'edge dashed'))
    # assignments -> deliveries import
    o.append(line(s4 + COLW, MID(BOX_Y[2]), rx - 3, MID(BOX_Y[2])))
    # deliveries import -> status
    o.append(line(rx + RIGHT_W // 2, BOX_Y[2] + BOX_H, rx + RIGHT_W // 2, STORE_Y - 3, 'edge accent'))
    # loop back to demand
    top = 80
    o.append(path([(rx + RIGHT_W, STORE_Y + STORE_H // 2), (W - 8, STORE_Y + STORE_H // 2), (W - 8, top),
                   (LEFT_X + LEFT_W // 2, top), (LEFT_X + LEFT_W // 2, HEAD_Y - 3)], 'edge accent'))
    o.append(label(W // 2, top - 6, '目标行状态回写 inresearch 的 Git，成为下一轮需求', cls='lbl accent'))
    # reflow (dashed, not connected)
    ry = STORE_Y + STORE_H + 18
    o.append(path([(rx + RIGHT_W // 2, STORE_Y + STORE_H), (rx + RIGHT_W // 2, ry), (s1 + COLW // 2, ry), (s1 + COLW // 2, STORE_Y + STORE_H + 3)], 'edge dashed'))
    o.append(label((s1 + s4) / 2 + COLW // 2, ry - 5, '回流（未接）：哪些行仍缺、缺哪类出版方；本地先用 plan / coverage 顶上'))

    # machines
    my = 590
    o.append(f'<text class="lanet" x="20" y="{my}">在哪台机器上跑</text>')
    machines = [('macmini', ['采集执行机：目标表 host = macmini', '原件与台账 ~/.local/share/fetchspec']),
                ('M5', ['开发与 NVIDIA 批次', '可完整跑十个命令；装有历史目录']),
                ('AWS', ['inresearch 网站与接收端', '运行库回执；不放规格原件长期档']),
                ('Spark', ['永久原件归档与 Reader 深读', '不执行采集'])]
    mw = (W - 40 - 3 * 22) / 4
    for i, (name, ls) in enumerate(machines):
        o.append(box(20 + i * (mw + 22), my + 12, mw, 70, name, ls, cls='box machine'))
    o.append(f'<text class="sub" x="20" y="{H - 14}">命令都是 python3 -m fetchspec.pipeline &lt;命令&gt;。数据根 ~/.local/share/fetchspec/pipeline/；源码进 Git，原件、数据库、快照、包与回执都不进 Git。</text>'.replace('&amp;lt;', '&lt;'))
    return '\n'.join(o)


FONT = "'PingFang SC','Noto Sans CJK SC','Noto Sans SC',system-ui,-apple-system,sans-serif"
MONO = "ui-monospace,'SF Mono',Menlo,Consolas,'PingFang SC',monospace"
TOKENS_LIGHT = "--bg:#fbfaf7;--fg:#1f2a24;--mute:#5b6b63;--box:#ffffff;--bd:#7c8a82;--step:#eef4f1;--head:#dcebe4;--up:#f4f0e4;--store:#f7f6f1;--machine:#eef0f5;--accent:#1e6f5c"
TOKENS_DARK = "--bg:#161a18;--fg:#e6e9e4;--mute:#a5b0aa;--box:#262d29;--bd:#8b9992;--step:#233029;--head:#1f3a31;--up:#2c2a21;--store:#1d2220;--machine:#23262e;--accent:#6fc7ac"
CLASSES = f"""
  .bg{{fill:var(--bg)}}
  .title{{fill:var(--fg);font:700 18px {FONT}}} .sub{{fill:var(--mute);font:12px {FONT}}}
  .lanet{{fill:var(--fg);font:600 13px {FONT}}}
  .box{{fill:var(--box);stroke:var(--bd);stroke-width:1.2}} .box.step{{fill:var(--step)}} .box.up{{fill:var(--up)}} .box.machine{{fill:var(--machine)}}
  .head{{fill:var(--head);stroke:var(--accent);stroke-width:1.4}} .up{{fill:var(--up);stroke:var(--bd);stroke-width:1.2}}
  .store{{fill:var(--store);stroke:var(--bd);stroke-width:1.1;stroke-dasharray:4 3}}
  .t1{{fill:var(--fg);font:600 13px {FONT}}} .t2{{fill:var(--mute);font:11.5px {FONT}}}
  .num{{fill:var(--accent)}} .numt{{fill:var(--bg);font:700 12px {FONT}}}
  .edge{{stroke:var(--fg);stroke-width:1.3;fill:none}} .edge.strong{{stroke-width:2}} .edge.dashed{{stroke-dasharray:5 4;stroke:var(--mute)}}
  .edge.accent{{stroke:var(--accent);stroke-width:1.8}}
  .lbl{{fill:var(--mute);font:11px {FONT}}} .lbl.accent{{fill:var(--accent);font-weight:600}}
  #ah path{{fill:var(--fg)}}
"""
DEFS = '<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z"/></marker></defs>'
ARIA = ('Fetchspec 架构：inresearch 的目标表、供应契约和出版方登记只读同步进来；Fetchspec 分四段——需求（快照与采集计划）、'
        '爬取（官方来源到不可变字节）、整理（原表原值到参数观测与目标绑定）、输出（包、回执、登记）；包交给 inresearch 接收，'
        '回执回到本地台账，登记经作者 deliveries import 把目标行翻成 delivered，并回到下一轮需求。')


def standalone():
    style = (f'<style>:root{{{TOKENS_LIGHT}}} @media (prefers-color-scheme: dark){{:root{{{TOKENS_DARK}}}}}{CLASSES}</style>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="{esc(ARIA)}">'
            f'<title>Fetchspec 架构（2026-09-30）：需求 → 爬取 → 整理 → 输出</title>{style}{DEFS}'
            f'<rect class="bg" x="0" y="0" width="{W}" height="{H}"/>{body()}</svg>\n')


def inline():
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img" aria-label="{esc(ARIA)}" class="arch">'
            f'{DEFS}{body()}</svg>')


if __name__ == '__main__':
    sys.stdout.write(inline() if sys.argv[1:] == ['--inline'] else standalone())
