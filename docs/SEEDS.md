# 种子：每条目标行从哪个官方页取（2026-10-01）

种子是一条审过的判断：**这个官方产品页回答这些目标行，理由是……**。种子写在 Git 里（`seeds/<公司>.json`），每个页面选择都可审阅；`collect-seeds` 按种子批量采集。

## 格式

```json
{"company_id": "vertiv", "reviewed_at": "2026-10-01", "note": "…",
 "seeds": [{"targets": ["P.ups.spec", "P.ups.operation"],
            "url": "https://www.vertiv.com/en-us/products-catalog/…/liebert-exl-s1/",
            "product": "Liebert EXL S1",
            "reason": "目标行点名 Liebert EXL；官方 Models 表给出额定功率、电压与双变换效率",
            "max_pages": 3}]}
```

载入时校验：文件名等于已注册适配器的 `company_id`；`url` 必须在该适配器的官方产品页范围内；`targets` 非空且不重复；`reason` 必填，最多 300 字；`max_pages` 为 1–20，默认 3。

## 命令

```bash
python3 -m fetchspec.pipeline collect-seeds --dry-run                    # 只列计划，不发请求
python3 -m fetchspec.pipeline collect-seeds --bind --budget 200          # 全部种子，一个请求预算
python3 -m fetchspec.pipeline collect-seeds --company vertiv --target P.ups.spec --bind
```

- 种子的目标行不在当前快照里：跳过，不改写种子。
- 目标行上游已是 sourced / assumed / delivered：跳过，除非加 `--include-closed`。
- 每个种子走与 `collect` 相同的路径：robots、逐主机节流、条件请求、内容 SHA、范围文件。
- `--bind` 只绑定**在种子 URL 本身**（或它的跳转目标）观察到、且有原生规格表的产品，理由写成 `seed: <reason>`。种子页链出去的其他产品会被采集，但不会绑定。产品类型不限：审过的系列页如果带官方对比表（NVIDIA HGX），也可以作为证据。
- 结果按种子给出状态：`collected`（有表的产品）、`no_tables`（有产品但无表）、`no_product`（页面无可识别产品，常见于空壳页）、`skipped`、`error`。
- `plan` 和 `coverage` 读种子：有种子的行动作是 `collect-seeds --target <行> --bind`；`coverage` 的 `seeded` 计数有种子的行。

## 现有种子与实测（2026-10-01）

23 个种子，覆盖"现在可抓"27 行中的 24 行；另有新适配器 Astera Labs 的 2 个、Delta 的 4 个、Micron 的 3 个、Siemens 的 3 个、Siemens Energy 的 2 个种子（见表）。新数据根上一次真实运行：23 个种子全部 `collected`，用 25 个请求（预算 150），24 行完成绑定。`plan` 从"现在可抓 27"变为"在途 24、现在可抓 3"。

| 公司 | 种子 | 行 |
|---|---:|---|
| NVIDIA | 8 | `P.gpu.spec/.operation`、`P.rack-system.spec`、`P.cpu.spec`、`P.nic.spec`、`P.network-switch.spec/.operation`、`P.optics.spec` |
| Supermicro | 2 | `P.server.spec/.operation`、`P.rack-system.spec` |
| Astera Labs | 2 | `P.retimer.spec`、`P.pcie-switch.spec`（同日实测 2/2） |
| Delta | 4 | `P.power-shelf.spec/.operation`、`P.psu.spec/.operation`、`P.bbu.spec`、`P.cdu.spec`（同日实测 4/4） |
| Micron | 3 | `P.hbm.spec`、`P.dram.spec`、`P.ssd.spec`（新适配器，同日实测 3/3） |
| Siemens | 3 | `P.mv-switchgear.spec`、`P.lv-switchgear.spec`、`P.busway.spec`（样本 PDF 版面行，同日实测 3/3） |
| Siemens Energy | 2 | `P.gas-turbine.spec/.operation`、`P.transformer.spec`（新适配器，同日实测 2/2） |
| Vertiv | 13 | `P.ups.spec/.operation`、`P.ups-battery.spec`、`P.bbu.operation`、`P.pdu.spec/.operation`、`P.power-shelf.spec`、`P.cdu.spec/.operation`、`P.sidecar-hx.spec`、`P.room-cooling.spec`、`P.chiller.spec`、`P.rack-frame.spec`、`P.modular-dc.spec` |

仍缺的 3 行：

| 行 | 原因 |
|---|---|
| `P.switch-asic.spec` | 点名的 Spectrum-4 只有交换机系统页写了"Switch: Spectrum-4"，ASIC 自身规格在 resources.nvidia.com 的文档查看器里，不在适配器范围；Broadcom 等尚无适配器 |
| `P.manifold.spec` | Vertiv CoolChip 歧管页的规格表全是 N/A；CoolIT、nVent、Parker 尚无适配器 |
| `P.dcim.spec` | Vertiv Environet Alert 页没有规格表；Schneider、Nlyte、Sunbird 尚无适配器 |

第一次运行（同日，26 个种子）有 14 个没取到产品，逐一查明后修正：

- Vertiv 单型号页没有 HTML 表，规格只在"Print"按钮背后的 pdfmake 定义里 → 适配器按字面值读取（见 [Vertiv 适配器](adapters/vertiv.md)）。CDU 600、CDU 70、FF3175、SmartMod Max 由此取到。
- 5 个 Vertiv 页面是空壳（HTTP 200，只有站点导航）→ 换成在售页：HPL → EnergyCore Li5；en-us Monitored rPDU → en-latam 同款页；VR Rack2 → Vertiv Rack；PowerDirect 跳转页 → PowerDirect 3000 单型号页；Trinergy 与 CRV 删除（同行已有 EXL S1、DSE）。
- 空属性（`<div class>`）让 HTML 解析抛 `TypeError` → 解析器把无值属性当作空串。
- NVIDIA HGX、Grace 被识别为系列页而未计入 → 种子接受种子页上任何类型的产品，绑定仍要求有表。
- 新增 `P.bbu.operation`：EnergyCore Li7 锂电柜页（目标行点名"Vertiv … 锂电柜"）。

## 种子坏了怎么办

厂商改版后，种子报 `no_product` 或 `no_tables`。不要自动改写：人工找到在售的官方页，更新种子的 `url` 与 `reason`、`reviewed_at`，再跑 `collect-seeds --target <行>`。
