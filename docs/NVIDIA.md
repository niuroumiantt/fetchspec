# NVIDIA 公司级 Fetchspec 规则

本规则把 NVIDIA 的公开产品页、产品资源页、公开 PDF/Word/Excel 入口接入公司级持久队列。下载原件不进 Git；执行目录由 `--out` 或 `FETCHSPEC_DATA_ROOT` 决定。

## 本次首轮范围

- 站点：`www.nvidia.com` 的英文 sitemap、2021–2024 on-demand sitemap、GTC sitemap；`sitemap_index.xml` 保留为来源证据。
- 官方文档主机：`www.nvidia.com`、`images.nvidia.com`、`resources.nvidia.com`、`docs.nvidia.com`；二级主机只接收直接文档，不把它们当成第二套网站递归抓取。
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

这是一轮“英文公开产品/资源范围”的首个 NVIDIA 公司规则，不宣称全站完成。其他地区 sitemap、gated 资源和仅由 API 返回的文件仍列为后续补充范围。采集完成后才进入阅读、字段提炼、Spark 交付和 `inresearch.ai` 采用流程；采集台账本身不是研究事实库。
