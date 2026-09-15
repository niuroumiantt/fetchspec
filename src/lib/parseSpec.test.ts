import { describe, expect, it } from "vitest";
import { inspectSpec, SpecParseError, summarizeSpec } from "./parseSpec";

const jsonSpec = JSON.stringify({
  openapi: "3.0.3",
  info: { title: "Widget API", version: "2.1.0", description: "Manage widgets." },
  servers: [{ url: "https://api.widgets.dev/v2", description: "Production" }],
  paths: {
    "/widgets": {
      get: { summary: "List widgets", operationId: "listWidgets", tags: ["widgets"] },
      post: { summary: "Create widget", tags: ["widgets"] },
    },
    "/widgets/{id}": {
      get: { summary: "Get widget", tags: ["widgets"] },
      delete: { summary: "Delete widget", tags: ["admin"], deprecated: true },
    },
  },
});

const yamlSpec = `
openapi: 3.1.0
info:
  title: Inventory API
  version: 0.9.0
paths:
  /items:
    get:
      summary: List items
`;

describe("inspectSpec", () => {
  it("summarizes a JSON OpenAPI document", () => {
    const summary = inspectSpec(jsonSpec);
    expect(summary.title).toBe("Widget API");
    expect(summary.version).toBe("2.1.0");
    expect(summary.openApiVersion).toBe("3.0.3");
    expect(summary.endpointCount).toBe(4);
    expect(summary.servers[0].url).toBe("https://api.widgets.dev/v2");
    expect(summary.methodCounts).toEqual({ get: 2, post: 1, delete: 1 });
    expect(summary.tags).toEqual(["admin", "widgets"]);
  });

  it("marks deprecated operations", () => {
    const summary = inspectSpec(jsonSpec);
    const del = summary.endpoints.find((e) => e.method === "delete");
    expect(del?.deprecated).toBe(true);
  });

  it("parses YAML documents too", () => {
    const summary = inspectSpec(yamlSpec);
    expect(summary.title).toBe("Inventory API");
    expect(summary.openApiVersion).toBe("3.1.0");
    expect(summary.endpointCount).toBe(1);
  });

  it("throws on non-OpenAPI JSON", () => {
    expect(() => summarizeSpec({ hello: "world" })).toThrow(SpecParseError);
  });

  it("throws on empty input", () => {
    expect(() => inspectSpec("   ")).toThrow(SpecParseError);
  });
});
