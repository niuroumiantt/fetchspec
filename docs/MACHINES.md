# 代码在 Git，原件在哪台机器

## 三台机器不是同一台

这次对话里的 `ubuntu` / 主机名 `cursor` 是 **Cursor Cloud Agent**（临时开发机），不是你的 AWS Lightsail，也不是阿里云。

| 角色 | 是什么 | 放什么 |
|---|---|---|
| GitHub | `fetchspec` / `inresearch.ai` 仓库 | 规则和代码 |
| 阿里云 | 你买的、磁盘更大的那台 | PDF / HTML 原件（`data_root`） |
| Spark | 研究阅读机 | 以后从阿里云搬过去 |
| AWS Lightsail | 你觉得空间小、延迟高的那台 | **不要**当资料盘 |
| Cursor Agent | 这次帮我跑命令的临时机 | 可以改代码、不能当你的资料盘 |

Agent **没有**你阿里云的 SSH 账号，所以我不能从这里直接把文件写进阿里云磁盘。能做的是：代码进 Git；你在阿里云上 clone 后跑抓取，原件只写阿里云本地盘。

## 在阿里云上怎么跑

```bash
git clone https://github.com/niuroumiantt/fetchspec.git
cd fetchspec
cp config/archive.example.json config/archive.local.json
# 把 data_root 改成这台阿里云上的目录，例如 /data/fetchspec
mkdir -p /data/fetchspec
PYTHONPATH=src python3 -m fetchspec where    # 应打印 /data/fetchspec
PYTHONPATH=src python3 -m fetchspec crawl --demo --fetch
```

`config/archive.local.json` 已 gitignore，不会进仓库。也可用环境变量：

```bash
export FETCHSPEC_DATA_ROOT=/data/fetchspec
```

优先级：`--out` > `FETCHSPEC_DATA_ROOT` > `config/archive.local.json` > `~/.local/share/fetchspec`

Spark 就绪后再从阿里云 rsync `library/` 和 `blobs/`，见 `docs/LAYOUT.md`。
