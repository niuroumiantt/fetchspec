# Siemens 适配器（siemens.com，2026-10-01）

- **范围**：`https://www.siemens.com/en-us/products/…/` 产品页，以及这些页链接的 `https://assets.new.siemens.com/siemens/assets/api/uuid:<id>/<名称>.pdf` 官方样本与手册。robots：siemens.com 只禁带查询串的地址，assets 站不禁。产品简介（Profile）、环境声明（EPD）、SIMARIS 工具单页等不跟。
- **为什么要样本 PDF**：NXAIR、SIVACON S8、SIVACON 8PS、Cerberus 的产品页只有营销文字，额定值写在句子里（"up to 17.5 kV up to 40 kA"），没有表。规格在样本里：NXAIR 的 HA 25.73（60 页）、SIVACON S8plus 手册、8PS 手册"Energy and data successfully put on track"。
- **解析器** `src/fetchspec/siemens_catalog.py`：
  - **认页**：页顶几行里有一行以 "Technical data" 开头的页才算技术数据页；同一行后面或下一行短标题是章节名（"Electrical data"、"LI system"、"Dimensions"）。S8 手册每页的导航条写着"Technical data & project checklist"，但那一行以产品名开头，不算。"Product range"等线路图页、少于 3 行带数字的页跳过。
  - **不重排列**：样本表的标签跨行、单位单列、一个数值横跨几个额定电压列。重建网格就得猜数值属于哪一列，所以不做：`pdftotext -layout` 的每一非空行是一行，只在两个以上空格处切分，每格保留在版面中的起始字符列 `x`。跨行标签保持多行，横跨多列的数值保持一格。
  - **清理**：脚注（"1) …"）进表注；页脚（"· Siemens HA 25.73 · 2023 33"）、图号（"R-HA25-708 psd"）去掉；排版用的软连字符与退格符去掉。
  - **方法名** `siemens_catalog_pdf_layout_rows`，章节写成 `p.<页码> · <章节>`，可回到原 PDF 那一页核对。
- **人审**：`map-field` 选一个格子时，`--condition` 写明样本、页码与所在列，例如 `HA 25.73 p.33 Rated values up to 40 kA, 17.5 kV column (rightmost)`。值仍是厂商原文。
- **有界**：一个产品页常链几本样本（NXAIR 链 5 本）。第一本给出表后，同级的其余样本按"已有更好原件"推迟下载，所以每个种子只下 1 本。
- **种子与实测**：[`seeds/siemens.json`](../../seeds/siemens.json) 3 个产品页，2026-10-01 真实运行 6 个请求、全部 `collected`：NXAIR（HA 25.73：电气数据、尺寸、运输 3 张表）→ `P.mv-switchgear.spec`；SIVACON S8（S8plus 手册 4 张）→ `P.lv-switchgear.spec`；8PS（BD01、BD2、LI、LD、LDM、LData、LR 7 张）→ `P.busway.spec`。在 NXAIR 上 `map-field` 一格（17.5 kV）、打包，经 inresearch 现行接收端收下，观测指向样本 PDF 的 SHA。
- **同时修掉的采集器缺陷**：assets 站用分块传输（chunked）。采集器原来从底层连接直接读字节，分块长度行混进了 PDF，判成"无法识别的内容"。现在用 `HTTPResponse.read1`，它会解分块，同样只读已到达的字节，慢速超时检查不变；回归测试见 `tests/test_company.py`。
- **实例名**：目标行里"Siemens Energy …""西门子能源"只算 Siemens Energy，不再同时算 Siemens（长名优先并消耗匹配）。
- **缺口**：`P.fire.spec`（Cerberus 页不链样本）、`P.leak-detection.spec`（siemens.com 无此产品线）、`P.lv-switchgear.operation` 与 `P.busway.operation`（运行类参数未审，先只绑规格行）。
