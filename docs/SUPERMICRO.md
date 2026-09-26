# Supermicro 首家公司归档规则（2026-09-23）

现行公司级入口是 `profiles/supermicro.json`；`rules/supermicro.json` 仍是历史 GPU 小样，不能代表全公司。
用户已授权执行公开资料下载；这不是原厂另行签发许可的证明。原件私有归档，不公开再分发，不绕过登录或 robots。

## 执行机器与源码

Macmini (`hermes`) 为主力抓取机。常驻源码 `/Users/hermes/code/fetchspec`；隔离开发/运行版本可位于
`/Users/hermes/.worktrees/fetchspec/supermicro-company-crawl`。原件和台账始终放在
`/Users/hermes/.local/share/fetchspec`，运行日志放在 `/Users/hermes/.local/state/fetchspec`。
本机 m5 上代码 `/Users/m5/code/fetchspec` 不等于 Macmini 的数据目录，也不等于 GitHub 已更新。

## 抓取边界

- 全部 15 个已声明子 sitemap（产品/资料/通用、图片所属页面、FAQ）保留全部语言条目；官网产品分类页、资料库所有可达分页、产品/解决方案/支持/新闻相关页面继续发现附件。英文、西班牙文、法文、德文、日文、简繁中文入口同等纳入。
- 目标格式：PDF、DOC、DOCX、DOCM、XLS、XLSX、XLSM、XLSB。按文件内容识别，不把 HTML 错误页计为 PDF。
- 通用部分：受限 HTTP、robots、sitemap、HTML 链接/表单解析、持久队列、条件检查、哈希归档、来源观察、报告。
- Supermicro 特定部分：域名/路径范围、公开的 Manuals 查询表单、`spec.js` 注入的 Datasheet 按钮规则、分类入口。
- 手册表单只允许观察到的 `/support/resources/results.php`、`Resource=Manuals`、数字 ProductID 和 ProductName；不提交其他表单。
- Datasheet 按钮规则来自官网 `spec.js`：`.system-blade` 页面中取 `.sku-model` 的 `rel`，排除 SRS，生成同语言公开下载地址。台账标记派生依据，不猜文件名。
- 单连接，请求启动间隔至少 1.5 秒；同时遵守更长 Crawl-delay。robots 缺失/获取失败时除明确 404/410 外关闭抓取。
- 遇到 429/503 或连续 10 个非 robots 错误暂停；文件最大 128 MiB，磁盘剩余不足 8 GiB 暂停。
- 未知 JavaScript API、登录区、robots 排除区、TLS 校验失败的域名和无法解析入口是明确缺口。图片 sitemap 只取页面入口、不保存图片二进制。队列耗尽不等于官网全部文件已找到。

## 目录与稳定性

```text
~/.local/share/fetchspec/
  blobs/<sha前两位>/<完整sha>.<实际格式>         唯一原件；不可变
  library/supermicro/
    vendor-categories/<官网一级分类>/          分类视图（符号链接）
    collections/<文档用途>/                   手册、规格书、营销、方案等视图
    unassigned/                              尚不能确定产品分类的文件
  ledger/companies/supermicro/
    crawl.sqlite                             持久队列、来源、版本观察与错误
    documents.jsonl                          文件/来源/目录视图导出
    status.json                              最近检查点统计
    snapshots/<sha前两位>/<sha>.html           网页证据，不计作下载文档
    runs/<run>/                              配置、robots、最终状态
    inventory/runs/<run>/                     sitemap URL清单、统计与原始配置
    inventory/snapshots/<sha>.xml              sitemap原始证据
  deliveries/                                未来验收后的Spark投递包；本命令不传输
```

目录视图不是重复原件。相同内容合并存储，但不同 URL、文件名、来源页的观察和链接关系保留。
同 URL 内容变化会增加新 SHA 和版本观察，原文件不覆盖。文件消失、抓取失败不自动删除、不宣称 EOL。
不会把 SHA 变化自动解释成 PCN、硬件版本或已审核的逻辑文档版本。
官网一级标签保留为 Servers & Storage、Building Blocks、Edge, Embedded & Telecom、Networking、Client。
分类归属目前有 URL/来源页推断，台账明确标为推断；来源页面标题、面包屑、导航上下文与 HTML 都保留，待审核后才用于研究事实。
跨产品/营销资料可有独立用途集合和多个来源，不强行塞进某个型号。`unassigned` 不隐去。
旧 `ledger/catalog.json` 和旧目录不改写；导入时验证旧 SHA/格式，不伪造旧精确抓取时间。

## 操作

```bash
PYTHONPATH=src python3 -m fetchspec inventory --company supermicro
# 取 inventory 输出的 url_manifest 路径，拼接数据根目录：
PYTHONPATH=src python3 -m fetchspec company-crawl --company supermicro --fetch --manifest /绝对路径/urls.jsonl
# 原地恢复持久队列，不重新下载已完成请求：
PYTHONPATH=src python3 -m fetchspec company-crawl --company supermicro --fetch
PYTHONPATH=src python3 -m fetchspec company-status --company supermicro
# 手动更新：先重新 inventory，再带最新 manifest 和 --recheck；全部已知附件独立检查。
# 强制内容审计：--recheck --force。未变父页面的 304 不跳过附件的独立请求。
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

只运行一个 worker（文件锁）。创建 `ledger/companies/supermicro/STOP` 可在当前请求结束后暂停；人工处理原因后移走 STOP，再运行恢复命令。
`--max-requests N` 是本次请求预算，剩余队列不会丢失。网络错误不无限重试，需要检查报告后用更新命令重试。
未实现自动日历定时全站刷新；不能把一次持续下载或完成提醒说成持续站点监测 UI。

## 与 inresearch 的边界

这里的 SQLite 是采集台账，不是研究事实库；本命令没有把文档读完，没有传输 Spark，没有正式采纳事实。
后续投递必须包含原件 SHA、来源和范围清单，在 Spark 验证完整性再进入阅读/事实提取。
产品、规格条件/单位、版本、比较结论、证据页码、冲突和任务关联仍需按 inresearch 现行阅读与 C3 规则处理。
研究目的与后续候选架构见 inresearch 的 `docs/inbox/framework_proposals/2026-09-23-product-research-and-site-evolution.md`，不能冒充已上线功能。
