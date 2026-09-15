# fetchspec

声明式规则：从厂商官网抓取公开产品资料（产品页 + PDF），按 inresearch 的 `company_id` 和 `library_path` 归档。

本仓库只负责规则和有界执行器。原件按内容哈希保存，不进 Git。抓取结果不是研究采用结论。

## Demo 四站

按研究逻辑先覆盖计算，并保留一种电力/液冷资料库形态：

| 规则 | 公司 | 生态 | 官网形态 |
|---|---|---|---|
| `rules/nvidia.json` | NVIDIA | 计算 | 产品页 + DAM/resources PDF |
| `rules/intel.json` | Intel | 计算 | Xeon 6 产品页 + product brief PDF |
| `rules/supermicro.json` | SuperMicro | 计算 | GPU SuperServer datasheet 页 |
| `rules/vertiv.json` | Vertiv | 电力 + 冷却 | 产品目录 + globalassets PDF |

爬取单位是 **host + 路径规则**，不是「计算」这一只蜘蛛。生态字段只用来挑选任务。

## 命令

```bash
PYTHONPATH=src python3 -m fetchspec list --demo
PYTHONPATH=src python3 -m fetchspec validate --demo
PYTHONPATH=src python3 -m fetchspec crawl --demo
PYTHONPATH=src python3 -m unittest discover -s tests
```

默认 `crawl` 是 dry-run：只检查起始 URL 是否落在规则内，不访问网站。加 `--fetch` 才按 robots、间隔和条数上限下载。

```bash
PYTHONPATH=src python3 -m fetchspec crawl --rule nvidia --out ./out
PYTHONPATH=src python3 -m fetchspec crawl --rule nvidia --fetch --out ./out
```

## 规则字段

见 `schema/rule.schema.json`。每条规则必须有：`company_id`、允许的 host、起始 URL、路径包含/排除、产品线与 `library_path`。可选 `link_text_include` 用来在站内链接上过滤型号/datasheet 词。

落盘：

- `out/blobs/<2位>/<sha256>.pdf|html` 原始字节，相同内容只存一次
- `out/library/...` 按产品线路径的符号链接
- `out/runs/<run_id>.json` 本次清单（URL、状态、哈希、产品线）

## 边界

- 只抓规则列明的公开官网；遵守 robots.txt
- 默认延迟 ≥ 0.5 秒；Demo 规则为 1.5 秒
- 不登录、不绕过 gated DAM、不抓政府价目与搜索页
- 新闻中心默认不在 include 路径里
- NVIDIA 规则排除 InfiniBand / ConnectX / BlueField（网络生态下一波再做）
