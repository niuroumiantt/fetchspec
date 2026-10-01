# Fetchspec

Fetchspec 是 [inresearch.ai](https://github.com/niuroumiantt/inresearch.ai) 六个采集队里的"产品与技术资料"队：按 inresearch 的目标行，从厂商官网取回官方产品规格，保留原件与原表，整理成可比较的参数，打包交付并拿回回执。收到不等于采用，研究事实由 inresearch 判断。

一条主线：**需求 → 爬取 → 整理 → 输出**。为什么这样分、每段的边界，见 [框架](docs/framework-2026-09-30.md)；模块、落盘和机器见 [架构](docs/ARCHITECTURE.md)。

仓库架构页（与 inews.today 同一约定，托管在 `inresearch.ai/admin/fetchspec/reporg.html`）由 `python3 scripts/reporg.py --upstream <inresearch checkout>` 从本仓库的子命令、厂商档案与规则、架构图和目标表生成到 `public/admin/reporg.html`；`--check` 可在提交前确认它没过期。

![Fetchspec 架构：需求 → 爬取 → 整理 → 输出](docs/architecture.svg)

## 给 inresearch 提供什么（供给卡，2026-09-30）

| 项 | 内容 |
|---|---|
| 给谁 | inresearch.ai。包由接收端 `manage.py fetchspec-receive` 收下，登记由作者 checkout 的 `manage.py deliveries import` 写回 Git |
| 什么 | v2.0 包：`manifest.json` + `SHA256SUMS` + `files/`。每项带 `target_ids`、`part_ids`、来源与语言、版本关系、产品身份、厂商原表，以及人审过的参数观测（字段即契约 `parameter_observation_fields`，值为厂商原文） |
| 按哪张表领任务 | inresearch `framework/tco_targets.json` 里 `team == "fetchspec"` 的行：2026-09-30 为 130 行，63 条 `P.<部件>.spec`、61 条 `P.<部件>.operation`、6 条 `F.*`。只领构成与运行两类变量 |
| 回执怎么进 delivered | 回执经 `receipt` 入本地台账；`assignments` 生成每目标行一条的登记文件；作者跑 `deliveries import`，重跑目标表，走 PR 与审阅。Fetchspec 不写 inresearch 的 Git |
| 不给什么 | 翻译、研究结论、目标表状态、全站 URL 清单；新闻、财报、研报、报价、统计归其他队 |
| 跑在哪 | 采集在 macmini（目标表 `host`）；NVIDIA 批次与开发在 M5；接收与展示在 AWS；永久归档与深读在 Spark |

## 四段与命令

所有命令都是 `PYTHONPATH=src python3 -m fetchspec.pipeline [--root <数据根>] <命令>`。

| 段 | 命令 | 做什么 |
|---|---|---|
| ① 需求 | `sync-targets` | 只读快照 inresearch 的目标表与供应契约（commit + SHA） |
| | `plan` · `coverage` | 每条目标的下一步：在途 / 现在可抓 / 复核实例 / 缺适配器；`--backflow` 读 inresearch 回流（[申请中](docs/upstream/backflow-request.md)） |
| ② 爬取 | `map-sync` · `map-ack` | 官方产品 sitemap 的新增与变化候选，人工确认 |
| | `collect` | 从明确种子有界采集：robots、节流、条件请求、内容 SHA |
| | `collect-seeds` | 按 `seeds/*.json` 里审过的种子批量采集，一个请求预算；`--bind` 只绑种子页上有表的产品（[说明](docs/SEEDS.md)） |
| ③ 整理 | `list` · `export` | 查看候选产品；导出产品图、原单元格、参数映射 CSV |
| | `bind` | 把产品显式绑定到目标行并写理由 |
| | `map-field` | 把一个原单元格标成参数（单位、条件、复核人、回答哪几行） |
| ④ 输出 | `package` | 生成 v2.0 包 |
| | `receipt` | 导入接收端回执，按环境记账 |
| | `assignments` | 生成 `deliveries import` 的输入 |
| | `author-proposal` · `catalog` | 可审核的登记提案；NVIDIA 规格目录 JSON |

```bash
export PYTHONPATH=src
python3 -m fetchspec.pipeline sync-targets --upstream ~/code/inresearch.ai
python3 -m fetchspec.pipeline plan --limit 20
python3 -m fetchspec.pipeline collect-seeds --bind --budget 200     # 审过的种子：23 个，覆盖 24 行
python3 -m fetchspec.pipeline collect --company vertiv --target P.ups.spec \
  --url https://www.vertiv.com/en-us/products-catalog/critical-power/uninterruptible-power-supplies-ups/liebert-exl-s1/ --max-pages 4
python3 -m fetchspec.pipeline list --company vertiv
python3 -m fetchspec.pipeline bind --company vertiv --product <product-id> --target P.ups.spec --reason '官方 Models 表与目标实例相符'
python3 -m fetchspec.pipeline map-field --company vertiv --product <product-id> --table 1 --row 3 --cell 2 \
  --field ups.rated_power --unit 'kVA/kW' --condition '250-400kW 型号' --reviewer <你的名字> --target P.ups.spec
python3 -m fetchspec.pipeline package --company vertiv --product <product-id>
python3 -m fetchspec.pipeline receipt --input receiver-receipt.json --environment production
python3 -m fetchspec.pipeline assignments --delivery-id <delivery-id> --output assignments.json --environment production
# inresearch 作者 checkout：python3 manage.py deliveries import --assignments assignments.json
```

## 数据与验证

默认数据根 `~/.local/share/fetchspec/pipeline/`，可用 `--root` 指定；`FETCHSPEC_DATA_ROOT` 与 `config/archive.local.json` 继续适用。源码与规则进 Git；原件、数据库、目标快照、包和回执不进 Git。

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests
# 带真实 inresearch 接收器与导入函数的集成测试：
FETCHSPEC_INRESEARCH_ROOT=~/code/inresearch.ai PYTHONPATH=src python3 -m unittest discover -s tests
```

文档目录见 [docs/README.md](docs/README.md)。旧的公司级全站抓取（`company-crawl`、`product_catalog`、`company-deliver` 1.1）保留兼容，说明在 [docs/archive/](docs/archive/)；仓库根目录的 Vite OpenAPI 查看器是另一个工具，与规格管线无关，见 [docs/archive/OPENAPI_VIEWER.md](docs/archive/OPENAPI_VIEWER.md)。
