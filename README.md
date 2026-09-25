# fetchspec

Fetch, parse, and inspect OpenAPI / Swagger specifications right in the browser.

`fetchspec` is a small Vite + React + TypeScript app. Give it a spec URL (or
paste a JSON/YAML document) and it renders a clean summary: title, version,
servers, a per-method breakdown, and the full list of endpoints.

## Requirements

- Node.js 20+ (developed on Node 22)
- npm 10+

## Getting started

2026-09-23：公司级 Supermicro 批量归档与 Macmini 运行方式见 [docs/SUPERMICRO.md](docs/SUPERMICRO.md)。
该入口支持持久队列、全部产品 sitemap、PDF/Word/Excel 内容识别和不可变原件；历史 `crawl --demo` 仍仅为小样。
NVIDIA 首轮公司级公开产品/资源规则见 [docs/NVIDIA.md](docs/NVIDIA.md)；只获取英文与中文附件，其采集适配器与 Supermicro 共用队列、robots、内容哈希和版本台账。

官网规格爬虫：代码只在 GitHub。PDF 写在**执行 crawl 的长期抓取盘**上（阿里云若已重置就换新盘重抓），不要写 Lightsail / Cursor Agent。机器与投递见 [docs/MACHINES.md](docs/MACHINES.md)、[docs/FEEDERS.md](docs/FEEDERS.md)。本仓库里的 Vite OpenAPI 查看器是另一产品，与爬虫无关。

```text
blobs/<sha256>                          唯一原件
library/<表>/<公司>/<产品线>/<型号>/     与 inresearch product/library 同形
ledger/catalog.json                     每份文件的对应键（待 Spark 发 doc_id）
```

A bundled sample spec (Swagger Petstore) lives at `public/samples/petstore.json`
and is pre-filled in the URL box, so the app works fully offline out of the box.

优先级：`--out` > `FETCHSPEC_DATA_ROOT` > `config/archive.local.json` > `~/.local/share/fetchspec`

```bash
PYTHONPATH=src python3 -m fetchspec where
PYTHONPATH=src python3 -m fetchspec list --demo
PYTHONPATH=src python3 -m fetchspec crawl --demo
PYTHONPATH=src python3 -m fetchspec crawl --demo --fetch
PYTHONPATH=src python3 -m unittest discover -s tests
```

| Command             | Description                                   |
| ------------------- | --------------------------------------------- |
| `npm run dev`       | Start the Vite dev server.                    |
| `npm run build`     | Type-check and build a production bundle.     |
| `npm run preview`   | Preview the production build.                 |
| `npm run lint`      | Run ESLint.                                   |
| `npm run typecheck` | Type-check without emitting output.           |
| `npm test`          | Run the Vitest unit tests.                    |

## Project layout

```
src/
  App.tsx                # top-level UI: fetch / paste / inspect
  components/
    SummaryView.tsx      # renders the parsed spec summary
  lib/
    parseSpec.ts         # JSON+YAML parsing and OpenAPI summarization
    parseSpec.test.ts    # unit tests for the parser
public/
  samples/petstore.json  # bundled offline sample spec
```

## Cloud Agent environment

`.cursor/environment.json` configures the Cursor Cloud Agent environment:
`npm ci` installs dependencies and a `dev` terminal runs the Vite dev server.
