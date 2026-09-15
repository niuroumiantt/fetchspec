# fetchspec

声明式规则：从厂商官网抓取公开产品资料，按 inresearch 的 `company_id` + `product_line` 在**本机**归档。当前环境与 Spark 隔离，原件先留在本机；Spark 就绪后再按 `docs/LAYOUT.md` 整树搬迁，不在云会话里假装已经进研究库。

本仓库只含规则和执行器。原件、哈希库、清单不进 Git。抓取结果不是 C3 采用。

## Demo 四站

| 规则 | 公司 | 生态 |
|---|---|---|
| `rules/nvidia.json` | NVIDIA | 计算 |
| `rules/intel.json` | Intel | 计算 |
| `rules/supermicro.json` | SuperMicro | 计算 |
| `rules/vertiv.json` | Vertiv | 电力 + 冷却 |

## 本机落盘

默认根目录在**跑 crawl 的那台机器**上：`~/.local/share/fetchspec/`（可用 `FETCHSPEC_DATA_ROOT` 或 `--out` 覆盖）。

这不是 GitHub 上的路径，也不会出现在你笔记本的访达里。Cloud Agent 跑出来的文件在 Agent 虚拟机的 `/home/ubuntu/.local/share/fetchspec/`。Cursor 工作区打开的是 git 仓库，默认看不到家目录；本仓库用 gitignore 的 `.data/archive/` 作为工作区里的只读副本入口（仍不进 Git）。

```text
blobs/<sha256>                          唯一原件
library/<表>/<公司>/<产品线>/<型号>/     与 inresearch product/library 同形
ledger/catalog.json                     每份文件的对应键（待 Spark 发 doc_id）
```

文件名：`<company_id>__<model>__<DS|PB|WEB>__vNA__<日期>__en__<sha8>.pdf`

```bash
PYTHONPATH=src python3 -m fetchspec where
PYTHONPATH=src python3 -m fetchspec list --demo
PYTHONPATH=src python3 -m fetchspec crawl --demo
PYTHONPATH=src python3 -m fetchspec crawl --demo --fetch
PYTHONPATH=src python3 -m unittest discover -s tests
```

默认 `crawl` 为 dry-run。`--fetch` 才下载到本机归档根。

## 边界

- 遵守 robots；Demo 间隔 1.5 秒；不登录、不绕 gated
- NVIDIA 规则排除 InfiniBand / ConnectX / BlueField
- Spark 搬迁见 `docs/LAYOUT.md`
