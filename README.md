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

代码只在 GitHub。PDF 写在**执行 crawl 的那台机器**的磁盘上，不要写 Lightsail / Cursor Agent。请在阿里云上 clone 后配置 `config/archive.local.json` 再 `--fetch`。机器分工见 [docs/MACHINES.md](docs/MACHINES.md)。

```text
blobs/<sha256>                          唯一原件
library/<表>/<公司>/<产品线>/<型号>/     与 inresearch product/library 同形
ledger/catalog.json                     每份文件的对应键（待 Spark 发 doc_id）
```

文件名：`<company_id>__<model>__<DS|PB|WEB>__vNA__<日期>__en__<sha8>.pdf`

优先级：`--out` > `FETCHSPEC_DATA_ROOT` > `config/archive.local.json` > `~/.local/share/fetchspec`

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
