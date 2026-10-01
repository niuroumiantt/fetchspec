# Micron 适配器（2026-10-01）

- **为什么先做它**：`plan` 的"缺适配器"里，Micron 在 6 行实例中被点名（HBM、DRAM、CXL 内存、SSD）。同批要加的 Samsung 半导体与 ABB 官网对 `robots.txt` 都返回 403（反爬），按规则不绕过，留给浏览器通道；Siemens / Siemens Energy 的 robots 与 sitemap 可用，排在下一个。
- **范围**：只收 `https://www.micron.com/products/…/part-catalog/part-detail/<零件号>` 零件页（不含 `obsolete/`、`spd-data/`），以及该页自己在 `data-apiresource` 里点名的规格组件 `/content/micron/us/en/products/…/_jcr_content.products.json/getproductinfo/-/-/-/en_US/-/<同一零件号>`。robots 允许这两类路径。
- **主来源**：系列页（如 `/products/memory/hbm/hbm3e`）只有营销文字，没有规格表；零件页的规格由脚本从上述 JSON 渲染。适配器把这个官方 JSON 当作零件页的规格来源：取 `details[]` 的名称与取值原文，完全相同的重复行只保留一行（厂商 JSON 里 HBM3E 字段重复 2–3 次），表的 `method` 记为 `official_component_json_no_execution`。不执行页面脚本。
- **采集链路**：零件页 → `candidates` 只给出它自己的规格组件（角色 specification）→ 采集器按内容类型确认是 JSON 且在组件范围内，交给适配器的 `component_tables`，表挂回零件产品。别的 JSON、跳成 HTML 的组件都不收。
- **身份**：零件号即型号，`named_product`；页面标题里的零件号必须与 URL 的零件号一致，否则不认。
- **交付**：JSON 原件按原字节进包，格式 `json`（必须是 UTF-8 JSON 对象）。inresearch 接收端需要同步接受 `json`，补丁在 inresearch 的 `fetchspec-backflow` 分支；接收后归档为候选，阅读器交接为 `extractor_required`。
- **种子与实测**：[`seeds/micron.json`](../../seeds/micron.json) 3 个零件页，2026-10-01 真实运行全部 `collected`，`P.hbm.spec`、`P.dram.spec`、`P.ssd.spec` 绑定；HBM3E 包经真实接收器（含 json 补丁）收下 2 项。
- **缺口**：零件 JSON 不含功耗，`P.hbm.operation` 待人审数据手册；CZ120（`P.cxl-memory.*`）在 sitemap 里没有零件页；`map-sync` 未启用（官网 sitemap 是 6,000 条混合 URL 的单文件）。

## 产品目录（2026-10-01，按 NVIDIA 打样标准）

`fetchspec.micron_catalog` 按 inresearch `framework/06_acquisition.md`「产品清单 → 官方规格 → 数据库 → 轻量展示」给 Micron 建全量产品目录，与上面的目标行适配器并行：目标行只挑代表型号做参数映射，目录收全部零件的全部原生参数。

```bash
PYTHONPATH=src python3 -m fetchspec.micron_catalog --out ~/Downloads/tempfetch-micron            # 首次 / 续跑
PYTHONPATH=src python3 -m fetchspec.micron_catalog --out ~/Downloads/tempfetch-micron --refresh  # 条件请求复查已知页
```

- **清单**：官网 `sitemap.xml`（每次运行条件复查）。`/products/` 下的分类与系列页是 `family_or_directory`，现行零件页是 `named_product`（listing `active`），`/products/obsolete/` 零件是 `named_product`（listing `obsolete`，只登记身份、来源是 sitemap 快照、不抓规格）。SPD 数据页与 `…/part-catalog` 索引页只计数。sitemap 里同一 URL 重复出现的去重；同一零件挂在两个系列下合成一个产品，`listings` 保留每个官方位置。
- **分类**：厂商自己的路径（`taxonomy`：slug + 分类页 `<title>` 名，不用 h1 的宣传语），父子关系按路径。
- **规格**：每个现行零件抓零件页和它点名的官方 JSON 组件，`details[]` 的名称与取值原文全收（完全相同的重复行合一），不映射、不换算；零件状态码原文记 `official_status`（如 Production、End of Life），`availability` 仍为 `not_verified`。
- **覆盖率**两个分母分开：现行具体零件中有官方规格的 / 现行具体零件；全部目录实体中有表的 / 全部实体。组件返回空的是厂商规格缺口，取不到的单列 failed/unavailable，都不猜值。
- **输出**：`<out>/catalog.json`（schema 1，`company_id: micron`，与 NVIDIA 同一接收格式）、`product-sitemap.json`、`blobs/`（原件按 SHA）、`micron-catalog.sqlite3`（状态与 ETag）。由 inresearch `manage.py product-catalog import|publish --company micron` 接收。
- **修过的坑**：通用 URL 语言猜测把零件号里的 `-it-`（工业级温度）、`-es-` 读成意大利语/西班牙语，丢掉了这些零件的规格组件；适配器对 Micron 自己的产品与 `us/en` 组件路径不再做语言猜测。
