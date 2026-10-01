# 需要 inresearch.ai 处理的事项

- [回流接口申请](backflow-request.md)（2026-10-01）：按目标行告诉 Fetchspec 状态、已收原件数与厂商。
- 下文：规格事件卡连带翻新闻行的补丁（2026-09-29）。

## 配套补丁

`inresearch-delivered-scope.patch` 针对 inresearch.ai `6f592ff`，`git apply --check` 干净。Fetchspec 对上游只读，这个补丁由 inresearch 作者审阅后在那边合入；本仓库只保存补丁和验证记录。

## 为什么需要

2026-09-29 真实端到端运行时，作者 checkout 执行 `manage.py deliveries import` 导入一条 `P.server.spec` 交付后，目标表 delivered 从 0 变成 **2**，不是 1。多出来的是 `P.server.news`，它属于 inews 队。

原因在 `src/inresearch/knowledge/targets.py`：任何带 `origin_pointer` 的事件卡，只要有 `part_id`，就把该部件的 news 行判为 delivered。`deliveries import` 生成的每张卡都带 `target_id` 和从目标行复制来的 `part_id`，所以任何一队交付任何部件行，都会把同部件的新闻行一起翻掉。这与 06 采集规范"delivered 按目标行、只认 Git 内载体"的判据不符。

## 补丁内容

| 文件 | 变化 |
|---|---|
| `src/inresearch/knowledge/targets.py` | 带 `target_id` 的事件卡只交付那一行；`part_id` / `site_right_id` 只对不带 `target_id` 的旧式卡保持原义 |
| `src/inresearch/workflow/supply.py` | 供应页 `generated_targets` 增加 `delivered` 计数；此前第一行 delivered 出现时 total ≠ sourced + assumed + needed |
| `tests/unit/test_supply.py` | 计数恒等式加入 delivered |
| `tests/unit/test_tco_targets.py` | 镜像判据同步；新增回归测试：`P.server.spec` 卡只交付该行，`P.server.news`、`P.server.operation` 保持 needed，无 `target_id` 的旧卡含义不变 |

## 验证

| 检查 | 结果 |
|---|---|
| 新回归测试在未修补代码上 | 失败（`'delivered' != 'needed'`），证明它能抓到这个问题 |
| 上游全部单测，干净 clone + 补丁 | 1603 项通过，1 项跳过 |
| 作者流程：补丁 + 导入 4 条 Fetchspec 交付 + `dashboard --refresh` + `validate --strict` | delivered 0 → 4，恰好是 `P.gpu.spec`、`P.server.spec`、`P.ups.spec`、`P.cdu.spec`；其余 126 行不变；1603 项通过 |

## 合入之后仍需人工的一步

`framework/tco_targets.json` 以及补丁里的三个源码与测试文件都登记在 `framework/verification_contract.json` 的受审文件里。`governance --check` 会报 "changed reviewed file; review required"，`--refresh` 按设计不替人批准哈希。所以每次导入交付后的目标表变更，都需要审阅人更新审阅记录再合并。这是上游有意设的人工闸门，Fetchspec 不绕过。
