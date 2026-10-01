# 2026-10-01 Micron 闭环与材料完整性验证

本记录是一次实测，不是规则。运行在隔离的云端容器，数据根、接收根与作者 checkout 都是临时目录；**没有向生产上传，也没有修改或推送 inresearch 的 Git**。接收端与作者侧用的是 inresearch `fetchspec-backflow` 分支（`16b7896`，含 json 接收、回流接口与交付范围补丁）的真实代码，回执环境是 `local_receiver_validation`。

## 输入

| 项 | 值 |
|---|---|
| inresearch 基线 | `origin/main` `9f1bc7f`；目标快照 `5e820715…`，Fetchspec 130 行 |
| 种子 | `seeds/micron.json` 3 个零件页 |
| 网络 | 直连 www.micron.com，遵守 robots，逐请求节流 |

## 逐段结果

| 段 | 动作 | 结果 |
|---|---|---|
| ① 需求 | `sync-targets` | 快照 130 行，3 行 `P.hbm.spec`、`P.dram.spec`、`P.ssd.spec` 为 needed |
| ② 爬取 | `collect-seeds --company micron --bind` | 6 个请求（3 个零件页 + 3 个官方 JSON 组件），3/3 `collected`，3 行绑定 |
| ③ 整理 | `map-field` ×10 | HBM3E 36GB/9.2GTPS/1.1 VOLTS；DDR5 RDIMM 64GB/6400MTPS/1.1 VOLTS/RDIMM；9550 PRO 15360GB/12 GB/s/E3.S。值为厂商原文，单位与条件人审 |
| ④ 输出 | `package`（3 个产品一个包） | `fetchspec-micron-6f707b42…`，manifest `9e6c0797…`，6 项（每行 1 个 HTML 零件页 + 1 个 JSON 组件），10 条参数观测 |
| 完整性 | `scripts/verify_package.py --require-format html --require-format json` | 通过：SHA256SUMS 与 files/ 一一对应、全部哈希相符；每行都有页面与组件；10 条观测都指向自己所在原件的 SHA，值在原件 JSON 的 `details[].value` 里逐字存在 |
| 接收（现行生产代码） | inresearch `origin/main` 的 `fetchspec_receive` | **拒收** `item_contract_invalid`：现行接收端不认 `json` 格式 |
| 接收（分支） | `fetchspec-backflow` 的 `fetchspec_receive` | 收下；part_ids 解析为 dram、hbm、ssd；6 项归档、10 条观测入库；内容寻址原件 18 份全部哈希核对一致；同包重放回执逐字节相同 |
| 回执 | `receipt --environment local_receiver_validation` | `receipt_validated`；重放 `replayed: true` |
| 登记 | `assignments` | `production` 拒绝（无生产回执）；`local_receiver_validation` 不加 `--allow-validation` 拒绝；加上后 3 条，note 以 `REHEARSAL` 开头，带 manifest、回执与原件 SHA、观测数 |
| 作者 | 临时 checkout 里 `manage.py deliveries import` | 3 张卡；目标表恰好这 3 行 needed → delivered，其余 127 行不变（交付范围补丁生效，没有连带新闻行）；重复导入 0 张；`deliveries check` ok |
| 回流 | 分支的 `build_backflow`（以接收根的 acquisition 汇总代替 Spark 发布的快照） | 130 行；3 行 `delivered`，各 `received_items` 2、`companies` [micron]、观测 3/4/3；其余行计数为 0 |
| 计划 | `plan --backflow` | 无回流时 3 行在途；读入回流后 `delivered_upstream` 3，在途 0，这 3 行退出队列 |

## 结论

Micron 从目标行到 delivered、再回流到计划，整条链在本地打通，材料完整：每条交付都带官方页面与官方组件两份原件，每个参数都能在它引用的原件里逐字找到。

## 离生产闭环还差什么

1. inresearch 合入 `fetchspec-backflow` 分支（json 接收、回流接口、交付范围补丁）；受审文件需站长签名。合入前，生产接收端会以 `item_contract_invalid` 拒收 Micron 包——本次已实测。
2. macmini 上重跑本记录的 ①–④，先 `scripts/verify_package.py` 自检，再把包传到生产接收端，取得生产回执后 `receipt --environment production`、`assignments --environment production`。
3. 作者 checkout `deliveries import`，审阅人更新受审记录后合并；Spark 发布后 `/api/targets/backflow` 即给出上表的回流。
