import { parse as parseYaml } from "yaml";

export type HttpMethod =
  | "get"
  | "put"
  | "post"
  | "delete"
  | "options"
  | "head"
  | "patch"
  | "trace";

const HTTP_METHODS: HttpMethod[] = [
  "get",
  "put",
  "post",
  "delete",
  "options",
  "head",
  "patch",
  "trace",
];

export interface Endpoint {
  method: HttpMethod;
  path: string;
  summary?: string;
  operationId?: string;
  tags: string[];
  deprecated: boolean;
}

export interface SpecServer {
  url: string;
  description?: string;
}

export interface SpecSummary {
  title: string;
  version: string;
  description?: string;
  openApiVersion: string;
  servers: SpecServer[];
  endpoints: Endpoint[];
  endpointCount: number;
  methodCounts: Record<string, number>;
  tags: string[];
}

export class SpecParseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SpecParseError";
  }
}

/**
 * Accept either JSON or YAML text. OpenAPI documents are frequently authored in
 * YAML, and JSON is a subset of YAML, so a single YAML parse handles both while
 * still giving a clear error for genuinely malformed input.
 */
export function parseDocument(text: string): unknown {
  const trimmed = text.trim();
  if (trimmed.length === 0) {
    throw new SpecParseError("The document is empty.");
  }
  try {
    return parseYaml(trimmed);
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    throw new SpecParseError(`Could not parse document as JSON or YAML: ${detail}`);
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return {};
  }
  return value as Record<string, unknown>;
}

function coerceString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

export function summarizeSpec(doc: unknown): SpecSummary {
  const root = asRecord(doc);

  const openApiVersion =
    coerceString(root.openapi) ?? coerceString(root.swagger);
  if (!openApiVersion) {
    throw new SpecParseError(
      'Not an OpenAPI document: missing "openapi" (or "swagger") version field.',
    );
  }

  const info = asRecord(root.info);
  const title = coerceString(info.title) ?? "(untitled API)";
  const version = coerceString(info.version) ?? "(no version)";
  const description = coerceString(info.description);

  const servers: SpecServer[] = [];
  if (Array.isArray(root.servers)) {
    for (const entry of root.servers) {
      const server = asRecord(entry);
      const url = coerceString(server.url);
      if (!url) continue;
      const description = coerceString(server.description);
      servers.push(description ? { url, description } : { url });
    }
  }

  const paths = asRecord(root.paths);
  const endpoints: Endpoint[] = [];

  for (const [path, pathItemRaw] of Object.entries(paths)) {
    const pathItem = asRecord(pathItemRaw);
    for (const method of HTTP_METHODS) {
      const operationRaw = pathItem[method];
      if (operationRaw === undefined) continue;
      const operation = asRecord(operationRaw);
      const tags = Array.isArray(operation.tags)
        ? operation.tags.filter((t): t is string => typeof t === "string")
        : [];
      endpoints.push({
        method,
        path,
        summary: coerceString(operation.summary),
        operationId: coerceString(operation.operationId),
        tags,
        deprecated: operation.deprecated === true,
      });
    }
  }

  endpoints.sort((a, b) =>
    a.path === b.path
      ? a.method.localeCompare(b.method)
      : a.path.localeCompare(b.path),
  );

  const methodCounts: Record<string, number> = {};
  for (const endpoint of endpoints) {
    methodCounts[endpoint.method] = (methodCounts[endpoint.method] ?? 0) + 1;
  }

  const tags = Array.from(
    new Set(endpoints.flatMap((endpoint) => endpoint.tags)),
  ).sort((a, b) => a.localeCompare(b));

  return {
    title,
    version,
    description,
    openApiVersion,
    servers,
    endpoints,
    endpointCount: endpoints.length,
    methodCounts,
    tags,
  };
}

export function inspectSpec(text: string): SpecSummary {
  return summarizeSpec(parseDocument(text));
}
