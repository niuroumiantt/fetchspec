import { useCallback, useMemo, useState } from "react";
import { inspectSpec, type SpecSummary } from "./lib/parseSpec";
import { SummaryView } from "./components/SummaryView";

const SAMPLE_URL = `${import.meta.env.BASE_URL}samples/petstore.json`;

type Status =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ok"; summary: SpecSummary }
  | { kind: "error"; message: string };

export default function App() {
  const [text, setText] = useState("");
  const [url, setUrl] = useState(SAMPLE_URL);
  const [status, setStatus] = useState<Status>({ kind: "idle" });

  const inspect = useCallback((raw: string) => {
    try {
      const summary = inspectSpec(raw);
      setStatus({ kind: "ok", summary });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setStatus({ kind: "error", message });
    }
  }, []);

  const handleInspect = useCallback(() => {
    inspect(text);
  }, [inspect, text]);

  const handleFetch = useCallback(async () => {
    setStatus({ kind: "loading" });
    try {
      const res = await fetch(url);
      if (!res.ok) {
        throw new Error(`Request failed: ${res.status} ${res.statusText}`);
      }
      const raw = await res.text();
      setText(raw);
      inspect(raw);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setStatus({ kind: "error", message });
    }
  }, [inspect, url]);

  const canInspect = useMemo(() => text.trim().length > 0, [text]);

  return (
    <div className="app">
      <header className="app__header">
        <h1>
          fetch<span>spec</span>
        </h1>
        <p>Fetch, parse, and inspect any OpenAPI / Swagger specification.</p>
      </header>

      <section className="panel">
        <div className="row">
          <input
            className="row__input"
            type="text"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://example.com/openapi.json"
            aria-label="Spec URL"
          />
          <button
            className="btn btn--primary"
            onClick={handleFetch}
            disabled={status.kind === "loading"}
          >
            {status.kind === "loading" ? "Fetching…" : "Fetch"}
          </button>
        </div>

        <div className="row row--hint">
          <span>or paste a JSON / YAML document below</span>
        </div>

        <textarea
          className="editor"
          value={text}
          spellCheck={false}
          onChange={(e) => setText(e.target.value)}
          placeholder="Paste an OpenAPI document here…"
          aria-label="Spec document"
        />

        <div className="row row--actions">
          <button
            className="btn btn--primary"
            onClick={handleInspect}
            disabled={!canInspect}
          >
            Inspect
          </button>
          <button
            className="btn"
            onClick={() => {
              setText("");
              setStatus({ kind: "idle" });
            }}
          >
            Clear
          </button>
        </div>
      </section>

      <section className="results">
        {status.kind === "idle" && (
          <p className="placeholder">
            Enter a URL and click <strong>Fetch</strong>, or paste a spec and
            click <strong>Inspect</strong>.
          </p>
        )}
        {status.kind === "error" && (
          <div className="alert" role="alert">
            {status.message}
          </div>
        )}
        {status.kind === "ok" && <SummaryView summary={status.summary} />}
      </section>
    </div>
  );
}
