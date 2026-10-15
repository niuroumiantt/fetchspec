# 取证范围跟进交接（更新于 2026-10-16，机器 m5）

## 目标
让上游新增 GPU 配置与取证要求进入既有 coverage/plan/backflow，不因历史 sourced 或旧回执而消失。

## 已定规则
- 原规格状态、原件和历史回执保留；交付只证明接收，不证明字段正确、现场适用、阅读或研究采用。
- 不增加来源、不改调度、不实际采集；不操作 dirty 的 InResearch 主区。
- 完整要求保留在投影中；实质范围指纹排除审阅时间、候选提示、状态和执行分配元数据。

## 进度
- coverage JSON/CSV、plan 和 backflow 保留 collection_contract、完整 request 及 requirements；来源目标 SHA 与冻结上下文可核验。
- 新范围进入 review_scope，不计作 in_flight/collect_now。仅同冻结范围的显式 production 接收可避免重复跟进，缺失/损坏历史快照不能消除缺口。
- Python 3.9.6、3.12.14 各 250 项全量测试通过，包含当前及旧 receiver 集成，无跳过；reporg 生成视图已更新，--check 与 git diff --check 通过。
- 隔离验收上游 main `dc776d007b6e81e413733defb692096da946afee`；目标原字节 SHA `1c65b2968e0c846997fd9ec6e3429af83608e310c3f06b365a584a2d8c65c4c2`；快照 `c545f1116a86e36d199fe8c4dd203646e0c577720c6e88734da2a8a7e8939e66`。130 个 request、1 个 collection_contract；GPU sourced 行进入 review_scope。
- GitHub 当次只读核对：0 Actions workflow、0 ruleset、main 未设 branch protection。无远程检查结果可冒称 CI 通过。

## 下一步
1. 正常 PR 审阅合并后，确认 M5/macmini Fetchspec 源码仍干净且无运行任务，再 fast-forward 到已合并版本；不重启未发现的服务。
2. 用独立、干净且 HEAD 等于 origin/main 的 InResearch checkout 同步目标。M5 现有状态显式指定 `/Users/m5/.local/share/fetchspec/pipeline`；macmini 尚无默认 pipeline 快照，初始化须记为新消费者。
3. 核对上述目标 SHA、130 个 request、1 个 collection_contract，以及 GPU 原 sourced 状态与独立范围复核组；只做 plan/coverage 验收。

## 待用户决定
无新增决定；根任务负责合并、部署与目标快照验收，实际新来源采集不在本批。

## 入口文件与工具
- 规则：`docs/SOURCE_PIPELINE.md`；实现：`src/fetchspec/coverage.py`、`pipeline.py`；回归：`tests/test_demand_scope.py`。
- M5 canonical `/Users/m5/code/fetchspec`，macmini `/Users/hermes/code/fetchspec`；各自数据根由 `store.default_data_root()` 解析，默认 `~/.local/share/fetchspec`。
- 运行点只读核对：M5 仍消费旧上游 `558f9c7e5903271e0c4b2ad41e4bbc4f0f8661b8`，目标版本 2.1.0，130 行且无 request/collection_contract；mini 默认根无当前快照。已查进程、LaunchAgents/launchctl 与 cron 未发现 Fetchspec；不能排除未来手动或自定义 --root。
- AWS 为 InResearch 接收/展示，Spark 为归档/Reader；本次未发现两处 canonical Fetchspec 或默认消费者快照，不向这两处部署采集代码。
- 本机隔离验收：`/Users/m5/.local/state/fetchspec/collection-contract-followup-20261016/consumer-validation.json`、`coverage.json`、`plan.json`。运行数据不入 Git。
