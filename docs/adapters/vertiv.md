# Vertiv 适配器（2026-10-01 修订）

- **范围**：`https://www.vertiv.com/en-<地区>/products-catalog/…/` 英文产品目录页。en-us 优先；有些产品只在英文地区目录上（如 en-latam 的 Monitored rPDU），身份键去掉地区前缀并忽略大小写，同一产品跨地区只算一个。中文站的产品路径与英文站不对应（实测 404），暂不收。
- **主来源，两种页型**：
  - 多型号页：原生 HTML `Models` 表（各型号额定功率、输入输出电压、效率、尺寸、重量）。表头不含 "specification"，适配器按表头 `Models` 认定为规格表。
  - 单型号页（CoolChip CDU 600/70、FF3175、SmartMod Max、EnergyCore、PowerDirect 3000 等）：页面没有 HTML 表，规格表只存在于"Print"按钮背后的 pdfmake 定义 `var docDefinitionSpecs` 里。适配器只读其中的字符串字面值（处理 JS 转义与 HTML 实体），不执行脚本，表的 `method` 记为 `vendor_print_definition_literal_no_execution`，与 HTML 表区分。页面已有 HTML 规格表时不读它；所有值都是 N/A 的表不算规格表。
- **附件**：Data Sheet PDF 在 `/<hex>/globalassets/…` 下。已有原生表时按规则推迟下载；需要时由执行机上的 `pdftotext` 提取。
- **身份**：有规格表的页是 `named_product`，没有的是 `family_or_directory`。一个产品页包含多个型号时，型号行留在原表里，不拆成多个产品。
- **发现 = 审过的种子**：没有官方产品 sitemap（`/sitemap.xml` 跳到 404 页），`map-sync` 明确拒绝；分类页的产品列表由脚本渲染，站内搜索被 robots 禁止。所以新产品只经 [`seeds/vertiv.json`](../../seeds/vertiv.json) 进入，由 `collect-seeds` 批量采集。
- **空壳页**：部分下架页面仍回 HTTP 200，只有站点导航、没有标题和产品内容（2026-10-01：Trinergy Cube、Liebert CRV、VR Rack2、HPL、en-us Monitored rPDU）。这类种子报 `no_product`，要人工换成在售页面。
- **需求与实测**：见 [种子说明](../SEEDS.md) 与 `collect-seeds` 的验证记录。
