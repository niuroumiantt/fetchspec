# Fetchspec 目标驱动管线交接（2026-09-29，m5）

## 目标与已定边界

按 inresearch 当前 `team=fetchspec` 目标采集官方产品和规格，保留原件、原表、版本与候选身份，交付 v2 并核验回执。inresearch.ai 当前只读；写其作者导入/目标状态必须取得扩展范围授权。旧工作区、分支、原件、台账均保留。

## 已完成

新隔离分支 `codex/target-driven-pipeline`：目标快照、公共产品获取和公司适配、官方 sitemap 待处理计划、确定性 HTML/PDF/OOXML、版本 SQLite/CSV、明确绑定、v2 包、环境隔离回执及作者提案。NVIDIA 595产品/825表/39,327原单元格无损迁移，重复导入不增长；H200真实定向刷新保留既有规格。Supermicro SYS-821GE-TNHR真实单型号20表/107原单元格。源与方法见 `docs/records/2026-09-29-redesign-verification.md`。

## 入口

`PYTHONPATH=src python3 -m fetchspec.pipeline --help`；当前工程规则 `docs/TARGET_PIPELINE.md`。
新数据根 `~/.local/share/fetchspec/pipeline`；旧 `~/Downloads/tempfetch` 只读保留。
完整测试：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src FETCHSPEC_INRESEARCH_ROOT=~/code/inresearch.ai python3 -m unittest discover -s tests`。
真实包接收验证：`scripts/verify_pipeline_receiver.py`（系统临时接收根，本地验收回执不表示生产接收）。

## 剩余

若获授权，为 inresearch 补严格回执作者导入与精确 target_id 的 Git delivered 载体，再刷新/审阅/治理/PR；不能继续沿用按部件同时升级 spec/operation 的规则。Fetchspec 通用包已封存结构化表，但上游网页规格投影仍走既有 NVIDIA catalog 接口。生产交付、持续调度、OCR/旧二进制 Office 和完整公司覆盖均需各自验收。

当前用户范围问题已提出：是否允许最小 inresearch 配套变更。未收到答复时维持只读。
