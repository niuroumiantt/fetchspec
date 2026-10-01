# Astera Labs 适配器（2026-10-01）

- **为什么**：Broadcom 之后需求最高的是 Retimer 与 PCIe 交换芯片（`P.retimer.*`、`P.pcie-switch.*`），目标行点名 Astera Labs Aries、Scorpio。Broadcom 的产品页是 React 应用、页面不点名数据来源，产品简介与数据手册在 robots 禁止的 `/docs-and-downloads/` 下，见 [可达性](access.md)。
- **范围**：`https://www.asteralabs.com/product-details/<零件>/` 零件页与 `/products/<系列>/` 系列页（WordPress；robots 只禁后台路径；`product-sitemap.xml` 列出每个零件页）。
- **主来源**：
  - 零件页末尾的属性列表（`<li><span class="fw-medium">名称:</span> 值</li>`：封装、最高 PCIe 代次、通道数、产品阶段、代次、包装数量），整理成两列表，值按原文；"Ordering"不算。方法名 `asteralabs_product_attributes`。
  - 没有零件页的系列（Scorpio 交换芯片）用系列页的"Ordering Information"表：只认表头写着 "Part Number" 的表，每行一个型号。
- **附件**：零件页链接系列产品简介 PDF；已有属性表时按规则推迟下载。
- **原文矛盾不改**：PT6162LX 页副标题写"PCIe 5.0 x8"，属性写"PCIe 6.x / CXL 3.x、x16"。两者都按原文保留，由人在 `map-field` 时判断。
- **种子与实测**：[`seeds/asteralabs.json`](../../seeds/asteralabs.json) 2 个页面，2026-10-01 真实运行 2 个请求、全部 `collected`，绑定 `P.retimer.spec`（Aries 6 PT6162LX）、`P.pcie-switch.spec`（Scorpio P 系列订购表）。
- **缺口**：功耗与时延不在这些页面上，`P.retimer.operation`、`P.pcie-switch.operation` 仍缺。
