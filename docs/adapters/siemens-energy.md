# Siemens Energy 适配器（2026-10-01）

- **为什么**：`plan` 的"缺适配器"里 Siemens / Siemens Energy 合计在 10 行实例中被点名，是最多的一家。两家是不同公司、不同官网：Siemens Energy（`www.siemens-energy.com`：燃气轮机、变压器、高压开关）与 Siemens（`www.siemens.com`：中低压开关柜、母线槽、消防）。本适配器只做 Siemens Energy；siemens.com 见下文"缺口"。
- **范围**：只收 `https://www.siemens-energy.com/global/en/home/products-services/product/<slug>.html` 全球英文产品页。robots 只禁 `/xy/`；`/global/en.sitemap.xml` 列出全部产品页。
- **主来源**：燃气轮机页的原生 HTML 表——"Performance data for simple cycle / combined cycle"（每个额定一列：出力、效率、热耗率、排气参数）与"Physical dimensions and weight"；变压器页的"Comparison of transformer types"（电压范围、额定容量、冷却方式）。这些表不含 "specification" 字样，适配器按章节名认定，其他表（如业绩清单）不算。
- **身份**：以 slug 为稳定键；名称取标题冒号前的部分（"SGT-800 gas turbine"）；有规格表为 `named_product`。
- **种子与实测**：[`seeds/siemens-energy.json`](../../seeds/siemens-energy.json) 2 个页面，2026-10-01 真实运行 2 个请求、全部 `collected`，绑定 `P.gas-turbine.spec`、`P.gas-turbine.operation`（性能表含热耗率与效率）、`P.transformer.spec`。
- **缺口**：
  - `P.hv-switchyard.*`：没有 8DA/8DQ 产品页；GIS 系列页与 LIFE Blue GIS 页没有表。
  - `P.transformer.operation`：损耗与寿命不在产品页上。
  - **siemens.com（NXAIR、SIVACON S8、8PS 母线槽、Cerberus）**：产品页只有营销文字，额定值以文字出现（"up to 17.5 kV up to 40 kA"），规格在 `assets.new.siemens.com` 的官方样本 PDF 里（如 HA 25.73，60 页）。样本表的标签跨行、单位单列、数值跨多列合并，现有 PDF 解析器只认 "Specifications" 标题，取不到；为它写通用解析容易把数值对错列，按规则留缺口。下一步需要一个只认 Siemens 样本"Technical data / Rated values"版式、并以原页码与行定位的专用解析器，配人工核对。
