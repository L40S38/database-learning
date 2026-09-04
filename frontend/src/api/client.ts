export interface StepResult {
  label: string;
  description: string;
  sql: string;
  columns: string[] | null;
  rows: unknown[][] | null;
  row_count: number | null;
  error: string | null;
  highlight_columns: string[];
}

export interface TablePreview {
  table: string;
  alias: string | null;
  columns: string[] | null;
  rows: unknown[][] | null;
  row_count: number | null;
  error: string | null;
}

export interface SourceTables {
  tables: TablePreview[];
  highlightColumns: string[];
}

interface QueryStepsResponse {
  steps: StepResult[];
}

interface SourceTablesResponse {
  tables: TablePreview[];
  highlight_columns: string[];
}

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

async function postJson<T>(path: string, sql: string): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sql }),
  });

  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const message = body?.detail ?? `リクエストに失敗しました (${res.status})`;
    throw new Error(message);
  }

  return res.json() as Promise<T>;
}

export async function fetchQuerySteps(sql: string): Promise<StepResult[]> {
  const data = await postJson<QueryStepsResponse>("/api/query/steps", sql);
  return data.steps;
}

export async function fetchSourceTables(sql: string): Promise<SourceTables> {
  const data = await postJson<SourceTablesResponse>("/api/query/source-tables", sql);
  return { tables: data.tables, highlightColumns: data.highlight_columns };
}
