# 第二批计算目录交接

基线是已合并的第一批交接及真实生产验收。当前本地验收完成，尚待接收端新版本部署和逐公司生产回执；合并代码不表示数据已上架。

本批净新增103个实体：AMD43、Intel35、兆芯12、摩尔线程2、壁仞1、Supermicro5、SK hynix5。保留旧批次后七家共111项。具体型号、原件SHA、原表数量及缺口见 [逐产品覆盖审计](../records/2026-10-03-catalog-batch2-coverage.json)。[InResearch第二批交接](https://github.com/niuroumiantt/InResearch.ai/blob/main/docs/handoff/compute-catalog-batch2-20261002.md)记录生产验收。

- AMD：27款EPYC 9005按HTML单元格显式换行拆型号，单值共享核数保留；16款Instinct/显存变体保留ROCm原表，MI300A仅取GPU部分规格、按APU计其他加速器。MI350X/MI355X是OAM模组，MI350P是PCIe板卡。
- Intel：增加30款CPU、5款GPU，补6980P原表，保留Gaudi3；GPU架构来自各ARK表，板卡—芯片关系未明示就留空。
- 兆芯：12项均为CPU系列，不算具体SKU。gzip原始字节保持SHA，解析只做有界解压；显式div目录行保留空白和原字段顺序，开先/开胜的日期和工艺列不同。
- 摩尔线程：补S4000原生规格表；S5000只取得正文精度及OAM形态等部分规格，没有把对标基准或集群吞吐当单卡参数。官方明确S4000使用QY102AA-800、S5000使用PH100，两个芯片实体不继承板卡参数。QY102AA-800架构未知，PH100有平湖架构原文。
- 壁仞：BR100只有系列发布原文；BR104及BR100独立型号/架构原件仍缺。166L/166M按已有原文拆为模组，166C为板卡，不推底层芯片关系。
- Supermicro：五款真实服务器原表保持服务器类别；SK hynix：PEB110、PS1012 U.2两款SSD与HBM3E、RDIMM、MRDIMM三项内存系列保持存储/内存类别。没有料号的内存系列不算SKU，不将Solidigm型号归入SK hynix。
- 燧原robots403、海光DCU逐型号官方架构缺口继续保留；当次海光页面→官方JS静态JSON组件只取得23款CPU，没有据软件兼容推DCU架构。

## 重跑与边界

`PYTHONPATH=src python3 -m fetchspec.catalog_batch2 --root ~/.local/share/fetchspec/compute-catalog-batch2-20261002 --company <company> --baseline ~/.local/share/inresearch.ai/compute-catalog-batch2-20261002/changed-company-baseline-v2.json`

`--collect`只请求配置中审阅过的官方URL，保留robots观察和原件；新SHA先归档并停止，复审更新pin后才解析。默认只重解析已存原件。补充结果复用ProductStore和schema1导出；旧生产ID、原表和来源保留，不能拿部分新型号清单覆盖公司当前批次。

SK hynix产品门户robots500仍封闭。新闻站根robots跳到同主机/en/robots.txt，配置只准这一精确跳转路径，最终404按现有404/410规则处理；403/5xx仍拒绝，未放开任意页面、主机或查询参数。依据RFC9309的redirect/unavailable语义及既有测试，非绕过robots。原始robots响应与跳转链在原件目录review中保留。

原件、源回执、ProductStore、导出在上述`.local/share/fetchspec`目录；生产基线和验收在`.local/share/inresearch.ai/compute-catalog-batch2-20261002/`。密钥不进Git，Spark未操作。

## 验证

本地228项测试通过（含与干净origin/main 1c41b91接收端的3项集成）；新增回归覆盖EPYC多型号对齐、共享值、歧义拒绝、gzip大小限制/SHA、保留式合并、robots精确路径及Disallow。七家真实原件SHA与新接收端本地接收验收通过；生产回执及最终集成结果待填。

逐型号复核：Flex 140 的半高PCIe形态与170/170V的全高PCIe说明分别取证；型号匹配使用完整边界，不把170的AI说明用于170V。生产包在这次复核后冻结。
