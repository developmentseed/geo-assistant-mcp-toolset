import type { Edge, Node } from "@xyflow/react";
import type { ThreadMessage, TurnsResponse, ViewContent } from "./api";

/** The input value that the model wrote itself; every other input names a state key. */
const MODEL = "model";

export interface ViewRef {
  toolCallId: string;
  uri: string;
  data: unknown;
}

export interface ValueData extends Record<string, unknown> {
  kind: "value";
  key: string;
  tool: string | null;
  turn: number;
  bytes?: number;
  /** The turn that wrote this key again, when this version is no longer current. */
  replacedIn?: number;
}

export interface CallData extends Record<string, unknown> {
  kind: "call";
  tool: string;
  turn: number;
  /** State keys this call published. */
  keys: string[];
  views: ViewRef[];
}

export interface ModelData extends Record<string, unknown> {
  kind: "model";
  turn: number;
}

export type FlowNodeData = ValueData | CallData | ModelData;
export type FlowNode = Node<FlowNodeData>;

export interface Flow {
  nodes: FlowNode[];
  edges: Edge[];
}

/** One write to a state key, at the turn that wrote it. */
interface Version {
  key: string;
  turn: number;
  tool: string | null;
  bytes?: number;
  inputs: Record<string, string>;
}

const versionId = (key: string, turn: number) => `val:${key}#${turn}`;
const callId = (turn: number, tool: string) => `call:${turn}:${tool}`;
const modelId = (turn: number) => `model:${turn}`;

/**
 * Turn the per-turn state snapshots and the thread's view activities into one
 * graph. A state key becomes a value node per write. A tool call becomes a
 * call node per (turn, tool). Each input that came from a state key becomes an
 * edge from that value into the call. A call that rendered a view carries the
 * view, so the canvas can open it.
 *
 * A (turn, tool) node can merge several calls to one tool in one turn. The
 * state panel does not say which call wrote which key, so neither can this.
 */
export function buildFlow(turns: TurnsResponse, messages: ThreadMessage[]): Flow {
  const versions = collectVersions(turns);
  const versionsByKey = new Map<string, Version[]>();
  for (const v of versions) {
    const list = versionsByKey.get(v.key) ?? [];
    list.push(v);
    versionsByKey.set(v.key, list);
  }
  for (const list of versionsByKey.values()) {
    list.sort((a, b) => a.turn - b.turn);
  }

  interface CallAcc {
    tool: string;
    turn: number;
    keys: string[];
    views: ViewRef[];
    /** Input edges, one per (param, source) pair. */
    inputs: { param: string; source: string }[];
  }
  const calls = new Map<string, CallAcc>();
  const callAt = (turn: number, tool: string): CallAcc => {
    const id = callId(turn, tool);
    let call = calls.get(id);
    if (!call) {
      call = { tool, turn, keys: [], views: [], inputs: [] };
      calls.set(id, call);
    }
    return call;
  };

  for (const v of versions) {
    const call = callAt(v.turn, v.tool ?? "unknown");
    call.keys.push(v.key);
    for (const [param, source] of Object.entries(v.inputs)) {
      call.inputs.push({ param, source });
    }
  }

  // A turn starts at each user message, the same count the state snapshots use.
  let turn = 0;
  for (const message of messages) {
    if (message.role === "user") {
      turn += 1;
    } else if (message.role === "activity" && message.activityType === "mcp.view") {
      const view = message.content as ViewContent;
      callAt(turn, view.tool).views.push({
        toolCallId: view.toolCallId,
        uri: view.uri,
        data: view.data,
      });
    }
  }

  const nodes: FlowNode[] = [];
  const edges: Edge[] = [];
  const modelTurns = new Set<number>();

  for (const v of versions) {
    const replaced = (versionsByKey.get(v.key) ?? []).find((other) => other.turn > v.turn);
    nodes.push({
      id: versionId(v.key, v.turn),
      type: "value",
      position: { x: 0, y: 0 },
      data: {
        kind: "value",
        key: v.key,
        tool: v.tool,
        turn: v.turn,
        bytes: v.bytes,
        replacedIn: replaced?.turn,
      },
    });
  }

  for (const [id, call] of calls) {
    nodes.push({
      id,
      type: "call",
      position: { x: 0, y: 0 },
      data: {
        kind: "call",
        tool: call.tool,
        turn: call.turn,
        keys: call.keys,
        views: call.views,
      },
    });

    for (const key of call.keys) {
      const produced = versionId(key, call.turn);
      edges.push({ id: `${id}->${produced}`, source: id, target: produced, type: "smoothstep" });
    }

    for (const { param, source } of call.inputs) {
      const from = resolveSource(source, call.turn, versionsByKey, modelTurns);
      if (from) {
        edges.push({
          id: `${from}->${id}:${param}`,
          source: from,
          target: id,
          label: param,
          type: "smoothstep",
        });
      }
    }
  }

  for (const t of modelTurns) {
    nodes.push({
      id: modelId(t),
      type: "model",
      position: { x: 0, y: 0 },
      data: { kind: "model", turn: t },
    });
  }

  const unique = dedupeEdges(edges);
  return { nodes: layout(nodes, unique), edges: unique };
}

function collectVersions(turns: TurnsResponse): Version[] {
  const seen = new Map<string, Version>();
  for (const snapshot of [...turns.history].sort((a, b) => a.turn - b.turn)) {
    for (const [key, entry] of Object.entries(snapshot.state)) {
      const at = entry.turn ?? snapshot.turn;
      const id = versionId(key, at);
      if (!seen.has(id)) {
        seen.set(id, {
          key,
          turn: at,
          tool: entry.tool ?? null,
          bytes: entry.bytes,
          inputs: entry.inputs ?? {},
        });
      }
    }
  }
  return [...seen.values()];
}

/**
 * The version an input read: the latest write of `source` before `turn`. A
 * write in the same turn counts only when no earlier one exists, since the
 * panel does not say which call came first within a turn.
 */
function resolveSource(
  source: string,
  turn: number,
  versionsByKey: Map<string, Version[]>,
  modelTurns: Set<number>,
): string | undefined {
  if (source === MODEL) {
    modelTurns.add(turn);
    return modelId(turn);
  }
  const writes = versionsByKey.get(source) ?? [];
  const chosen =
    writes.filter((v) => v.turn < turn).at(-1) ?? writes.find((v) => v.turn === turn);
  return chosen ? versionId(chosen.key, chosen.turn) : undefined;
}

function dedupeEdges(edges: Edge[]): Edge[] {
  const seen = new Set<string>();
  return edges.filter((edge) => {
    if (seen.has(edge.id)) return false;
    seen.add(edge.id);
    return true;
  });
}

/** Node heights, for stacking a column. Kept in step with the node styles. */
const HEIGHT: Record<FlowNodeData["kind"], number> = {
  value: 96,
  call: 120,
  model: 56,
};

/** A call with a view also holds its preview, so its column needs more room. */
function heightOf(node: FlowNode): number {
  if (node.data.kind === "call" && node.data.views.length > 0) return 380;
  return HEIGHT[node.data.kind];
}

const COLUMN_GAP = 400;
const ROW_GAP = 32;

/**
 * Lay nodes out in columns, one per longest path from a source. Inputs read
 * left to right, in the order the turns used them. Within a column, nodes sort
 * by turn so each turn stays together.
 */
function layout(nodes: FlowNode[], edges: Edge[]): FlowNode[] {
  const parents = new Map<string, string[]>();
  for (const edge of edges) {
    const list = parents.get(edge.target) ?? [];
    list.push(edge.source);
    parents.set(edge.target, list);
  }

  const rank = new Map<string, number>();
  const rankOf = (id: string, trail: Set<string>): number => {
    const known = rank.get(id);
    if (known !== undefined) return known;
    if (trail.has(id)) return 0; // state cannot form a cycle; guard anyway.
    trail.add(id);
    const up = parents.get(id) ?? [];
    const r = up.length === 0 ? 0 : 1 + Math.max(...up.map((p) => rankOf(p, trail)));
    trail.delete(id);
    rank.set(id, r);
    return r;
  };
  for (const node of nodes) rankOf(node.id, new Set());

  const columns = new Map<number, FlowNode[]>();
  for (const node of nodes) {
    const r = rank.get(node.id) ?? 0;
    const column = columns.get(r) ?? [];
    column.push(node);
    columns.set(r, column);
  }

  for (const [r, column] of columns) {
    column.sort((a, b) => a.data.turn - b.data.turn || a.id.localeCompare(b.id));
    let y = 0;
    for (const node of column) {
      node.position = { x: r * COLUMN_GAP, y };
      y += heightOf(node) + ROW_GAP;
    }
  }
  return nodes;
}
