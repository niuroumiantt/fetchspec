# Delta 适配器（2026-10-01）

- **为什么**：目标行里 Delta / 台达 被点名 11 次（电源架、PSU、BBU、CDU、冷板、服务器风扇及其运行行）。
- **范围**：`https://www.deltaww.com/en-US/products/<路径>` 产品页（robots 只禁 `/api/`、`/_next/`、`/admin/`；`/sitemaps/products-1.xml` 约 4,000 条）。另外只收 Delta 产品页规格段里点名的 `https://docs.google.com/document/d/e/<id>/pub` 已发布文档（Google 的 robots 允许 `/document`）；别的 Google 文档到不了。
- **页面结构**：deltaww.com 是 Next.js 站。渲染出来的 HTML 只有页面骨架；各段内容在同一个响应的服务端组件负载（`self.__next_f.push`）里。"Product Specifications"段有两种：
  - **段内 HTML 文本**（CDU、BBU、锂电系统）：一串"名称: 值"行。内容常以引用 `"$1c"` 指向负载里的文本块 `1c:T<十六进制字节长度>,…`，适配器按字节长度取出。每行只在厂商自己写的冒号处分成名称与取值，其余原样；没有冒号且不含数字的行记为小标题。方法名 `delta_spec_section_rows`。
  - **嵌入的已发布 Google 文档**（ORV3 电源架与 PSU）：Delta 用它维护规格表。适配器把它当作产品页的规格链接去取，文档里的表全部算规格表，挂回产品；文档本身不成为产品。
- **不执行脚本**：负载按 JSON 字符串读取，不运行任何代码。
- **种子与实测**：[`seeds/delta.json`](../../seeds/delta.json) 4 个页面，2026-10-01 真实运行 6 个请求、全部 `collected`，绑定 `P.power-shelf.spec/.operation`（ORV3 33 kW）、`P.psu.spec/.operation`（ORV3 5.5 kW PSU，含满载效率 >97.5%）、`P.bbu.spec`（33 kW BBU）、`P.cdu.spec`（GoCool-1500）。PSU 效率一格经 `map-field`、打包、`scripts/verify_package.py` 自检，由 inresearch 现行接收端收下。
- **缺口**：AI GPU 冷板页没有规格段（`P.coldplate.spec`）；服务器风扇页尚未审（`P.server-fan.*`）。
