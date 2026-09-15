# fetchspec

Fetch, parse, and inspect OpenAPI / Swagger specifications right in the browser.

`fetchspec` is a small Vite + React + TypeScript app. Give it a spec URL (or
paste a JSON/YAML document) and it renders a clean summary: title, version,
servers, a per-method breakdown, and the full list of endpoints.

## Requirements

- Node.js 20+ (developed on Node 22)
- npm 10+

## Getting started

```bash
npm install       # install dependencies
npm run dev       # start the dev server at http://localhost:5173
```

A bundled sample spec (Swagger Petstore) lives at `public/samples/petstore.json`
and is pre-filled in the URL box, so the app works fully offline out of the box.

## Scripts

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
