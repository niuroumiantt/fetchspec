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
`max_response_seconds`（缺省为 `timeout_seconds × 3`）；超时记为 error。

只补失败项用 `--retry-errors`：把超时、连接/TLS 失败和 5xx 的 error 请求重新排队，
404、robots 明确禁止、主机白名单拦截、HTML 冒充文档等确定性结果不重试。它与 `--recheck`（全量重排）互斥。

robots.txt 在启动时遇到断连会重试 3 次。仍取不到时，该主机的请求记为 error（`robots unavailable for host`），
可以用 `--retry-errors` 补抓；只有 robots 明确禁止的请求才记为 blocked。旧台账里的 `robots missing or disallowed`
没有区分这两种情况，确认主机 robots 可用后，手工把对应的 blocked 改回 pending：

```bash
sqlite3 <data>/ledger/companies/<company>/crawl.sqlite \
  "update requests set state='pending',attempts=0 where state='blocked' and error like '%robots missing%' and url like 'https://<host>/%';"
```

停止 worker 用 `stop`（当前请求完成后退出）。急停可以直接 `kill -TERM <pid>`：worker 会把本轮记为
`interrupted_or_setup_error` 再退出，不会留下停在 running 的记录。

```bash
scripts/run-company.sh start nvidia --retry-errors
```

## 迁移数据目录

blob 按内容 SHA 命名，可以合并到已有数据目录。先 `stop`，确认 `status` 显示 `worker=stopped`，
再按公司复制 `blobs/`、`library/<company>/` 和 `ledger/companies/<company>/`；
不要覆盖目标上的 `ledger/catalog.json` 等其他公司共用文件。源目录保留到目标续跑确认后再单独处理。

## 交付包（inresearch 交付协议 v1）

```bash
PYTHONPATH=src python3 -m fetchspec company-deliver --company nvidia [--task <inresearch 任务 ID>]
```

在数据目录下生成 `deliveries/<delivery_id>/`：

- `manifest.json`：信封字段（`provider_id`、`delivery_id`、`task_id_or_discovery`、`collector_revision`、`items`），加上逐项的来源、获取时间、SHA、格式、完整性、使用范围和版本关系（原件 / 新版本及其取代的 SHA）
- `SHA256SUMS`：在包目录内运行 `shasum -a 256 -c SHA256SUMS` 核验
- `files/<2 hex>/<sha>.<kind>`：原件；同一个卷上用硬链接，跨卷时复制

打包时逐个重算 SHA。缺失或不一致的文件不进包，写进 `summary` 并以退出码 1 结束。按当前语言和范围规则已不会再采集的旧内容，也不进包。已交付的 SHA 记在台账的 `deliveries` 表，下次只打包新增内容；`--include-delivered` 可以全量重打。交付不等于验收：回执和研究采用在 inresearch 端完成。
