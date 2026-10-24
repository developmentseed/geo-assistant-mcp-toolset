import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  Controls,
  MiniMap,
  Panel,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Edge,
  type ReactFlowInstance,
} from "@xyflow/react";
import { fetchStateValue, fetchThread, fetchTurns, type StateValue } from "./api";
import { buildFlow, type CallData, type FlowNode } from "./graph";
import { FlowActionsContext, nodeTypes, type FlowActions } from "./FlowNodes";
import { ViewModal } from "./ViewModal";

/** How often the page re-reads the thread while it is open. */
const POLL_MS = 3000;

/** The thread id from `?thread=`, which the chat page writes into its own URL. */
function threadFromUrl(): string {
  return new URLSearchParams(window.location.search).get("thread") ?? "";
}

interface Stats {
  turns: number;
  total: number;
  running: boolean;
  updated: string;
}

interface Inspect {
  key: string;
  turn: number;
}

export function App() {
  const [threadId, setThreadId] = useState(threadFromUrl);
  const [draft, setDraft] = useState(threadFromUrl);
  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [inspect, setInspect] = useState<Inspect | null>(null);
  const [inspected, setInspected] = useState<StateValue | null>(null);
  const [inspectError, setInspectError] = useState<string | null>(null);
  const [view, setView] = useState<{ tool: string; view: CallData["views"][number] } | null>(null);
  const [canvas, setCanvas] = useState<ReactFlowInstance<FlowNode, Edge> | null>(null);
  const fittedFor = useRef("");

  // Frame the graph once per thread, when its first nodes arrive; later polls leave the view alone.
  useEffect(() => {
    if (canvas && nodes.length > 0 && fittedFor.current !== threadId) {
      fittedFor.current = threadId;
      void canvas.fitView({ padding: 0.15 });
    }
  }, [canvas, nodes.length, threadId]);

  const load = useCallback(
    async (id: string) => {
      if (!id) return;
      try {
        const [turns, thread] = await Promise.all([fetchTurns(id), fetchThread(id)]);
        const flow = buildFlow(turns, thread.messages);
        // Keep positions the reader has dragged; a fresh read only adds or removes nodes.
        setNodes((previous) => {
          const placed = new Map(previous.map((node) => [node.id, node.position]));
          return flow.nodes.map((node) => ({ ...node, position: placed.get(node.id) ?? node.position }));
        });
        setEdges(flow.edges);
        setStats({
          turns: turns.turns,
          total: turns.total,
          running: thread.running,
          updated: new Date().toLocaleTimeString(),
        });
        setError(null);
      } catch (err) {
        setError(String(err));
      }
    },
    [setNodes, setEdges],
  );

  useEffect(() => {
    setNodes([]);
    setEdges([]);
    setStats(null);
    void load(threadId);
    if (!threadId) return;
    const timer = window.setInterval(() => void load(threadId), POLL_MS);
    return () => window.clearInterval(timer);
  }, [threadId, load, setNodes, setEdges]);

  useEffect(() => {
    if (!inspect || !threadId) return;
    let live = true;
    setInspected(null);
    setInspectError(null);
    fetchStateValue(threadId, inspect.key, inspect.turn)
      .then((value) => live && setInspected(value))
      .catch((err: unknown) => live && setInspectError(String(err)));
    return () => {
      live = false;
    };
  }, [inspect, threadId]);

  const actions: FlowActions = useMemo(
    () => ({
      showValue: (key, turn) => setInspect({ key, turn }),
      showView: (tool, v) => setView({ tool, view: v }),
    }),
    [],
  );

  const closeView = useCallback(() => setView(null), []);

  function openThread(event: React.FormEvent) {
    event.preventDefault();
    const id = draft.trim();
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("thread", id);
    else url.searchParams.delete("thread");
    window.history.replaceState(null, "", url);
    setThreadId(id);
  }

  const empty = threadId && stats && stats.turns === 0 && !error;

  return (
    <div className="app">
      <header className="bar">
        <h1>session flow</h1>
        <form onSubmit={openThread}>
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="thread id (from the chat page's ?thread=)"
            aria-label="thread id"
          />
          <button type="submit">open</button>
        </form>
        {stats ? (
          <span className="dim status">
            {stats.turns} of {stats.total} turns · {nodes.length} nodes · {stats.running ? "running" : "idle"} · updated {stats.updated}
          </span>
        ) : null}
      </header>

      <div className="canvas">
        <FlowActionsContext.Provider value={actions}>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onInit={setCanvas}
            onlyRenderVisibleElements
            minZoom={0.1}
            proOptions={{ hideAttribution: true }}
          >
            <Background />
            <Controls />
            <MiniMap pannable zoomable />
            <Panel position="top-left" className="legend">
              <span className="swatch value" /> state value
              <span className="swatch call" /> tool call
              <span className="swatch model" /> model input
            </Panel>
            {empty ? (
              <Panel position="top-center" className="notice">
                No turns yet. Ask a question in the chat page, then open its thread here.
              </Panel>
            ) : null}
            {!threadId ? (
              <Panel position="top-center" className="notice">
                Paste a thread id above, or open this page with ?thread=&lt;id&gt;.
              </Panel>
            ) : null}
            {error ? (
              <Panel position="top-center" className="notice error">
                {error}
              </Panel>
            ) : null}
          </ReactFlow>
        </FlowActionsContext.Provider>

        {inspect ? (
          <aside className="drawer">
            <header>
              <code>{inspect.key}</code>
              <button onClick={() => setInspect(null)} aria-label="close">close</button>
            </header>
            <p className="dim">value as of turn {inspect.turn}</p>
            {inspectError ? <p className="error">{inspectError}</p> : null}
            {inspected ? (
              <pre>{JSON.stringify(inspected.value, null, 2)?.slice(0, 50000) ?? "undefined"}</pre>
            ) : (
              !inspectError && <p className="dim">loading…</p>
            )}
          </aside>
        ) : null}
      </div>

      {view ? <ViewModal tool={view.tool} view={view.view} onClose={closeView} /> : null}
    </div>
  );
}
