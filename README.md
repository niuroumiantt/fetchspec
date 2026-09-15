# fetchspec

Fetch, parse, and inspect OpenAPI / Swagger specifications right in the browser.

`fetchspec` is a small Vite + React + TypeScript app. Give it a spec URL (or
paste a JSON/YAML document) and it renders a clean summary: title, version,
servers, a per-method breakdown, and the full list of endpoints.

## Requirements

- Node.js 20+ (developed on Node 22)
- npm 10+

## Getting started

代码只在 GitHub。PDF 写在**执行 crawl 的那台机器**的磁盘上，不要写 Lightsail / Cursor Agent。请在阿里云上 clone 后配置 `config/archive.local.json` 再 `--fetch`。机器分工见 [docs/MACHINES.md](docs/MACHINES.md)。

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
