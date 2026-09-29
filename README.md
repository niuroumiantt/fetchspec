# Fetchspec

Fetchspec 是 inresearch.ai 的厂商规格采集队。任务只来自当前 `framework/tco_targets.json` 中 `team=fetchspec` 的目标；官方产品与规格都是候选证据，接收不等于研究采用。

当前入口：**目标同步 → 产品地图 → 有界增量/定向采集 → 原生规格 → SQLite/CSV → v2 包 → 核验回执**。公共层管理网络政策、不可变字节、来源观察和交付；NVIDIA、Supermicro 适配器只管理官方路径、产品身份与例外。只收英文、中文，不执行网页脚本，不进行无界全站爬取。

```bash
export PYTHONPATH=src
python3 -m fetchspec.pipeline sync-targets --upstream /path/to/inresearch.ai
python3 -m fetchspec.pipeline collect --company supermicro \
  --target P.server.spec \
  --url https://www.supermicro.com/en/products/system/gpu/8u/sys-821ge-tnhr --max-pages 3
python3 -m fetchspec.pipeline list --company supermicro
# 核对产品与目标的适用范围后，用明确的产品 ID 绑定并交付：
python3 -m fetchspec.pipeline bind --company supermicro --product <product-id> \
  --target P.server.spec --reason '官方 GPU 服务器型号规格与目标实例相符'
python3 -m fetchspec.pipeline package --company supermicro --product <product-id>
python3 -m fetchspec.pipeline receipt --input /path/to/receiver-receipt.json
python3 -m fetchspec.pipeline export --company supermicro --directory /path/to/csv
```

默认新运行根为 `~/.local/share/fetchspec/pipeline`；可在命令前用 `--root` 指定，既有 `FETCHSPEC_DATA_ROOT` / `config/archive.local.json` 继续适用。源码进 Git，数据库、官方原件、目标快照、包和回执不进 Git。

- 我们在 inresearch 骨架里的位置、任务来源、十步流程、交付物与架构流程图：[总览](docs/OVERVIEW.md)。
- 当前实现、数据库、迁移和运行边界：[目标驱动管线](docs/TARGET_PIPELINE.md)。
- 本轮审计与实测：[重构验收](docs/REDESIGN_VERIFICATION.md)。
- NVIDIA 历史采集成果和兼容入口：[NVIDIA](docs/NVIDIA.md)。
- 旧 `company-crawl`、`product_catalog`、`company-deliver` 保留兼容，`company-deliver` 仍为旧 v1.1；新目标任务使用 `pipeline`，不得把旧打包记录解释为接收回执。
- 仓库原有 Vite OpenAPI 查看器是独立兼容工具，见 [OpenAPI viewer](docs/OPENAPI_VIEWER.md)。

验证：`PYTHONPATH=src python3 -m unittest discover -s tests`。跨仓库接收验收另设 `FETCHSPEC_INRESEARCH_ROOT=/path/to/inresearch.ai`；只使用临时接收库，不写上游 checkout 或生产。
