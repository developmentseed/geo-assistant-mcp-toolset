// The agent API's read routes, as this page uses them. Shapes follow
// mcp_agent_api/routes.py; only the fields the flow needs are typed.

export interface StateEntry {
  tool?: string | null;
  bytes?: number;
  seq?: number;
  /** Where each argument of the producing call came from: a state key, or "model". */
  inputs?: Record<string, string>;
  /** The turn that wrote this value, counted from 1. */
  turn?: number;
  /** How many turns hold a value for this key, when more than one. */
  turnsWritten?: number;
}

export interface TurnInfo {
  turn: number;
  question: string;
  state: Record<string, StateEntry>;
}

export interface TurnsResponse {
  threadId: string;
  turns: number;
  total: number;
  history: TurnInfo[];
}

export interface ThreadMessage {
  id: string;
  role: string;
  activityType?: string;
  content?: unknown;
}

export interface ThreadResponse {
  threadId: string;
  messages: ThreadMessage[];
  running: boolean;
}

/** An `mcp.view` activity: the view a tool call rendered and its structured data. */
export interface ViewContent {
  toolCallId: string;
  tool: string;
  uri: string;
  data: unknown;
}

export interface StateValue {
  key: string;
  tool: string | null;
  turn: number | null;
  value: unknown;
}

/** Thread ids are the only credential on these routes; they stay in the URL, never in logs. */
function base(threadId: string): string {
  return `/threads/${encodeURIComponent(threadId)}`;
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText} for ${path}`);
  }
  return (await res.json()) as T;
}

export function fetchTurns(threadId: string): Promise<TurnsResponse> {
  return getJson(`${base(threadId)}/turns`);
}

export function fetchThread(threadId: string): Promise<ThreadResponse> {
  return getJson(base(threadId));
}

/** One value in full. The key holds slashes, so each segment is encoded on its own. */
export function fetchStateValue(
  threadId: string,
  key: string,
  turn: number,
): Promise<StateValue> {
  const encoded = key.split("/").map(encodeURIComponent).join("/");
  return getJson(`${base(threadId)}/state/${encoded}?turn=${turn}`);
}

/** A view's HTML bundle, from a ui://<toolset>/<view> URI. */
export async function fetchViewHtml(uri: string): Promise<string> {
  const match = /^ui:\/\/([^/]+)\/(.+)$/.exec(uri);
  if (!match) {
    throw new Error(`not a ui:// view URI: ${uri}`);
  }
  const res = await fetch(
    `/views/${encodeURIComponent(match[1])}/${encodeURIComponent(match[2])}`,
  );
  if (!res.ok) {
    throw new Error(`${res.status} for view ${uri}`);
  }
  return res.text();
}
