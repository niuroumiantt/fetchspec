# 公司级抓取运维

抓取由 fetchspec 的 Python 代码执行（持久队列、robots、限速、去重都在 `src/fetchspec/company.py`），
不需要模型或 Spark 在场。每家公司一个 profile（`profiles/<company>.json`）；新增厂商写 profile，不改引擎。

## 日常命令

```bash
scripts/run-company.sh start  nvidia   # 后台启动；已在跑则不重复启动；断点续抓
scripts/run-company.sh status nvidia   # 队列、近 60 分钟速度、预计剩余时间、错误分类
scripts/run-company.sh log    nvidia   # 跟随最新日志（每行带 ts 的 JSON 进度）
scripts/run-company.sh stop   nvidia   # 写 STOP 文件，当前请求完成后干净退出
```

- 数据目录：`FETCHSPEC_DATA_ROOT` > `config/archive.local.json` > `~/.local/share/fetchspec`。
- 日志目录：`FETCHSPEC_STATE_ROOT`（默认 `~/.local/state/fetchspec`）下的 `<company>/`。
- `start` 期间用 `caffeinate -i` 防止休眠，进程结束即解除。
- `status --json` 输出机器可读摘要；`company-status` 不带 `--summary` 仍输出完整报告。
- 预计剩余时间按最近速度估算，只代表队列排空时间，不代表网站覆盖完整。

## 常驻（抓取主机 Macmini）

```bash
scripts/install-launchd.sh install   nvidia
scripts/install-launchd.sh uninstall nvidia
```

launchd 在崩溃、远端错误暂停或重启后自动续跑（间隔 600 秒）；`stop` 或队列排空时以 0 退出，不会被拉起。
安装时环境里的 `FETCHSPEC_DATA_ROOT`、`FETCHSPEC_STATE_ROOT` 会写进 plist。

## 速度与超时

速度主要受抓取机到厂商站点的带宽限制：PDF 优先出队，大文件阶段每小时只有一两百个请求，
进入 HTML 发现阶段后接近 `delay_seconds` 上限。单次传输总时长上限为 profile 的
`max_response_seconds`（缺省为 `timeout_seconds × 3`）；超时记为 error，可在更新时用 `--recheck` 重试。

## 迁移数据目录

blob 按内容 SHA 命名，可以合并到已有数据目录。先 `stop`，确认 `status` 显示 `worker=stopped`，
再按公司复制 `blobs/`、`library/<company>/` 和 `ledger/companies/<company>/`；
不要覆盖目标上的 `ledger/catalog.json` 等其他公司共用文件。源目录保留到目标续跑确认后再单独处理。

## 交付包（inresearch 交付协议 v1）

```bash
PYTHONPATH=src python3 -m fetchspec deliver --company nvidia               # 主动发现批次
PYTHONPATH=src python3 -m fetchspec deliver --company nvidia --task <任务ID> # 对应 inresearch 供应任务
```

在 `<数据目录>/deliveries/<时间>-<公司>/` 生成：`manifest.json`（交付信封）、`items.jsonl`（每个不重复内容一条，
含全部来源 URL、首次获取时间、SHA、格式、完整性、使用范围、版本关系）、`SHA256SUMS`、`blobs/`（从归档硬链接）
和 `library/`（指向包内 blobs 的相对链接）。已在之前交付包出现过的内容不重复打包；构建前逐个复核 SHA。
交付不等于验收或研究采用。送往 Spark 时只写 `incoming/<日期>-fetchspec/`，不加 `--delete`：

```bash
cd <数据目录>/deliveries/<交付ID> && shasum -a 256 -c SHA256SUMS
rsync -a <数据目录>/deliveries/<交付ID>/ spark:<incoming>/<日期>-fetchspec/<交付ID>/
```
