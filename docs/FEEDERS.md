# fetchspec 在研究弹药链中的位置

fetchspec 是 **inresearch.ai 的一个采集产品**，不是 inresearch 的子模块，也不是「每个细分行业一个仓库」里的模板。

完整论证（候选、未采用）：inresearch `docs/inbox/framework_proposals/2026-09-21-feeder-repos.md`。

## 只做这一形态

厂商官网、声明式规则（`rules/*.json`）、公开 PDF / HTML 快照。身份键是 inresearch `data/products.json` 的 `company_id + product_line`。

加 NVIDIA 以外的服务器、电源、液冷厂商： **加规则，不要新开 Git 仓库。**

不要在本仓库实现：新闻聚类（inews）、SEC/EDGAR、必须登录的页面、C3 采用、网站发布。

本仓库里的 Vite OpenAPI 查看器与规格爬虫不是同一产品；扩规则之前应拆开，避免名字混用。

## 磁盘

代码在 Git。PDF 只写 **执行 crawl 的那台长期机器**。阿里云若已重置，旧 `data_root` 作废，换新盘后重抓。机器表见 [MACHINES.md](MACHINES.md)，目录形见 [LAYOUT.md](LAYOUT.md)。

交给 Spark 时送 `incoming/`，不要直接覆盖 reader 的 `raw-materials/` 或 `product_library_index.json`。
