# 产品制造商归属审计（2026-10-07）

用户指出inresearch.ai的Supermicro产品窗口混入Kioxia、Samsung。已核对官网的[Kioxia支持表](https://www.supermicro.com/en/products/storage/pci-e/kioxia)、[Samsung支持表](https://www.supermicro.com/en/products/storage/pci-e/samsung)和[VROC兼容表](https://www.supermicro.com/en/products/nvme/vroc)：SMCI P/N与Manufacturer P/N是不同身份。同一支持路径还包含Intel、HGST和Micron；不能因为官网来源、认证、SMCI料号或供货关系，就归为Supermicro自有产品。

现行`ownership.py`版本2026-10-07.1排除已审查的支持路径（含旧cfm/php、英文/中文路径），不会排除Supermicro自有NVMe/SSG服务器，也不搜索原表中的配件品牌。历史导出先验证HTML，再把排除的URL、SHA、观察时间和标题写入报告；既有原件/台账不动，不把支持页重新投递为别家厂商产品。资料链接索引仍保留来源，归属未知的资料不取得产品身份。

新采集身份拒绝支持页；已有采集缓存与ProductStore导出统一排除明确冲突并保留归属审计，历史产品/版本不删除；新catalog入库拒绝明确冲突。其他公司标题开头的异厂品牌列为review_required，规格正文的配件名称不触发整机排除。未经正面制造商核验的无命中仍是有限规则检查。

审阅两份已保存批次：`2026-10-02-compute-coverage.json`、`2026-10-03-catalog-batch2-coverage.json`，16家公司、19个公司/批次记录、169条目，当前已知冲突/异厂标题规则命中均为0。数据含历史重叠，不能称当前生产总数。其他公司的真实接收库、Supermicro整批归档与现行线上排除数量尚未取得，不能宣称全量无错。运行JSON留在Git外。

**[云开发环境]** 在Fetchspec隔离checkout执行历史批次只读审计：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m fetchspec.ownership --input docs/records/2026-10-02-compute-coverage.json --input docs/records/2026-10-03-catalog-batch2-coverage.json
```

同一命令可审计导出的`catalog.json`。全部输入为空会失败，不报通过。跨仓库测试实际通过InResearch历史增补接收器，证明合法旧型号、原单元格和附件仍接收。网站侧使用相同策略排除既有误归属的首页/分类/搜索/CSV/详情，并提供全公司只读生产审计；其部署与实机回执单独验收。当前云环境没有mini原件或AWS SSH连接，本地测试不是已上线。

本轮验证：Fetchspec全量239项测试通过（6项需要外部集成环境而跳过），另单独执行与InResearch修正checkout的历史增补接收集成5项通过；两个运行策略文件逐字一致。网站相关55项、完整1843项及六组浏览器回归通过，治理/严格校验/registry通过。审计原始JSON保存于当前云工作区`/workspace/scratch/ownership-batch-audit.json`，不进Git。
