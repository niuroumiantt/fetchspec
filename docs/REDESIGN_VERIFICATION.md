# 2026-09-29 重构审计与验收

本记录是本轮实测，持续执行入口为 TARGET_PIPELINE.md；不能将本地验收称为线上发布或正式研究采用。

## 基线与保护

- Fetchspec 从最新远程 main `203f54a` 建立 `codex/target-driven-pipeline`；原主工作区仍为 `codex/supermicro-expand-sitemaps`，本地 main 未切换或重置。
- inresearch 权威读取基线 `e8c6db2`；供应契约 1.5、生成目标契约 2.0、目标表 2.1.0，当时 351 条目标中 130 条属于 Fetchspec，数量未写死。上游 2026-09-28 升至供应契约 1.6（providers 增加 `host_default`，其余两项版本不变）；校验接受 1.5 及之后的 1.x。
- 9 个旧工作区（主区加 8 linked）、20 个旧分支只读盘点。catalog-reconciliation 有 1 处未提交修改；nvidia-company-crawl 有 2 处修改和 2 个未跟踪文件；全部保留。
- 以远程 PR、merge tree、patch 对比核实：Supermicro 扩 sitemap 的 c438b74 已被 PR #14 覆盖；component-specs 两提交由 #24 覆盖；product-map 独有 patch 与 #25 相同，后续 #26 已在 main。没有整批 cherry-pick 历史分支。
- 进程 cwd/启动项未发现旧 Fetchspec 工作树引用，但 IDE 状态无法穷尽核验；不据此删除旧树。原件与旧交付台账也未清理。

## 模块取舍

| 既有实现 | 决定 |
|---|---|
| InventoryFetcher 的 HTTPS/robots/节流/跳转/响应边界 | 复用；新产品获取使用按需主机政策层，避免单页先查所有主机 |
| NVIDIA product_catalog 与 nvidia_components | 保留成熟解析和身份，适配器复用；不重造 595 个身份 |
| company crawl/frontier/旧附件台账 | 保留为兼容路径，原资料不迁走；新目标任务用有界产品队列 |
| deliver.py 1.1 | 保留兼容；新 targets + delivery_v2 明确分开打包和回执 |
| 产品 JSON 中的原表 | 无损迁入当前/历史 SQLite 和原单元格表，JSON 可重建导出 |
| OpenAPI Vite viewer | 保留工具，退出规格管线 README 首屏 |

## NVIDIA 无损迁移与真实定向更新

只读来源 `Downloads/tempfetch/product-catalog/nvidia/catalog.json`，SHA256：
`f98af2797d2e710d477816eb5f46938e5291a6617fb35d268a5ff315b563d43d`。

迁移至新 `~/.local/share/fetchspec/pipeline`：

| 验证 | 实测 |
|---|---:|
| 活跃产品 / 具体型号 / 有规格的具体型号 | 595 / 237 / 235 |
| 原表 / 原始单元格 | 825 / 39,327 |
| 来源观察 / 唯一字节 | 923 / 891 |
| 缺失文件 / SHA 错误 / 悬空父关系 | 0 / 0 / 0 |
| 独立保存的旧历史、地图、frontier、成员关系行 | 17,595 |

所有源文件逐份重哈希；导出产品 payload 与原 JSON 逐项相等。重复导入 products、versions、sources、blobs、tables、cells、changes 计数完全不变。旧数据库累计 1,025 条 product_map 没有冒充 595 条当前目录。

随后从 `P.gpu.spec` 出发定向检查 H200 官方产品页：保留既有产品 ID、PDF 表与其他证据，更新 HTML 来源；当前 NVIDIA 仍为 595 产品 / 825 表 / 39,327 原单元格，旧版本全部保留。

## Supermicro 第二家公司

真实官方来源：`https://www.supermicro.com/en/products/system/gpu/8u/sys-821ge-tnhr`。
通过 `P.server.spec` 定向抓取，识别 SYS-821GE-TNHR 稳定型号并保留原 HTML。解析排除嵌套布局父表，保留 Processor、GPU、Memory、Chassis、电源等原厂分节，当前 20 张表 / 107 个原单元格。修正解析器时只读已有 SHA 快照重解析，没有重新下载。

两家公司使用相同网络/字节/版本/原表/产品库/交付模块；适配器只包含官方路径、身份、公司例外。未运行公司全站或宣称目录穷尽。

## 交付与回执

真实候选绑定：NVIDIA H200 → `P.gpu.spec` → `gpu`；Supermicro SYS-821GE-TNHR → `P.server.spec` → `server`。
生成 v2 manifest、SHA256SUMS 和实际原件，保留逐项目标、产品版本与原表；同 SHA 多来源合并为一个包文件。用现行 inresearch 接收代码在系统临时目录接收并重复接收，验证回执完全幂等、原件不重复，回执核验导回本地独立环境台账。

验证脚本：

```bash
PYTHONPATH=src python3 scripts/verify_pipeline_receiver.py \
  --upstream /path/to/inresearch.ai --root /path/to/new-fetchspec-root \
  --package /path/to/nvidia-package --package /path/to/supermicro-package \
  --report /path/to/receiver-validation.json
```

测试运行回执明确 `environment=local_receiver_validation`、`production_received=false`；未上传生产、未修改研究事实。旧 NVIDIA 网页 catalog 接收链仍保留，通用包内的结构化规格目前尚未被上游通用 receiver 投影到网页产品库。

## 验收范围与缺口

最终完整运行 **144 项 Python 测试全部通过**（含实际 inresearch 接收器的临时库集成）。当前 595 项 NVIDIA schema 1 结构化导出另经上游 catalog validate/verify_snapshots 全量校验通过。

Python 回归覆盖既有爬虫、目标形状/归属/漂移、无变化、同 URL 新版本、多语言、原字节去重、历史保留、坏包/坏回执、重复接收、原单元格 CSV、安全公式展示、比较映射的版本、目录不完整/消失复核、原生 Office/静态 HTML、错误恢复和当前来源优先。

**上游边界仍需独立修改与审批：** 现行 inresearch 没有“运行回执 → 作者 checkout → Git”的导入命令；旧 docs-plan 按部件匹配 delivered，会把规格和运行目标一起升级。Fetchspec 已提供严格回执和作者登记提案，但没有越过上游只读授权去改其正式目标状态，也没有宣称 Git 四态闭环已完成。

旧 Office 二进制、扫描 PDF/OCR、未识别动态组件明确保留需要解析/复核的状态；规格候选不是全文研究或正式采用。完整公司覆盖、生产持续调度、线上规格投影与备份恢复不由本轮本地单型号验收证明。
