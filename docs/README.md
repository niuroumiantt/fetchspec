# 文档索引

| 读什么 | 文件 |
|---|---|
| 我们是谁、四段逻辑、与 inresearch 的接口（先读） | [framework-2026-09-30.md](framework-2026-09-30.md) |
| 架构图与模块、落盘、机器 | [ARCHITECTURE.md](ARCHITECTURE.md)（图：[architecture.svg](architecture.svg)） |
| 工程细则：身份、存储、采集、交付、回执 | [TARGET_PIPELINE.md](TARGET_PIPELINE.md) |
| 每条目标行从哪个官方页取（种子）与 `collect-seeds` | [SEEDS.md](SEEDS.md) |
| 各厂商适配器的范围与例外 | [adapters/](adapters/)：[NVIDIA](adapters/nvidia.md)、[Supermicro](adapters/supermicro.md)、[Vertiv](adapters/vertiv.md) |
| 需要 inresearch 合入的补丁 | [upstream/](upstream/README.md) |
| 实测与验收记录（带日期，不是规则） | [records/](records/)：[2026-09-29 重构验收](records/2026-09-29-redesign-verification.md)、[2026-09-29 端到端](records/2026-09-29-e2e.md) |
| 交接 | [handoff/](handoff/) |
| 归档（旧公司级全站抓取、旧机器表、旧总览、OpenAPI 查看器） | [archive/](archive/) |

改规则的顺序：先改框架，再改细则与代码，最后改图（`python3 scripts/architecture_svg.py > docs/architecture.svg`）。
