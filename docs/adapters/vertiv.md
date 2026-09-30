# Vertiv 适配器（2026-09-29）

- **范围**：只收 `https://www.vertiv.com/en-us/products-catalog/…/` 产品目录页。中文站的产品路径与英文站不对应（实测 404），暂不收。
- **主来源**：产品页的原生 HTML `Models` 表（各型号的额定功率、输入输出电压、效率、尺寸、重量）。这张表不含 "specification" 字样，适配器按表头 `Models` 认定为规格表。
- **附件**：页面链接的 Data Sheet PDF 在 `/<hex>/globalassets/…` 下。已有原生表时按规则推迟下载；需要时由执行机上的 `pdftotext` 提取。
- **身份**：以去掉 `/en-us` 的路径为稳定键；有规格表的页是 `named_product`，没有的是 `family_or_directory`。一个产品页包含多个型号，型号行留在原表里，不拆成多个产品。
- **发现**：没有官方产品 sitemap（`/sitemap.xml` 跳到 404 页），`map-sync` 明确拒绝；新型号从 profile 声明的分类页有界展开。
- **需求**：profile 声明 `target_parts` 为 ups、pdu、cdu；目标行实例点名 Vertiv 的共 18 行，`plan` 会把它们列进"现在可抓"。
- **实测**：Liebert EXL S1 1 表 58 单元格、CoolChip CDU 1 表 52 单元格，见 [端到端记录](../records/2026-09-29-e2e.md)。
