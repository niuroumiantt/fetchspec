# Supermicro 历史档案接入（2026-10-07）

用户当前只读 mini 台账查询：4,916 个 PDF 内容、1,735 个网页快照，最新 observation 为 2026-09-23T17:55:06Z。样例包含 SYS-6039P-TXRT、MicroCloud 旧型号、SSG 存储系统、带加号的 SYS-6029UZ-TR4+；PDF 链接含 Chassis Manual、Test Report、附件规格书，不能全部解释成整机规格。网站此前经用户 SSH 验收的版本 9000b22 有 5 个型号、105 张原表，不是全归档覆盖。

`python3 -m fetchspec.legacy_catalog` 在旧 ledger 和原件上保持只读：验证已捕获英文/中文 HTML 的 SHA，复用原表解析器，保留原始观察时间；旧 URL 上的型号必须在标题/heading 中找到对应文字，保留 + 变体。目录页不计具体型号。按持久 edges 精确关联附件，不靠文件名猜型号。全部有官方 GET 来源的已归档文档按内容 SHA 去重成索引，未关联文档保留；PDF 内容不重新读取、传输或提取，附件关系不等于整个产品规格。

导出 products/sources 与 material_index 分成有界 historical_supplement 批次。每批源 HTML 可独立验证；bundle 只带 JSON 和 HTML。接收端增补当前批次，保留原有身份的当前 payload，重叠旧资料进来源/版本历史；增补与全量快照语义不同。需先部署支持 import-bundle 的 InResearch 版本。文档首页计数和分页搜索使用同一公司地址的 view=materials，原文按点击加载。

**[m5 → macmini → AWS]**

从干净隔离 checkout 执行 `bash scripts/publish_supermicro_legacy_from_m5.sh`。脚本在 mini 创建该 Fetchspec commit 的独立 worktree，从旧档案离线整理；启动 AWS 现有部署服务并检查导入模块存在，再经 m5 的 SSH 管道传输 HTML/索引包，由容器验证后写接收库。两台常驻源码分支均不切换。回执写在 m5 的 ~/.local/state/fetchspec。若导入中断，完全相同批次可重放；旧包不替换更新资料。

本机测试夹具保留生产 URL/型号形态，但单元格为 TEST_VALUE；跨仓库测试真正调用接收器和 bundle 校验。私有 mini 原件不在云开发环境，实际解析数量、失败情况和 AWS 新数据必须以用户执行脚本回执验收，不能由夹具推定。
