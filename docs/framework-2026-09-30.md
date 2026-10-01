# Fetchspec 的位置与框架（2026-09-30）

> 对齐 inresearch.ai 骨架（`framework/CURRENT.md`，2026-09-28 起）与 inews.today 框架（`docs/framework-2026-09-30.md`）后写定。这里只定位置、逻辑与接口，不定实现。实现细则在 [TARGET_PIPELINE.md](TARGET_PIPELINE.md)，架构图在 [ARCHITECTURE.md](ARCHITECTURE.md)。改框架先改这里，再改代码和图。

## 一、定位

一切围绕 inresearch.ai。Fetchspec 是它六个采集队里的"产品与技术资料"队，唯一的客户是 inresearch。我们没有自己的网站，线上的产品目录页属于 inresearch。

在 inresearch 的唯一逻辑里，我们只占两个位置：

| 逻辑 | 与 Fetchspec 的关系 |
|---|---|
| 一棵树 | 61 个物理部件是我们的对象；目标行上的 `part_id` 就是骨架部件 ID |
| 三级账 | 不直接产出。我们交的参数经研究采用后，才进入模型输入，例如 `gpus_per_mw`、`load_factor` |
| 四问 · 四段 | 我们在第一段"采集"，只回答第四问"数据从哪来、缺什么" |
| 五类变量 | 只领 **1 构成**（spec 行、因子行）与 **2 运行**（operation 行）；数据类别全是 `reference`，按版本改 |
| 六队 | 厂商官网与官方文档归我们；新闻归 inews，财报归 fetchfilings，研报与标准归 fetchreports，报价归 fetchquotes，统计与费率归 fetchstat |
| 一个模板 | 节点页每列的"→ 采集"链接指向目标表里我们的行；我们不做页面 |

我们产出三样东西：

1. **原件包**：官方页面与附件的不可变字节，带 SHA、来源、语言和版本关系。
2. **结构化证据**：产品身份、厂商原表、以及人审过的参数观测。
3. **回执与登记**：证明对方收到了什么，并把收到的东西登记到具体目标行。

一句话：**inresearch 定义研究要什么，Fetchspec 从厂商官网把可追溯的官方规格证据取回来、整理成可比较的参数、打包交付；证据是否成为研究事实不由我们判断。**

## 二、需求：目标行 → 采集计划

**唯一任务书**是 inresearch `framework/tco_targets.json` 里 `team = fetchspec` 的行，由 `manage.py targets --refresh` 从因子树、部件表、站点权利和 `part_fetch.json` 生成，不手写。2026-09-30 的快照（inresearch `6f592ff`）：

| 项 | 值 |
|---|---:|
| Fetchspec 行 / 全表 | 130 / 351 |
| 部件规格行 `P.<部件>.spec` | 63 |
| 部件运行行 `P.<部件>.operation` | 61 |
| 因子行 `F.*` | 6 |
| 上游状态 sourced / delivered / needed | 6 / 0 / 124 |

**一条目标行 = 部件 × 数据类别 × 出版方实例。** 部件说对象是谁；数据类别说要构成还是运行；出版方实例（`instances`）点名去哪家的哪份资料找，例如 `P.ups.spec` 点名 Vertiv Liebert EXL、Eaton 9395、Schneider Galaxy。数量从文件读，代码不写死。

需求段做两件事：

- **同步**：`sync-targets` 只读 inresearch 的干净 Git checkout，保存完整原文、commit 与 SHA。供应契约接受 1.5 及之后的 1.x；目标漂移、重复 ID、不属于 Fetchspec 的行一律拒绝。
- **计划**：`plan` 把快照变成下一步队列，每行一个动作，分四组，组内按 `next_due` 与敏感度排序，并附参数提示：

| 组 | 含义 | 2026-09-30 |
|---|---|---:|
| 在途 | 已绑定、已打包或已拿到验收回执，下一步是交付或登记 | 0 |
| 现在可抓 | 目标行自己的实例里点名了我们已有适配器的厂商 | 27 |
| 复核实例 | 我们有该部件的适配器，但实例点名的是别家 | 9 |
| 缺适配器 | 实例点名的厂商都还没有适配器 | 88 |

这些数字来自空数据根。算法很简单：把实例文本和各 profile 的 `company_en` 与 `instance_aliases` 比对。所以"要不要加适配器、先加哪家"由目标行决定，不由我们拍脑袋。例子：Vertiv 在 18 行的实例里被点名，其中 16 条是仍需采集的部件行，远超它 profile 声明的 3 个部件。

不自定抓什么。没有目标行的产品不主动扩；主动发现必须显式记为 `discovery`，之后再关联到行。

## 三、爬取：四个通道

按来源归属分，互斥且穷尽：一个来源只属一个通道、一台执行机、一个日历。

| 通道 | 定义 | 执行机 | 现在 |
|---|---|---|---|
| 官方产品页 | 厂商官网产品、系列、对比页里的原生 HTML 表 | macmini（目标表 `host`） | NVIDIA、Supermicro、Vertiv 三个适配器 |
| 官方文档 | 同一厂商公开的 datasheet、手册：PDF、Office、独立文档站 | macmini | NVIDIA 历史抽取可用；新管线的 PDF 需要执行机装 `pdftotext` |
| 浏览器 | 需要登录或渲染的门户（目标表机制 `js_page`、`pdf_registered`） | macmini，人工辅助 | 未做；当前 130 行全是 `vendor_page` |
| 人工 | 前三类拿不到时由人投递原件 | m4 / m5 上传 | 未做 |

**发现**有三条路：已声明的官方产品 sitemap（`map-sync` 只给候选，`lastmod` 变化待人工 `map-ack`）；已声明分类页的有界展开；目标行点名的明确种子 URL。没有官方 sitemap 的厂商（如 Vertiv）`map-sync` 直接拒绝。

**取数**：`collect` 必须给种子和请求预算。遵守 robots，逐主机节流，ETag / Last-Modified 条件请求与内容 SHA 分开计量；同 SHA 不重复存，旧字节不覆盖。只收英文和中文，不执行网页脚本。

**选源优先级**：型号页原生表 > 明确列出型号的系列或对比表 > 原生 PDF / Office 表 > 官方静态数据组件 > OCR 或模型。后两者没实现时明确留缺口，不伪称已处理。已有更好的原生表时，次优附件自动推迟下载。

## 四、整理：四层身份，原值不动

1. **字节**：`blobs/<sha>` 不可变。URL、语言、HTTP 头、观察时间属于来源观察，另记。
2. **产品身份图**：公司 → 官方分类 → 系列 / 平台 → 具体型号，ID 稳定。目录页不是型号；局部消失和 404 只进复核，不判停产。
3. **原表**：`spec_tables` / `spec_cells` 保留厂商的分组、列、合并单元格、脚注、单位和条件，分当前与历史版本。
4. **参数观测**：人审的 `map-field` 把某个原单元格标成一个参数，写明字段名、单位、条件、复核人，以及它回答哪几条目标行（`--target`）。值就是厂商原文，不换算、不补缺；只取当前版本的映射。

**绑定**：`bind` 把产品和目标行显式连起来并写理由。绑定属于某个目标快照，不做模糊推断。同一产品可以回答多行，例如 H200 的显存回答 `P.gpu.spec`，最大 TDP 只回答 `P.gpu.operation`。

中间产物可以 `export` 成 CSV 给人抽查。

## 五、输出：一个包、一张回执、一份登记

| 产物 | 命令 | 去向 | 证明什么 |
|---|---|---|---|
| v2.0 包 | `package` | rsync 到接收机，inresearch `fetchspec-receive` | 我们交了什么：原件、`target_ids`、`part_ids`、原表、参数观测 |
| 回执 | `receipt --environment` | 本地 `delivery-ledger.sqlite` | 对方收到了什么；本地验收不等于生产接收 |
| 登记 | `assignments` | 作者 checkout 跑 `deliveries import` | 哪几条目标行可以翻成 delivered |
| 候选目录 | `catalog` | inresearch `product-catalog import` | 现行的线上规格页（NVIDIA schema） |

包里的参数观测字段就是 inresearch 契约 `parameter_observation_fields`：`company_id`、`product_id`、`target_id`、`part_id`、`parameter_name`、`value`、`unit`、`condition`、`source_url`、`source_sha256`、`observed_at`，另带单元格定位与复核人。

四个状态由四个不同的载体证明，彼此不能冒充：

| 状态 | 谁证明 | 在哪 |
|---|---|---|
| packaged | Fetchspec | `delivery-ledger.sqlite` |
| received | inresearch 接收端 | 回执，按环境记 |
| delivered | inresearch 作者 | Git 内的事件卡，逐目标行 |
| sourced | inresearch 研究 | 序列或已录值指标 |

## 六、与 inresearch 的五个接口

| 接口 | 方向 | 载体 | 现状 |
|---|---|---|---|
| 需求 | inresearch → Fetchspec | `tco_targets.json` + `supply_contract.json` 的 Git 快照 | 已接 |
| 交付原件 | Fetchspec → inresearch | v2.0 包 → `fetchspec-receive` | 已接；真实接收器在临时环境验过，生产未跑 |
| 交付参数 | Fetchspec → inresearch | 包内 `parameter_observations` | 我方已出；接收端只归档，还不入库 |
| 回执与登记 | 双向 | 回执 → `assignments` → `deliveries import` | 已接；上游"规格卡连带翻新闻行"的补丁在 [upstream/](upstream/README.md) 待合 |
| 回流 | inresearch → Fetchspec | 按目标行的状态、已收原件数、已收厂商 | 已申请（[backflow-request.md](upstream/backflow-request.md)）；我方 `plan --backflow` 已能读，等 inresearch 上线接口 |

需要 inresearch 决定的一件事：参数名目前由我们提议（如 `gpu.tdp.max`），它和模型输入键（如 `load_factor`）的对应关系该由研究侧登记。

## 七、骨架会变，系统为变而建

- 目标快照带 commit 与 SHA，绑定属于快照。目标行消失或改 ID 时，打包会拒绝，需要重新绑定，而不是静默沿用。
- 部件归属与厂商别名写在 `profiles/*.json` 的 `target_parts`、`instance_aliases`，不写死在代码里。加一家厂商 = 加一个 profile + 一个小适配器，不开新仓库。
- 参数映射绑在产品版本上；厂商改版后旧映射标 historical，不自动沿用到新版。
- 需要 inresearch 保证的一条（与 inews 相同）：ID 稳定与替代关系。部件拆分或合并时旧 ID 留 `supersedes`，历史绑定才能迁到新行。

## 八、不做什么

- 不做新闻、财报、研报、报价、统计；不翻译。
- 不绕登录、不绕 robots；需要登录的门户只走浏览器通道并单独授权。
- 不写 inresearch 的研究事实、目标表状态或作者 checkout。
- 不把 sitemap URL 数、下载文件数或目录页数当产品数；不以"整站抓完"为目标。
- 不用 OCR 或模型猜值；不换算单位、不补缺。

## 九、落地顺序

1. **本次**：本框架与架构图；`plan`（需求队列）；参数观测进包（整理 → 输出）；文档目录重整。
2. **inresearch 侧**：合入 [upstream/](upstream/README.md) 补丁；接收端把 `parameter_observations` 入库；上线回流接口（[申请](upstream/backflow-request.md)，2026-10-01）；登记参数名与模型输入的对应。
3. **生产闭环**：在 macmini 数据根重跑已验证的四条目标，交生产接收端，取回生产回执，作者导入并审阅合并；执行机装 `pdftotext`。
4. **按 `plan` 铺开**：先做"现在可抓"的 27 行；再按"缺适配器"88 行里被点名最多的厂商加适配器，依次是 Siemens / Siemens Energy（10 行）、Micron 与 Samsung（各 6 行，内存与存储）、ABB（5 行）、Delta（4 行）、Schneider 与 Eaton。
5. **定时**：按目标行 `calendar` 定期 `map-sync` 与 `collect --refresh`。调度还没实现。
