import type { SpecSummary } from "../lib/parseSpec";

const METHOD_ORDER = ["get", "post", "put", "patch", "delete", "options", "head", "trace"];

export function SummaryView({ summary }: { summary: SpecSummary }) {
  const methods = Object.keys(summary.methodCounts).sort(
    (a, b) => METHOD_ORDER.indexOf(a) - METHOD_ORDER.indexOf(b),
  );

  return (
    <div className="summary" data-testid="summary">
      <div className="summary__head">
        <div>
          <h2>{summary.title}</h2>
          <div className="summary__meta">
            <span className="badge">v{summary.version}</span>
            <span className="badge badge--muted">
              OpenAPI {summary.openApiVersion}
            </span>
            <span className="badge badge--muted">
              {summary.endpointCount} endpoints
            </span>
          </div>
        </div>
      </div>

      {summary.description && (
        <p className="summary__desc">{summary.description}</p>
      )}

      {summary.servers.length > 0 && (
        <div className="summary__section">
          <h3>Servers</h3>
          <ul className="servers">
            {summary.servers.map((server) => (
              <li key={server.url}>
                <code>{server.url}</code>
                {server.description && <span> — {server.description}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="summary__section">
        <h3>Method breakdown</h3>
        <div className="chips">
          {methods.map((method) => (
            <span key={method} className={`chip chip--${method}`}>
              {method.toUpperCase()} · {summary.methodCounts[method]}
            </span>
          ))}
        </div>
      </div>

      <div className="summary__section">
        <h3>Endpoints</h3>
        <table className="endpoints">
          <tbody>
            {summary.endpoints.map((endpoint) => (
              <tr key={`${endpoint.method}-${endpoint.path}`}>
                <td>
                  <span className={`chip chip--${endpoint.method}`}>
                    {endpoint.method.toUpperCase()}
                  </span>
                </td>
                <td className="endpoints__path">
                  <code>{endpoint.path}</code>
                  {endpoint.deprecated && (
                    <span className="tag tag--deprecated">deprecated</span>
                  )}
                </td>
                <td className="endpoints__summary">{endpoint.summary ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
