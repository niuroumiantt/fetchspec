# 申请：fetchspec 的回流接口（2026-10-01）

**向 inresearch.ai 申请**一个只读接口，按目标行告诉 Fetchspec "交过去的东西到了哪一步、哪些行还缺"。这是 [框架](../framework-2026-09-30.md) 第六节五个接口里唯一没接的"回流"。形状照抄 inresearch 2026-10-01 给新闻队做的回流：公开 `/api/news` 的 `by_target`（inresearch #298、#299；inews.today #136 已在读）。

## 为什么要

Fetchspec 现在只看得到自己这一侧：绑定了什么、打了什么包、拿到了哪些回执。看不到的有三件事：

1. 交过去的包在 inresearch 收下之后，目标行有没有被作者登记成 delivered；
2. 哪些行已经被研究侧采用成 sourced，不必再抓；
3. 哪些行收到了东西但还缺，例如只收到一家厂商，而目标行点名了五家。

没有回流，`plan` 只能按我们自己的快照排队，会重复抓已经够了的行，也看不出"收到了但还没登记"的行。

## 接口

```
GET https://inresearch.ai/api/targets/backflow?team=fetchspec
```

建议做成通用的 `team` 参数，以后 fetchstat、fetchfilings、fetchreports、fetchquotes 直接复用，新闻队也可以迁过来。和 `/api/news` 一样放进公开只读白名单（`interfaces/public.py` 的 `READER_API`）：内容只有目标行 ID 与计数，目标行 ID 本来就在公开的目标表里，不含原件、文件名、URL 或研究结论。

```json
{
  "schema_version": 1,
  "team": "fetchspec",
  "generated_at": "2026-10-01T09:00:00Z",
  "targets_sha256": "9bcb56d8fe639020…",
  "by_target": {
    "P.gpu.spec":   {"status": "delivered", "received_items": 2, "last_received_at": "2026-10-01T08:12:00Z", "companies": ["nvidia"]},
    "P.ups.spec":   {"status": "needed",    "received_items": 1, "last_received_at": null,                  "companies": ["vertiv"]},
    "P.server.spec":{"status": "needed",    "received_items": 0, "companies": []}
  }
}
```

| 字段 | 含义 | inresearch 里的来源 |
|---|---|---|
| `status` | 目标行四态 | `framework/tco_targets.json` 的 `status`（Git） |
| `received_items` | 接收端为这一行收下的原件数，只计生产接收 | 运行库 `data/raw/supply-center/receipts.json`：每个 delivery 的 `items[].target_ids` 与 `status`；`/api/supply` 的 `snapshot()` 已在读这个文件 |
| `last_received_at` | 最近一次收下的时间；没有就是 `null` | 接收台账 `acquisition/catalog.sqlite` 的 `product_documents.received_at`。`receipts.json` 现在没有时间戳，建议 `fetchspec-receive` 在每个 delivery 记录里加一个 `received_at` |
| `companies` | 这一行已收到哪些厂商的资料 | delivery 记录的 `company_id` |
| `targets_sha256` | 生成时用的目标表指纹，便于双方确认同一版骨架 | `framework/tco_targets.json` 的 SHA-256 |

规则：

- 只列 `team == fetchspec` 的行；`team` 不是已登记的队时返回 400。
- 每一行都给出，`received_items` 为 0 的也给，这样"缺什么"一眼可见。
- 只是计数，不是采用；`status` 照抄目标表，不在接口里推断。
- 读不到台账时返回 503，与 `/api/news` 一致，不返回半截数据。

## Fetchspec 这边已经做好

`plan --backflow <文件或 https URL>` 已能读这个格式（`src/fetchspec/coverage.py` 的 `load_backflow`，测试在 `tests/test_parameters_and_plan.py`）。读入时：

- `team` 不是 `fetchspec` 或 `schema_version` 不是 1，整份拒绝；
- 形状不对的行、不在当前快照里的行逐行丢弃并计数，做法与 inresearch 对 `/api/news` 的处理相同；
- `status` 为 delivered、sourced、assumed 的行不再排队；
- `status` 仍为 needed 但 `received_items` 大于 0 的行排到最前，动作是"已收到，等作者 `deliveries import`"。

接口上线后，在 macmini 上这样用：

```bash
python3 -m fetchspec.pipeline plan --backflow 'https://inresearch.ai/api/targets/backflow?team=fetchspec'
```

## 一并请 inresearch 处理

- 更新 `web/pages/admin/fetchspec/reporg.html`：Fetchspec 的架构页现在只印目标表最后一次改动的 commit，inresearch 的无关提交不再让它过期。从本仓库 `public/admin/reporg.html` 复制覆盖即可。
- [inresearch-delivered-scope.patch](inresearch-delivered-scope.patch)：规格事件卡连带把同部件的新闻行翻成 delivered 的问题，仍待合入。回流接上之后，这个问题会直接表现为错误的 `status`。
