# NVIDIA 公司级 Fetchspec 规则

本规则把 NVIDIA 公开产品网站上的 PDF、Word、PowerPoint、Excel、CSV 和 OpenDocument 文件接入公司级持久队列。只下载英文和中文资料；英文/中文页面用于发现链接，其他语言页面不抓取。HTML 页面只在内存中解析可下载链接，不写成原件或页面快照。下载原件不进 Git；执行目录由 `--out` 或 `FETCHSPEC_DATA_ROOT` 决定。

## 研究范围（2026-09-26 收窄）

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

这是一轮 NVIDIA 英文/中文公开范围采集，不宣称全站完整：其他 NVIDIA 官方主机/微站、gated 资源和仅由 API 返回的文件仍需单独核对。语言标记依赖官方 URL 路径、文件名和查询参数；未标记附件按 NVIDIA 官方默认英文处理。HTML 页面只用于内存中的链接发现，磁盘目录只新增识别为允许文档类型的附件。采集完成后才进入阅读、字段提炼、Spark 交付和 `inresearch.ai` 采用流程；采集台账本身不是研究事实库。
