# NVIDIA 公司级 Fetchspec 规则

现行目标（2026-09-27）：建立 NVIDIA 官方产品规格数据库。先从官方产品目录发现产品，再按各产品自己的官方参数目录寻找 HTML/PDF/Office 资料。英文与中文保留；页面快照和附件都是证据载体，产品数、规格覆盖与下载文件数分别计量。原件不进 Git；本轮原件放在 M5，结构化数据直交 AWS 的 inresearch.ai，Spark 不参与。

## 产品优先入口

```sh
PYTHONPATH=src python3 -m fetchspec.product_catalog --out /Users/m5/Downloads/tempfetch --max-pages 220 --reparse
```

以官方 `/en-us/products/` 为入口，逐目录扩展产品/规格链接，排除新闻、案例、招聘。复用公共 InventoryFetcher 的 robots、白名单、超时和响应大小限制；最多三个请求在途，起始请求遵守至少一秒及 robots Crawl-delay。重复启动续跑持久 frontier；`--reparse` 校验并重用快照，不重复下载；`--refresh` 显式重新观察并保留旧版。

`blobs/<SHA前2位>/<SHA>.html` 保存不可变页面；`product-catalog/nvidia/discovery.sqlite3` 保存发现队列、目录关系和版本观察；`catalog.json` 为结构化交付。原厂规格表保留分组、字段、配置列、合并单元格和脚注，上下标保留标记。不同产品不硬套同一模板，不猜缺值、不自动把整柜参数换成单卡参数。

型号页、系列/平台、软件服务分开；型号识别待复核，官网列出不等于确认在售。目录数量不是 SKU 数，frontier 耗尽不等于全公司产品穷尽。动态规格、PDF 定向抽取、独立文档站和配置拆分仍待补齐。英文目录先建身份基线，中文资料关联同一产品；其他语种不采集。

inresearch 的 `manage.py product-catalog import --input <catalog.json> --archive-root <原件根>` 核验快照后入私有 SQLite；`product-catalog publish` 用 NVIDIA 专用凭证交 AWS。`/product-catalog.html` 直接用结构化数据展示、筛选、并排核查和导出 CSV；原文只在核查时打开。规格提取不自动成为正式研究采用。

以下 `company-crawl` / `profiles/nvidia.json` 保留旧附件专项任务兼容，其收窄范围不作为产品清单覆盖规则。

## 官方动态规格组件

GeForce 50 系列正文把规格放在官方脚本组件的数据常量里。目录抓取结束后运行 PYTHONPATH=src python3 -m fetchspec.nvidia_components --out /Users/m5/Downloads/tempfetch：只获取已归档产品页明确引用的官方组件，读取受限 JSON 字面量，不执行下载的 JavaScript；只把页面 staticColumns 明确展示的型号入库，不把组件中历史对比选项当成当前系列产品。组件原件、SHA、引用它的产品页和字段原文保留；状态为部分字段提取，完整规格与性能条件仍待核对。与目录 worker 共用排他锁，重复执行复用已验证快照。

## 旧附件任务范围（2026-09-26 收窄）

首轮按"整站覆盖"设计，剩余 2.5 万个待抓 URL 全是 HTML（GeForce 新闻、十几个地区英文站副本、on-demand 视频、驱动、GTC 议程），其中直接文档为 0，已抓页面只有约 10% 链接过 PDF。现改为按研究需求取材：

- 页面只走 `en-us` 与 `zh-cn`（含 www.nvidia.cn）；地区英文站和 zh-tw 页面不再打开，附件语言规则不变。
- 栏目只保留 data-center、networking、products、learn、technologies；sitemap 只用 en-us 与 zh-cn，去掉 on-demand 与 GTC。
- `max_pages_without_new_document: 400`：连续打开 400 个页面没有新文档就以 `paused_low_yield` 暂停，提示检查范围，而不是继续跑。
- 现有队列按新规则重新判定，范围外的记录标为 excluded 保留审计，不删除。

## 中国区网络的图片/文档主机

从中国区网络访问时，`images.nvidia.com` 的 robots.txt 和所有 DAM 附件都会 301 到 `images.nvidia.cn`，路径不变。`images.nvidia.cn` 列入 `allowed_hosts` 与 `robots_hosts`，只接收直接文档，不作为页面主机抓取。未列入时，该主机的 robots 记为 blocked，附件判为 `robots missing or disallowed`（旧版错误文本，2026-09-26 在 M5 上出现，8 份白皮书和 CSR 报告受影响）。

## 网络文档（networking-docs.nvidia.com）

官网网络栏目几乎不直接链接手册。NVIDIA 网络产品文档放在独立站 `networking-docs.nvidia.com`，按产品分成两百多个文档空间（网卡、交换机、光模块、线缆、BlueField 等），每个空间首页直接链接整本手册 PDF。

- `space_sitemaps`：读取该站 sitemap，每个空间只把首页入队（不展开每一页）。
- `page_path_regex_by_host`：该站只打开空间首页；`__attachments` 下的文档照常下载。
- `host_categories`：该站文档归入 Networking。

2026-09-26 核对：sitemap 列出 235 个空间，只有带「Download PDF」按钮（`pdf-download-action`）的空间提供整本 PDF，首轮取到约 48 个附件。抽查无按钮的空间（如 `connectx6enhw`、`nmxcswum`、`800gmma4z00ns`），首页、子页面和 `__pagetree.json` 都没有 PDF，内容只以 HTML 页面发布；页面上的 `__attachments` 多为图片。这部分不是规则漏抓，而是 fetchspec 不归档 HTML 造成的覆盖缺口。

## 网络文档的 HTML 归档（2026-09-26）

这类产品空间多数只发布 HTML，规格参数就在页面里。规则按空间大小区分：

- `space_page_archive.max_pages: 60`：逐个读取每个空间的 sitemap，页数不超过 60 的空间（网卡、BlueField、线缆、光模块、交换机等硬件手册，约 160 个空间、1,500 页）全部页面入队并保存 HTML 快照
- 超过 60 页的空间（UFM、Onyx、BSP、固件发布说明等软件文档，约 39 个空间、5 万页）仍只打开首页、只取 Download PDF
- 每个空间的决定写进台账的 `space_archive` 表，以后的运行不再重读该空间的 sitemap；调整阈值后，删除对应记录即可重新判定
- `save_pages_hosts`：只有这个主机保存 HTML 快照（`ledger/companies/nvidia/snapshots/`，索引在 `pages` 表），其他主机仍不保存页面
- 新保存的快照计为新内容，不触发 `max_pages_without_new_document` 暂停

## 首轮范围（历史）

- 站点：只选择英文（`en-*`）与中文（`zh-cn`、`zh-tw`）页面 sitemap，并展开公开 on-demand sitemap 和 GTC sitemap；索引与各 sitemap 都保留原始快照作为来源证据。其他语种站点不进入抓取队列。
- 语言：默认/无语言标记的官方 DAM 附件视为英文；明确标注英文或中文的附件允许下载；路径、文件名或语言参数明确标注为其他语种的附件排除。现有数据不会因规则更新而删除。
- 官方文档主机：`www.nvidia.com`、`images.nvidia.com`、`resources.nvidia.com`、`docs.nvidia.com`、`www.nvidia.cn`；二级主机只接收直接文档，不把它们当成第二套网站递归抓取。
- 首层分类：Data Center & AI、Networking、Gaming、Professional Visualization、Automotive & Robotics、Omniverse、Developer & Software。
- 文档用途：datasheets、brochures、white-papers、solution-briefs、case-studies、product-guides、manuals、presentations、PCN、other-documents。
- 明确排除：gated/account-only 资源、robots 禁止路径、认证/登录页面、追踪参数和未经证据支持的 API/JavaScript 下载。

## 本地落盘和去重

在 M5 临时运行时：

```text
/Users/m5/Downloads/tempfetch/
  blobs/<2 hex>/<sha256>.<kind>       # 不可变原件，按内容 SHA-256 去重
  library/nvidia/<collection>/...      # 可读视图/符号链接
  ledger/catalog.json                  # 旧 demo 规则的兼容目录
  ledger/companies/nvidia/crawl.sqlite # 公司级队列、来源、版本、错误和观察
  ledger/companies/nvidia/documents.jsonl
  ledger/companies/nvidia/inventory/    # sitemap 快照和 URL 清单
```

同一字节内容只保留一个 blob；文件名、来源 URL 或产品页变化会新增来源记录，内容变化才新增 SHA 版本。原件会保留，不覆盖历史版本。

## 当前已知边界

旧附件专项任务不宣称全站完整；格式/语言检查继续生效。现行产品优先流程与 M5→AWS 分工以上方入口为准，官方产品页面需要保存快照，规格定向提取不以全文阅读完成为前提。独立官方主机、未索引微站和动态资料仍需补齐；gated、登录和 robots 禁止路径不访问。采集台账本身不是正式研究事实库。
