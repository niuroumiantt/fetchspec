# 代码在 Git，原件在哪台机器

## 这些机器不是同一台

| 角色 | 是什么 | 放什么 |
|---|---|---|
| GitHub | `fetchspec` / `inresearch.ai` 仓库 | 规则和代码，**没有** PDF |
| 抓取盘 | 一台会长期留文件的 Linux（以前用过阿里云；**重置后那台盘作废**） | `FETCHSPEC_DATA_ROOT` 下的 blobs / library / ledger |
| Spark | 研究阅读机 `spark@100.100.1.2` | 只接收已分拣要入库的原件；见 inresearch Spark 手册的 `incoming/` → `raw-materials/` |
| infra | 机房与发布仓库 | 域名、compose、密钥骨架；临时队列不是研究档案 |
| AWS 网站机 | inresearch 网页 | 派生快照，不放规格原件 |
| AWS Lightsail | 小站 | **不要**当资料盘 |
| Cursor Agent | 临时开发机（主机名常为 `cursor`） | 可以改代码、试跑；**不能**当你的资料盘 |
| macmini | 已授权浏览器登录态 | 登录墙采集；Cookie 不搬到本爬虫 |
| m4 | 人 | 上传、核对、拍板 |

Agent **没有**你私人 VPS 的 SSH。这里跑 `--fetch` 只会写这台临时盘。要留住文件：在你自己的抓取机 clone 后跑，或立刻 `rsync` 到那台机。

阿里云重置 ≠ 项目停摆。重买或改用 Spark 上的隔离目录 / NAS 即可；Git 规则仍在，PDF 必须重抓（除非 Spark 当时已经收过同一 SHA）。

## 在抓取机上怎么跑

```bash
git clone https://github.com/niuroumiantt/fetchspec.git
cd fetchspec
cp config/archive.example.json config/archive.local.json
# data_root 改成本机长期目录，例如 /data/fetchspec
mkdir -p /data/fetchspec
PYTHONPATH=src python3 -m fetchspec where    # 应打印 /data/fetchspec
PYTHONPATH=src python3 -m fetchspec crawl --demo --fetch
```

`config/archive.local.json` 已 gitignore。也可用：

```bash
export FETCHSPEC_DATA_ROOT=/data/fetchspec
```

优先级：`--out` > `FETCHSPEC_DATA_ROOT` > `config/archive.local.json` > `~/.local/share/fetchspec`

交给 Spark 时 rsync `library/` 与 `blobs/` 到 `incoming/<日期>-fetchspec/`，再分拣提升。不要 `--delete`，不要覆盖已有 product library 索引。见 [LAYOUT.md](LAYOUT.md) 与 [FEEDERS.md](FEEDERS.md)。
