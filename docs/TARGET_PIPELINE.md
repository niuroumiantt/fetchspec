# 目标驱动规格管线

现行 Fetchspec 工程细则，2026-09-30 更新。位置、四段逻辑与接口见 [框架](framework-2026-09-30.md)，架构图见 [ARCHITECTURE.md](ARCHITECTURE.md)。本页替代新任务继续扩大 `company-crawl` 全站队列的做法；旧命令保留维护既有资料。研究权威仍只来自 inresearch.ai 的 CURRENT、现行目标表与供应契约，本仓库不复制或修改正式研究事实。

## 边界与模块

| 层 | 实现 | 所有权 |
|---|---|---|
| 需求 | `targets.py` | 只读当前目标和供应契约（1.5 及以后的 1.x；新主版本拒绝），保存完整原文、commit、SHA；只领取 Fetchspec |
| 公司适配 | `adapters/` | 官方路径、语言、目录/型号身份和公司解析例外 |
| 变化计划 | `product_map.py` | 官方产品 sitemap 候选、lastmod 对账、持久待处理计划和人工复核 |
| 公共获取 | `acquisition.py` + `inventory.InventoryFetcher` | 有界 frontier、条件请求、robots、节流、重试、快照、观察和失败 |
| 原生提取 | `extraction.py`，兼容 NVIDIA 原规则 | HTML 表格，原生 PDF 文本，DOCX/XLSX/PPTX 原表；不执行脚本/宏，不猜扫描件 |
| 候选库 | `products.py` | 产品和原表版本、关系、来源、字节、单元格、变化、显式任务绑定、CSV |
| 交付 | `delivery_v2.py` | 逐项目标/部件/产品、原规格表、SHA 包、核验回执、作者登记提案、`deliveries import` 输入 |
| 需求计划 | `coverage.py` | 每条目标：声明适配器、实例点名的厂商、绑定、包、各环境回执；`plan` 排出下一步队列；上游状态只复制不推断 |
| 命令 | `pipeline.py` | 编排以上用例，不拥有研究判断 |

NVIDIA 的成熟 `product_catalog.py` 解析和身份规则通过兼容适配复用，避免重新识别导致 595 个既有 ID 丢失。旧公司抓取和旧 1.1 生产器没有被强行改写为新库；新任务默认走本管线，历史队列保留原状。

## 身份与存储

默认 `~/.local/share/fetchspec/pipeline/`：

- `targets/snapshots/<id>/` 保存完整上游文件和摘要；`targets/current.json` 是当前快照指针。供应契约接受 1.5 及之后的 1.x（现行 1.6），生成目标契约 2.0，目标表 2.x；版本/形状不兼容、重复 ID、未知或非 Fetchspec 目标拒绝执行。目标数量从文件读取。
- `blobs/<前2位>/<sha>` 保存唯一字节，无格式扩展名；格式、URL、语言和文件名属于来源观察。原件独占发布，旧字节绝不覆盖。
- `products/catalog.sqlite3` 分表保存 products、product_versions、relations、sources、blobs、spec_tables、spec_cells、changes、bindings、imports、catalog_snapshots、historical_evidence、errors、comparisons。当前投影与历史版本分开；catalog_snapshots 保存原目录覆盖说明和 frontier 等元数据，规格原值不因比较字段而改写。
- `acquisition/<company>/sources.sqlite3` 管理可恢复抓取 frontier、来源字节版本、每次 HTTP 观察、错误和工作中产品载荷；公司进程锁避免重复抓取。同一产品库导入失败后可从该台账重新投影。
- `scopes/` 保存此次明确目标和种子 URL；来源路径不自动证明产品与需求相符。`bind` 需要明确产品 ID 和理由，绑定版本属于特定目标快照。
- `deliveries/<delivery_id>/` 含 manifest、SHA256SUMS、files/；`delivery-ledger.sqlite` 区分 packaged 与有真实回执的 received。包生成绝不把目标变成 sourced。

产品版本指纹覆盖完整产品身份、来源和原表，只排除检查时间与展示变化标签。辅助 PDF/表格变化也会生成版本。旧时间批次保存历史而不回退当前指针；同时间冲突拒绝。缺失产品继续保留，局部目录消失和 404 只进入复核，不能推断停产。

CSV 分别导出产品图、原始单元格和版本化比较映射；包括行/列、跨度、表名、脚注、方法、来源 SHA 和目标绑定。仅 CSV 展示转义公式，数据库原值不变。单位、典型/最大、配置、条件保留在厂商原表；`map-field` 需要明确表/行/单元格、字段名、单位、条件与复核人，不自动换算或填补未知值；可用 `--target` 限定它回答哪几条目标行（`comparison_targets` 表），省略时回答产品绑定的全部目标行。打包时当前版本的映射按 inresearch `parameter_observation_fields` 写进 `items[].product_evidence[].parameter_observations`，值为厂商原文；没有映射的包不带这一键，与旧包逐字节一致。旧版映射不会自动沿用到新版规格，CSV 标记 current/historical。

## NVIDIA 迁移

先核验 active `product-catalog/nvidia/catalog.json`，不能把旧 discovery 数据库累计 product_map 全表当活跃产品。

```bash
PYTHONPATH=src python3 -m fetchspec.pipeline --root ~/.local/share/fetchspec/pipeline migrate \
  --catalog /path/to/legacy/product-catalog/nvidia/catalog.json \
  --archive-root /path/to/legacy \
  --history-db /path/to/legacy/product-catalog/nvidia/discovery.sqlite3
```

迁移拒绝把新根放进旧归档目录。逐份重算源字节 SHA，复制到新 CAS，完整保存产品 JSON、原表与单元格；不移动或删除原件。历史产品/事件/目录/frontier 行单独保存为 historical_evidence，不恢复旧活跃身份。旧 company ledger、下载原件与旧交付仍按原路径保留，没有被本次迁移冒充为已归档完毕。

## 采集与恢复

`map-sync --company ... --target ...` 只读取已声明的官方产品 sitemap 并输出待复核计划；未处理候选跨无变化重跑保留，显式 `map-ack --company ... --url ... --reason ...` 才确认处理。NVIDIA 首次导入已知 URL 建立基线，不全量重抓；新 URL/lastmod 变化只生成候选，需按目标选择后再 collect。Supermicro 排除 FAQ、新闻和广泛资源 sitemap。

`collect --company ... --target ... --url ...` 仅从指定官方产品/目录展开，最大请求预算必需有界；产品页有可解析原表时优先使用 HTML，再选择 PDF、Office。只重试可恢复错误，受 robots 拒绝的内容不绕过。普通重跑恢复未完成项，`--refresh` 条件重查指定来源；`--reparse` 只重解析已保存字节，不请求网络；ETag/Last-Modified 与 SHA 分开计量。HTTP 请求成功但无法确定解析时明确保留缺口，OCR/模型没有实现时不会伪称已处理。

`collect-seeds [--company ...] [--target ...] [--bind] --budget N` 对 `seeds/*.json` 里审过的种子批量执行同一条 `collect` 路径，整批共用一个请求预算；`--bind` 只绑定在种子 URL 本身观察到且有原生表的产品，理由取自种子。细则与实测见 [SEEDS.md](SEEDS.md)。

NVIDIA 保留英文/中文现有身份映射与成熟原表；Supermicro 仅有轻量型号、官方路径和公开资源解析规则；Vertiv 只收 en-us 产品目录页，产品页原生 `Models` 表是主来源（无官方 sitemap，`map-sync` 明确拒绝，新型号从已声明分类页有界发现）。每个 profile 的 `target_parts` 声明它可服务的部件，`coverage` 据此列出无人覆盖的部件。目录页面不是产品型号；scope_exhausted 只说明本次有界队列耗尽，不表示公司目录完整。

## v2 交付与接收

`package` 只接受当前快照下显式绑定的产品；检查所有源字节和表格引用。相同 SHA 一个文件、多个来源观察和产品/目标关联；不同字节保留新包和 supersedes。每份 manifest 带上游快照与采集代码版本；同范围无变化重跑返回原包。

现行 inresearch v2 receiver 要求 `task_id_or_discovery` 是已有人工 supply task 或 `discovery`。本生产器采用兼容信封 `discovery`，另以 `collection_trigger=generated_targets` 和真实非空 `target_ids` 表示生成目标触发，绝不编造人工 task ID。接收端按自己的当前目标表解析 part_ids。

回执按 `--environment` 隔离；默认 receiver 表示环境未指定，不自动证明生产接收。`scripts/verify_pipeline_receiver.py` 只在系统临时目录调用真实上游接收器，回执存入 local_receiver_validation，报告明确 production_received=false；默认作者提案不会读取本地验收回执。

回执导入校验 manifest hash、全文件集合、逐项目标、批次目标及当前部件解析，拒绝错包、缺项、目标漂移和冲突；原回执永久保留。包内结构化产品规格保存在 manifest 中，通用 receiver 目前只归档与索引原件；现有 NVIDIA 网页规格目录仍使用其既有 catalog 导入接口，不声称通用包已经更新线上规格页面。

`author-proposal` 输出带回执和真实目标的可审核 JSON，不写 inresearch checkout。`assignments --delivery-id ... --output ...` 从已核验回执生成 inresearch `manage.py deliveries import --assignments` 直接可读的文件：每条绑定目标一条记录，`evidence_path` 取公开官方产品页 URL（先 HTML 后附件），note 带 delivery、manifest、receipt 与原件 SHA。`local_receiver_validation` 回执默认拒绝，只有显式 `--allow-validation` 才生成并标 `REHEARSAL`。

**上游两处需先修（补丁与验证见 [upstream/](upstream/README.md)）：** inresearch `knowledge/targets.py` 把任一事件卡的 `part_id` 当作该部件 news 行已交付，我们的规格卡会把 inews 的 `P.<部件>.news` 一起翻成 delivered；旧 docs-plan 载体也按部件同时翻 spec 与 operation。另外 `framework/tco_targets.json` 是受审文件，导入后需人工更新审阅记录。收到回执不等于 Git 目标四态已闭环；作者导入并通过审阅才算。

## 退出与清理

旧主工作区、dirty/untracked 文件、分支、台账和原件全部保留。只清理本任务无唯一资料、已经合并并推送、无进程或应用引用的临时源码工作树；无法证明时保留。程序不实现原件垃圾回收，不把删除远程分支当作本地可删证据。
