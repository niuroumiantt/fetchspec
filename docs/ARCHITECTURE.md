# Fetchspec 架构（2026-09-30）

一条主线：**需求 → 爬取 → 整理 → 输出**。左边是 inresearch 的需求源，右边是 inresearch 的接收与采用，目标行状态回写 inresearch 的 Git 后成为下一轮需求。为什么这样分、每段的边界是什么，见 [框架](framework-2026-09-30.md)。

![Fetchspec 架构：需求 → 爬取 → 整理 → 输出](architecture.svg)

图的约定与 infra 相同：实线是已经实现的路径，虚线是未接通或需要人工的路径。SVG 跟随系统明暗主题；下面的 Mermaid 版是同一份事实，方便在 GitHub 上直接看。

## 1. 四段主线

```mermaid
flowchart LR
  subgraph UP["inresearch.ai · 需求源（Git，只读）"]
    TT["tco_targets.json<br/>team = fetchspec 130 行"]
    SC["supply_contract.json<br/>1.6 · 生成目标契约 2.0"]
    PF["part_fetch.json<br/>出版方实例"]
  end

  subgraph S1["① 需求"]
    SYNC["sync-targets<br/>快照 commit + SHA"]
    PLAN["plan · coverage<br/>下一步队列 + 参数提示"]
    PROF["profiles/*.json<br/>target_parts · instance_aliases"]
  end

  subgraph S2["② 爬取"]
    MAP["map-sync · map-ack<br/>sitemap 候选"]
    COL["collect<br/>种子 + 预算 · robots · ETag · SHA"]
  end

  subgraph S3["③ 整理"]
    EXT["原生提取<br/>HTML 表 → PDF → OOXML"]
    CAT["候选库<br/>身份图 · 原表 · 版本"]
    BIND["bind · map-field<br/>目标绑定 · 参数观测"]
  end

  subgraph S4["④ 输出"]
    PKG["package<br/>v2.0 包"]
    RCP["receipt<br/>按环境入账"]
    ASG["assignments<br/>deliveries import 输入"]
  end

  subgraph DOWN["inresearch.ai · 接收与采用"]
    RCV["fetchspec-receive<br/>运行库"]
    IMP["deliveries import<br/>作者 checkout · PR · 审阅"]
    ADO["Reader → 证据 → C3<br/>delivered → sourced"]
  end

  TT & SC --> SYNC
  PF --> TT
  SYNC --> PLAN
  PROF --> PLAN
  PLAN -->|"种子与目标 ID"| COL
  MAP --> COL
  COL -->|"字节 SHA"| EXT --> CAT --> BIND
  BIND -->|"显式绑定 + 参数"| PKG
  PKG -->|"rsync 包目录"| RCV
  RCV -->|"回执 JSON"| RCP --> ASG
  ASG --> IMP -->|"事件卡 → 目标行 delivered"| TT
  RCV -.->|"显式选定 SHA"| ADO
  ADO -.->|"回流（未接）"| PLAN
```

## 2. 每段落在哪里

| 段 | 命令 | 模块 | 落盘（数据根 `~/.local/share/fetchspec/pipeline/`） |
|---|---|---|---|
| ① 需求 | `sync-targets` · `plan` · `coverage` | `targets.py` · `coverage.py` · `profiles/*.json` | `targets/snapshots/<id>/`、`targets/current.json` |
| ② 爬取 | `map-sync` · `map-ack` · `collect` | `product_map.py` · `acquisition.py` · `network.py` · `adapters/` | `blobs/<sha>`、`acquisition/<公司>/sources.sqlite3`、`scopes/` |
| ③ 整理 | `list` · `export` · `bind` · `map-field` · `migrate` | `extraction.py` · `products.py` | `products/catalog.sqlite3`、CSV |
| ④ 输出 | `package` · `receipt` · `assignments` · `author-proposal` · `catalog` | `delivery_v2.py` | `deliveries/<id>/`、`delivery-ledger.sqlite` |

公共层与公司层的分工：`network.py`、`acquisition.py`、`extraction.py`、`products.py`、`delivery_v2.py` 对所有厂商一样；`adapters/<公司>.py` 只写官方路径范围、产品身份和该厂商的解析例外。现有三家的说明在 [adapters/](adapters/)。

## 3. 在哪台机器上跑

```mermaid
flowchart LR
  GH[("GitHub<br/>niuroumiantt/fetchspec<br/>只有源码与规则")]
  MINI["macmini<br/>采集执行机（目标表 host）<br/>~/.local/share/fetchspec"]
  M5["M5<br/>开发 · NVIDIA 批次"]
  AWS["AWS<br/>inresearch 网站与接收端"]
  SPARK["Spark<br/>永久原件归档 · Reader"]
  VENDOR(("厂商官网<br/>英文 / 中文"))

  GH -->|"git pull"| MINI & M5
  MINI & M5 -->|"HTTPS · robots · 节流"| VENDOR
  MINI -->|"v2.0 包"| AWS
  M5 -.->|"NVIDIA 目录 JSON（现行规格页）"| AWS
  AWS -.->|"原件永久归档"| SPARK
```

一个来源只属一个通道、一台执行机、一个日历；换机器等于结束旧任务、开新任务，不双跑。原件、数据库、快照、包和回执都不进 Git。

## 4. 刷新

改了框架或命令，同时改 [框架](framework-2026-09-30.md)、本文和 `architecture.svg`。SVG 由脚本生成，数字与文件名以代码和 inresearch 快照为准。
