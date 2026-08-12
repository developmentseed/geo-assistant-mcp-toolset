// Table view for `query`: its rows as a scrollable, sticky-header table.
import { onData } from "@developmentseed/mcp-view";

import "./styles.css";

// Mirror of QueryResult in tools.py — the tool's structuredContent.
interface QueryResult {
  message?: string;
  rows?: Record<string, unknown>[];
  row_count?: number;
}

const root = document.getElementById("root")!;

function columnsOf(rows: Record<string, unknown>[]): string[] {
  // Union of row keys in first-appearance order: rows are JSON records, so a
  // column missing from row one (NULL pruned upstream) can still appear later.
  const seen = new Set<string>();
  for (const row of rows) {
    for (const key of Object.keys(row)) seen.add(key);
  }
  return [...seen];
}

function isNumeric(rows: Record<string, unknown>[], column: string): boolean {
  let hasValue = false;
  for (const row of rows) {
    const value = row[column];
    if (value == null) continue;
    if (typeof value !== "number") return false;
    hasValue = true;
  }
  return hasValue;
}

function cellText(value: unknown): string {
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function render(data: QueryResult): void {
  root.textContent = "";
  const view = document.createElement("div");
  view.className = "view";
  root.append(view);

  const rows = data.rows ?? [];
  if (!rows.length) {
    const note = document.createElement("p");
    note.className = "view-note";
    note.textContent = data.message ?? "No rows returned.";
    view.append(note);
    return;
  }

  const columns = columnsOf(rows);
  const numeric = new Set(columns.filter((c) => isNumeric(rows, c)));

  const scroll = document.createElement("div");
  scroll.className = "table-scroll";
  const table = document.createElement("table");
  const head = table.createTHead().insertRow();
  for (const column of columns) {
    const th = document.createElement("th");
    th.textContent = column;
    if (numeric.has(column)) th.className = "num";
    head.append(th);
  }
  const body = table.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    for (const column of columns) {
      const td = tr.insertCell();
      const value = row[column];
      if (numeric.has(column)) td.className = "num";
      if (value == null) {
        const span = document.createElement("span");
        span.className = "null";
        span.textContent = "—";
        td.append(span);
      } else {
        const text = cellText(value);
        td.textContent = text;
        td.title = text;
      }
    }
  }
  scroll.append(table);
  view.append(scroll);

  const note = document.createElement("p");
  note.className = "view-note";
  note.textContent = `${data.row_count ?? rows.length} row(s) × ${columns.length} column(s)`;
  view.append(note);
}

onData<QueryResult>(render);
