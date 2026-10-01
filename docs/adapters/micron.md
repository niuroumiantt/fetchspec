# Micron 适配器（2026-10-01）

- **为什么先做它**：`plan` 的"缺适配器"里，Micron 在 6 行实例中被点名（HBM、DRAM、CXL 内存、SSD）。同批要加的 Samsung 半导体与 ABB 官网对 `robots.txt` 都返回 403（反爬），按规则不绕过，留给浏览器通道；Siemens / Siemens Energy 的 robots 与 sitemap 可用，排在下一个。
- **范围**：只收 `https://www.micron.com/products/…/part-catalog/part-detail/<零件号>` 零件页（不含 `obsolete/`、`spd-data/`），以及该页自己在 `data-apiresource` 里点名的规格组件 `/content/micron/us/en/products/…/_jcr_content.products.json/getproductinfo/-/-/-/en_US/-/<同一零件号>`。robots 允许这两类路径。
- **主来源**：系列页（如 `/products/memory/hbm/hbm3e`）只有营销文字，没有规格表；零件页的规格由脚本从上述 JSON 渲染。适配器把这个官方 JSON 当作零件页的规格来源：取 `details[]` 的名称与取值原文，完全相同的重复行只保留一行（厂商 JSON 里 HBM3E 字段重复 2–3 次），表的 `method` 记为 `official_component_json_no_execution`。不执行页面脚本。
- **采集链路**：零件页 → `candidates` 只给出它自己的规格组件（角色 specification）→ 采集器按内容类型确认是 JSON 且在组件范围内，交给适配器的 `component_tables`，表挂回零件产品。别的 JSON、跳成 HTML 的组件都不收。
- **身份**：零件号即型号，`named_product`；页面标题里的零件号必须与 URL 的零件号一致，否则不认。
- **交付**：JSON 原件按原字节进包，格式 `json`（必须是 UTF-8 JSON 对象）。inresearch 接收端需要同步接受 `json`，补丁在 inresearch 的 `fetchspec-backflow` 分支；接收后归档为候选，阅读器交接为 `extractor_required`。
- **种子与实测**：[`seeds/micron.json`](../../seeds/micron.json) 3 个零件页，2026-10-01 真实运行全部 `collected`，`P.hbm.spec`、`P.dram.spec`、`P.ssd.spec` 绑定；HBM3E 包经真实接收器（含 json 补丁）收下 2 项。
- **缺口**：零件 JSON 不含功耗，`P.hbm.operation` 待人审数据手册；CZ120（`P.cxl-memory.*`）在 sitemap 里没有零件页；`map-sync` 未启用（官网 sitemap 是 6,000 条混合 URL 的单文件）。
