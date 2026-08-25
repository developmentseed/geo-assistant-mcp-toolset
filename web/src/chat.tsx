import { HttpAgent, type Message } from "@ag-ui/client";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { readState, readThread, readTurns } from "./agui";

/** Session state as the stream describes it: no payloads, one line per key. */
type StateEntry = {
  tool?: string;
  bytes?: number;
  seq?: number;
  /** Parameter -> the state key it came from, or "model". Absent when the
   * producing call took no arguments. */
  inputs?: Record<string, string>;
};

/** Every argument of the call that produced an entry, in a stable order.
 *
 * Both halves, not just the model's. The listing a *refusal* shows the model
 * names only what the model wrote, because every line there costs context and
 * "this one came from state" is the unremarkable case. A panel has neither
 * constraint and a reader has no memory of the call, which by now has scrolled
 * away — dropping half the record here would leave the chain unreadable from
 * the one surface built to show it.
 *
 * One level, deliberately: this reads the call that produced the entry and
 * follows nothing further. The reader follows it by clicking, since every
 * state-sourced input names a key that is itself a row in this panel. */
function producedBy(entry: StateEntry): [string, string][] {
  // Model-authored first: it is the caveat, and a reader scanning a column of
  // these is looking for it rather than for the unremarkable half.
  return Object.entries(entry.inputs ?? {}).sort(
    ([a, from], [b, other]) =>
      Number(other === "model") - Number(from === "model") ||
      a.localeCompare(b),
  );
}

/** `state key -> the arguments of the call that produced it`.
 *
 * The value a model wrote is deliberately *not* on the wire: `inputs` carries
 * parameter names and state keys and nothing else, because an argument can be
 * arbitrarily large and the state channel is re-sent every turn. A client does
 * not need it to be — it already holds the call. `state.published` names the
 * `toolCallId`, the transcript holds that call, and this is the join.
 *
 * Read across every message rather than one turn's, so a key published three
 * turns ago still resolves.
 */
function producedArguments(
  all: readonly Message[],
): Record<string, Record<string, unknown>> {
  const calls: Record<string, Record<string, unknown>> = {};
  for (const message of all) {
    for (const call of (message as any).toolCalls ?? []) {
      try {
        calls[call.id] = JSON.parse(call.function.arguments || "{}");
      } catch {
        calls[call.id] = {};
      }
    }
  }
  const found: Record<string, Record<string, unknown>> = {};
  for (const message of all) {
    if ((message as any).activityType !== "state.published") continue;
    const content = (message as any).content;
    const args = calls[content?.toolCallId];
    if (!args) continue;
    for (const key of Object.values<string>(content?.published ?? {})) {
      found[key] = args;
    }
  }
  return found;
}

/** How much of a model-authored value fits on a line before it is folded. */
const INLINE = 56;

/** Whether a value is a string holding JSON — an object or an array.
 *
 * Providers differ on whether a structured argument arrives as an object or
 * as the text of one, and the server coerces either. Rendering the text form
 * with `JSON.stringify` escapes it a second time, which helps nobody. Only
 * objects and arrays qualify: a model that wrote the string "4" wrote a
 * string, and quoting it is the honest rendering. */
function isJsonText(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    const parsed: unknown = JSON.parse(value);
    return typeof parsed === "object" && parsed !== null;
  } catch {
    return false;
  }
}

/** A value as the tool received it, indented. */
function pretty(value: unknown): string {
  return isJsonText(value)
    ? JSON.stringify(JSON.parse(value), null, 2)
    : JSON.stringify(value, null, 2);
}

/** The value the model wrote, shown whole or folded.
 *
 * The case worth seeing is the expensive one — a model inlining a large
 * literal into an untagged parameter — and that is exactly the case that
 * would fill the panel. `<details>` because collapsing is what the element is
 * for, and the keyboard and screen-reader behaviour comes with it.
 */
function Wrote({ value }: { value: unknown }) {
  if (value === undefined) {
    return <span className="authored">written by the model</span>;
  }
  // Quoted, so a string reads as a value rather than as a second identifier
  // beside the parameter — except where it is the text of an object, which
  // `JSON.stringify` would escape twice.
  const whole = isJsonText(value) ? value : JSON.stringify(value);
  if (whole.length <= INLINE) {
    return (
      <>
        <code className="wrote">{whole}</code>
        <span className="authored"> · written by the model</span>
      </>
    );
  }
  return (
    <details className="folded">
      <summary>
        <code className="wrote">{whole.slice(0, INLINE)}…</code>
        <span className="authored"> · written by the model</span>
        <span className="dim"> · {whole.length} chars</span>
      </summary>
      <pre>{pretty(value)}</pre>
    </details>
  );
}

/** A state key that may wrap, preferring its own separators.
 *
 * `<toolset>/<tool>/<field>` has no spaces, so a narrow column breaks it
 * mid-word — `datas / ets` — unless it is told where the seams are. `<wbr>`
 * marks them; `overflow-wrap: break-word` remains the fallback for a segment
 * too long to fit on its own. */
function Key({ value }: { value: string }) {
  const parts = value.split("/");
  return (
    <>
      {parts.map((part, index) => (
        <Fragment key={index}>
          {index > 0 ? (
            <>
              /<wbr />
            </>
          ) : null}
          {part}
        </Fragment>
      ))}
    </>
  );
}

/** Every key the thread holds, which is what the state channel describes. */
type Snapshot = Record<string, StateEntry>;

/**
 * Key inside AG-UI's `state` object holding session-state metadata. The rest
 * of that object belongs to the client, and every operation the server sends
 * names a path inside this one key — so whatever a client keeps beside it
 * survives a run untouched.
 */
const TOOL_STATE = "toolState";

/** One JSON Patch operation, as `STATE_DELTA` carries them. */
type Operation = { op: string; path: string; value?: StateEntry };

/**
 * Our `toolState`, moved on by one delta.
 *
 * Short because the server only ever sends two shapes: `add` of the whole
 * namespace, which opens every run and is the resynchronisation point, and
 * `add`/`remove` of one key under it. RFC 6901 escaping has to be undone —
 * state keys are `toolset/name`, and `/` is the pointer's own separator.
 */
function applyDelta(state: Snapshot, delta: Operation[]): Snapshot {
  let next = state;
  for (const { op, path, value } of delta) {
    if (path === `/${TOOL_STATE}`) {
      next = { ...((value ?? {}) as unknown as Snapshot) };
      continue;
    }
    const key = path
      .slice(`/${TOOL_STATE}/`.length)
      .replace(/~1/g, "/")
      .replace(/~0/g, "~");
    next = { ...next };
    if (op === "remove") delete next[key];
    else if (value) next[key] = value;
  }
  return next;
}

/** The activity messages naming one tool call — `mcp.view` and both state
 * halves alike.
 *
 * A turn's `published` origins reach only `state.published`, because that is
 * the one activity keyed by what it wrote. A view is published under no key
 * and a `state.consumed` writes none, so neither is in that map. Every
 * activity carries the `toolCallId` it belongs to, and an activity is a
 * message, so reading the messages is what covers all three.
 */
function activitiesOf(all: readonly Message[], toolCallId: string): string[] {
  return all
    .filter(
      (message) =>
        (message as any).role === "activity" &&
        (message as any).content?.toolCallId === toolCallId,
    )
    .map((message) => String(message.id));
}

/** Where a key came from, read off the `state.published` that announced it.
 *
 * The announcing activity is not held here: both hover paths resolve
 * activities through `activitiesOf`, which finds all three rather than only
 * the one that named a key. */
type Origin = { toolCallId: string; tool: string };

/** One question and what it did.
 *
 * The stream has no turn boundary in it — `RUN_STARTED` and `RUN_FINISHED`
 * bracket a run, but a client that reloads mid-thread never saw them. So a
 * turn is marked here, at the point the question is asked, and `from` is where
 * its messages begin.
 */
type Turn = {
  n: number;
  question: string;
  /** The question's own message id, so scrolling to a turn needs no index. */
  questionId: string;
  from: number;
  /** Cumulative, not a delta: everything in state as of this turn. */
  state: Snapshot;
  /** Only the keys *this* turn wrote, and which call wrote each. */
  published: Record<string, Origin>;
};

/** What a hover has lit up, on all three sides at once. */
type Linked = { keys: string[]; calls: string[]; activities: string[] };

const NOTHING: Linked = { keys: [], calls: [], activities: [] };

/** The line a minimal client prints. Every activity carries one, generated by
 * the same code the bundled Chainlit host renders, so the two cannot drift. A
 * client with opinions reads the fields beside it instead. */
function shown(content: any): string {
  if (typeof content?.display === "string") return content.display;
  // state.consumed carries one receipt per parameter, each with its own line.
  if (content?.received) {
    return Object.entries<any>(content.received)
      .map(([parameter, receipt]) => `${parameter} ${receipt.display}`)
      .join("   ·   ");
  }
  return JSON.stringify(content);
}

/** A tool result on one line. The whole thing is in the thread route if a
 * client wants it; this is the glance. */
function summarise(result: string): string {
  const line = result.replace(/\s+/g, " ").trim();
  return line.length > 140 ? `${line.slice(0, 140)}…` : line;
}

function bytes(size?: number): string {
  if (size === undefined) return "";
  return size < 1024 ? `${size} B` : `${(size / 1024).toFixed(1)} kB`;
}

/** ext-apps `LATEST_PROTOCOL_VERSION`, which the view's SDK checks. */
const UI_PROTOCOL_VERSION = "2026-01-26";

/** A tool's `ui://` bundle, mounted and driven over MCP Apps `ui/*`.
 *
 * This is the host end of the same JSON-RPC-over-postMessage protocol Claude,
 * ChatGPT, Goose and VS Code implement — the view's end is the standard
 * `@modelcontextprotocol/ext-apps` SDK, wrapped by `@developmentseed/mcp-view`.
 * The exchange is four messages:
 *
 *     view "ui/initialize"                 -> host info and capabilities
 *     view "ui/notifications/initialized"  -> host pushes tool-input, then
 *                                             tool-result (that order is
 *                                             required, and the view only
 *                                             needs the second)
 *     view "ui/message"                    -> a turn back into the chat
 *
 * `src` is the view route rather than `srcdoc`, so the bundle really is fetched
 * over HTTP — that route exists because a bundle is hundreds of kilobytes and
 * does not change between turns. `allow-scripts` without `allow-same-origin`
 * gives the frame an opaque origin: it can run, and it can reach nothing.
 */
function View({
  uri,
  data,
  onMessage,
}: {
  uri: string;
  data: unknown;
  onMessage: (text: string) => void;
}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const [toolset, view] = uri.replace("ui://", "").split("/");
  // The turn's data never changes once rendered, but the listener is mounted
  // once — so it reads through a ref rather than closing over the first value.
  const latest = useRef(data);
  latest.current = data;
  const reply = useRef(onMessage);
  reply.current = onMessage;

  useEffect(() => {
    const iframe = frame.current;
    if (!iframe) return;
    const post = (message: unknown) =>
      iframe.contentWindow?.postMessage(message, "*");

    function handle(event: MessageEvent) {
      if (event.source !== iframe?.contentWindow) return;
      const message = event.data;
      if (!message || message.jsonrpc !== "2.0") return;

      if (message.method === "ui/initialize") {
        post({
          jsonrpc: "2.0",
          id: message.id,
          result: {
            protocolVersion: UI_PROTOCOL_VERSION,
            hostInfo: { name: "geo-assistant-chat", version: "1.0.0" },
            hostCapabilities: { message: { text: {} } },
            hostContext: {},
          },
        });
      } else if (message.method === "ui/notifications/initialized") {
        post({
          jsonrpc: "2.0",
          method: "ui/notifications/tool-input",
          params: { arguments: {} },
        });
        post({
          jsonrpc: "2.0",
          method: "ui/notifications/tool-result",
          params: { content: [], structuredContent: latest.current },
        });
      } else if (message.method === "ui/message" && message.id !== undefined) {
        const text = (message.params?.content ?? [])
          .filter((block: any) => block?.type === "text")
          .map((block: any) => block.text)
          .join("");
        if (text) reply.current(text);
        post({ jsonrpc: "2.0", id: message.id, result: {} });
      }
    }

    window.addEventListener("message", handle);
    return () => window.removeEventListener("message", handle);
  }, []);

  return (
    <iframe
      ref={frame}
      className="view"
      title={uri}
      src={`/api/views/${toolset}/${view}`}
      sandbox="allow-scripts"
    />
  );
}

export function Chat() {
  // The reference client, not a parser of our own: `HttpAgent` POSTs a
  // `RunAgentInput`, runs the SSE through `verifyEvents`, and applies each
  // event to `messages` and `state`. If this server emitted anything the
  // protocol disallows, the run would fail here rather than render wrongly.
  // `?thread=` if the URL names one, so a reload comes back to the same
  // conversation rather than a fresh one — the thread lives in the
  // checkpointer, and the id is the only thing a client needs to keep.
  const [threadId] = useState(
    () =>
      new URLSearchParams(location.search).get("thread") || crypto.randomUUID(),
  );
  const agent = useMemo(
    () => new HttpAgent({ url: "/api/runs", threadId }),
    [threadId],
  );
  const log = useRef<HTMLDivElement>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [showing, setShowing] = useState(0);
  // Whether the turn selector is pinned to a past turn. A new turn steals focus
  // only while it is not — otherwise reading turn 1 is interrupted by turn 3.
  const pinned = useRef(false);
  const [opened, setOpened] = useState<{
    key: string;
    turn: number | null;
    value?: unknown;
    error?: string;
  } | null>(null);
  const [folded, setFolded] = useState(false);
  const [linked, setLinked] = useState<Linked>(NOTHING);
  const [running, setRunning] = useState(false);
  // The message currently receiving tokens, or null. Bracketed by the stream's
  // own TEXT_MESSAGE_START/END rather than inferred from the transcript: "the
  // newest assistant message" is a different claim, and it is wrong twice —
  // before this turn has written anything it names the last turn's answer, and
  // a tool call with no preamble is an assistant message with no text.
  const [writing, setWriting] = useState<string | null>(null);
  const busy = useRef(false);
  const [question, setQuestion] = useState(
    "find cafes within 500 m of the Space Needle",
  );

  // Rendered message elements, so a turn can be scrolled to by the question
  // that started it. Keyed by message id rather than index: ids are stable and
  // indices shift as a turn fills in beneath them.
  const nodes = useRef(new Map<string, HTMLElement>());

  useEffect(() => {
    // Not while a past turn is pinned: following the newest message would drag
    // the reader off the turn they went back to look at.
    if (pinned.current) return;
    log.current?.scrollTo({ top: log.current.scrollHeight });
  }, [messages]);

  // Put the thread in the URL, so reloading the page restores it. Replace
  // rather than push: this is not a navigation, and a back button that stepped
  // through thread ids would be nonsense.
  useEffect(() => {
    const url = new URL(location.href);
    if (url.searchParams.get("thread") === threadId) return;
    url.searchParams.set("thread", threadId);
    history.replaceState(null, "", url);
  }, [threadId]);

  /** Rebuild the conversation from the thread id alone.
   *
   * Two routes, because the stream has no turn boundary a reloaded client
   * could have seen: `/threads/{id}` is the transcript **and its activities**,
   * `/threads/{id}/turns` is what state held at the end of each turn. They are
   * joined on the question — turn *n* starts at the *n*th user message.
   *
   * The activities come back as messages, which is what an activity is in
   * AG-UI, so `origins` folds them into the same `key -> origin` map the live
   * client builds and the cross-highlighting works with no special case. Each
   * turn is bounded by the next one's start: unbounded, turn 1 would claim
   * every later turn's publications too.
   */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const thread = await readThread(threadId).catch(() => null);
      if (cancelled || !thread || thread.messages.length === 0) return;
      const past = await readTurns(threadId).catch(() => null);
      if (cancelled) return;

      const all = thread.messages as unknown as Message[];
      const starts = thread.messages
        .map((message, index) => ({ message, index }))
        .filter(({ message }) => message.role === "user");
      const restored: Turn[] = starts.map(({ message, index }, n) => ({
        n: n + 1,
        question: message.content || "",
        questionId: message.id,
        from: index,
        state: (past?.history[n]?.state ?? {}) as Snapshot,
        published: origins(
          all.slice(0, starts[n + 1]?.index ?? all.length),
          index,
        ),
      }));

      agent.setMessages(all);
      setMessages([...agent.messages]);
      setTurns(restored);
      setShowing(Math.max(restored.length - 1, 0));
    })();
    return () => {
      cancelled = true;
    };
  }, [agent, threadId]);

  /** Put the question that started a turn at the top of the log.
   *
   * `scrollTo` on the log rather than `scrollIntoView` on the message, which
   * scrolls every scrollable ancestor as well — including, at narrow widths,
   * the page itself.
   */
  function scrollToTurn(turn: Turn) {
    const node = nodes.current.get(turn.questionId);
    if (!node || !log.current) return;
    // `offsetTop` counts from inside the log's padding, so a bare scroll puts
    // the question flush against the edge. The browser clamps at 0.
    log.current.scrollTo({ top: node.offsetTop - 12, behavior: "smooth" });
  }

  /** Fold this turn's `state.published` activities into `key -> origin`. */
  function origins(
    all: readonly Message[],
    from: number,
  ): Record<string, Origin> {
    const found: Record<string, Origin> = {};
    for (const message of all.slice(from)) {
      const content = (message as any).content;
      if (
        (message as any).role !== "activity" ||
        (message as any).activityType !== "state.published"
      ) {
        continue;
      }
      for (const key of Object.values<string>(content?.published ?? {})) {
        found[key] = { toolCallId: content.toolCallId, tool: content.tool };
      }
    }
    return found;
  }

  /** Run one turn. Called by the form, and by a view over `ui/message`.
   *
   * A ref rather than the `running` state for the guard: a view's button is
   * driven from a listener mounted once, so it closes over the first render's
   * value of anything held in state and would happily start a second turn on
   * top of the first.
   */
  async function run(text: string) {
    if (!text || busy.current) return;
    busy.current = true;
    setRunning(true);

    const from = agent.messages.length;
    const questionId = crypto.randomUUID();
    agent.addMessage({ id: questionId, role: "user", content: text });
    setMessages([...agent.messages]);
    setTurns((held) => {
      const started: Turn = {
        n: held.length + 1,
        question: text,
        questionId,
        from,
        // Carried forward: state is cumulative, so a turn starts holding
        // everything the last one ended with.
        state: held[held.length - 1]?.state ?? {},
        published: {},
      };
      if (!pinned.current) setShowing(held.length);
      return [...held, started];
    });

    const patch = (change: (turn: Turn) => Turn) =>
      setTurns((held) =>
        held.map((turn, index) =>
          index === held.length - 1 ? change(turn) : turn,
        ),
      );

    try {
      // One subscriber, called after each event is applied. Rendering from
      // `messages` rather than from the events is the point of the library:
      // an activity *is* a message, so it already sits where it belongs.
      await agent.runAgent(undefined, {
        onEvent: ({ messages }) => {
          setMessages([...messages]);
          patch((turn) => ({
            ...turn,
            published: origins(messages, turn.from),
          }));
        },
        // Where the caret goes. The protocol brackets one assistant message's
        // text with these two, which is exactly what the caret claims.
        onTextMessageStartEvent: ({ event }) => setWriting(event.messageId),
        onTextMessageEndEvent: () => setWriting(null),
        // Session state arrives on AG-UI's standard `state` channel as
        // patches, every one of them under `toolState`. Each entry carries
        // `{tool, bytes, seq, inputs}`; see the README.
        //
        // Applied rather than merged: the operations say what changed,
        // including a key leaving, which a merge could not express. The one
        // that opens a run replaces the namespace whole — that is the
        // resynchronisation point, and it carries the thread's state, not just
        // this turn's writes.
        onStateDeltaEvent: ({ event }) => {
          patch((turn) => ({
            ...turn,
            state: applyDelta(turn.state, event.delta as Operation[]),
          }));
        },
      });
    } catch (error) {
      agent.addMessage({
        id: crypto.randomUUID(),
        role: "assistant",
        content: `client error: ${String(error)}`,
      });
      setMessages([...agent.messages]);
    } finally {
      busy.current = false;
      setRunning(false);
      // A run that fails between START and END never sends the END, which
      // would otherwise leave the caret blinking on a message nothing is
      // writing to.
      setWriting(null);
    }
  }

  function ask(submitted: React.FormEvent) {
    submitted.preventDefault();
    const text = question.trim();
    setQuestion("");
    void run(text);
  }

  const turn: Turn | undefined = turns[showing];
  const latest = showing === turns.length - 1;

  /** Fetch a key's value *as of the turn being shown*, not as of now.
   *
   * Passing the turn is the whole difference: a key a later turn overwrote
   * reads back as the later value without it, which is the wrong answer to
   * "what did this turn run on".
   */
  async function open(key: string) {
    const at = turn?.n;
    setFolded(false);
    try {
      const got = await readState(agent.threadId, key, at);
      setOpened({ key, turn: got.turn, value: got.value });
    } catch (error) {
      // A turn the checkpointer has pruned answers 410 with a sentence saying
      // so. Showing it beats a blank panel: "gone" and "never existed" are
      // different facts and the API has already told them apart.
      setOpened({
        key,
        turn: at ?? null,
        error: (error as Error).message,
      });
    }
  }

  /** Light a key, and with it the call and every activity about that call.
   *
   * The same set `litByCall` lights, deliberately: one relationship should
   * light identically whichever end of it is hovered, or the pair reads as two
   * coincidences rather than one link.
   */
  function litByKey(key: string) {
    const origin = turn?.published[key];
    setLinked(
      origin
        ? {
            keys: [key],
            calls: [origin.toolCallId],
            activities: activitiesOf(messages, origin.toolCallId),
          }
        : { ...NOTHING, keys: [key] },
    );
  }

  /** Light a call, and with it every key it wrote and every activity about it.
   *
   * Every activity, not only the ones announcing a key: a call's `mcp.view`
   * is the row hardest to attribute by eye, since several tools in a turn
   * each produce one and the rows are identical but for the URI.
   */
  function litByCall(toolCallId: string) {
    const wrote = Object.entries(turn?.published ?? {}).filter(
      ([, origin]) => origin.toolCallId === toolCallId,
    );
    setLinked({
      keys: wrote.map(([key]) => key),
      calls: [toolCallId],
      activities: activitiesOf(messages, toolCallId),
    });
  }

  const entries = Object.entries(turn?.state ?? {}).sort(
    ([leftKey, left], [rightKey, right]) =>
      (left.seq ?? 0) - (right.seq ?? 0) || leftKey.localeCompare(rightKey),
  );
  // What the model actually wrote, recovered from the calls the transcript
  // holds. Nothing on the wire carries it; see `producedArguments`.
  const wroteFor = useMemo(() => producedArguments(messages), [messages]);

  return (
    <main className={opened ? (folded ? "folded" : "opened") : undefined}>
      <div className="chat">
        <header>
          <b>Geo Assistant</b>
          {/* The two colours are the whole point of the wire: blue is what
              AG-UI gives any client, amber is what this runtime adds on top
              of it. Naming them beats leaving a reader to infer it. */}
          <span className="legend">
            <i className="swatch tool" /> AG-UI
            <i className="swatch activity" /> receipts and views
            <span className="dim">· thread {agent.threadId.slice(0, 8)}</span>
          </span>
        </header>

        <div className="log" ref={log}>
          {messages.map((message) =>
            message.role === "user" || message.role === "assistant" ? (
              <div
                key={message.id}
                className={`said ${message.role}`}
                ref={(node) => {
                  const id = String(message.id);
                  if (node) nodes.current.set(id, node);
                  else nodes.current.delete(id);
                }}
              >
                {/* Models answer in markdown whether or not you asked, and a
                    half-written stream is half-written markdown — an unclosed
                    ** or a table with one row. react-markdown re-parses each
                    delta, so it degrades to plain text rather than showing
                    syntax, and renders no raw HTML, which matters when the
                    text came from a model.

                    GFM because a model asked to compare things answers with a
                    table, and tables are not CommonMark — without this the
                    pipes are the output. Strikethrough and bare URLs come with
                    it. */}
                <Markdown remarkPlugins={[remarkGfm]}>
                  {String(message.content ?? "")}
                </Markdown>
                {(message as any).toolCalls?.map((call: any) => (
                  // <details> rather than state: collapsing is what the
                  // element is for, and the keyboard and screen-reader
                  // behaviour comes with it.
                  <details
                    key={call.id}
                    className={`tool ${linked.calls.includes(call.id) ? "lit" : ""}`}
                    onMouseEnter={() => litByCall(call.id)}
                    onMouseLeave={() => setLinked(NOTHING)}
                  >
                    <summary>
                      <code>{call.function.name}</code>
                    </summary>
                    <pre>{call.function.arguments || "{}"}</pre>
                  </details>
                ))}
                {message.id === writing ? <i className="caret" /> : null}
              </div>
            ) : message.role === "tool" ? (
              <details key={message.id} className="tool">
                <summary>
                  <span className="dim">result</span>{" "}
                  {summarise(String(message.content ?? ""))}
                </summary>
                <pre>{String(message.content ?? "")}</pre>
              </details>
            ) : message.role === "activity" ? (
              <details
                key={message.id}
                className={`activity ${
                  linked.activities.includes(String(message.id)) ? "lit" : ""
                }`}
                onMouseEnter={() => {
                  // Whatever the activity is, not only `state.published`:
                  // the question a reader has in front of a view or a
                  // consumed receipt is which call it belongs to, and the
                  // `toolCallId` answering it is on all three. `keys` stays
                  // empty for the two that publish nothing.
                  const content = (message as any).content;
                  if (!content?.toolCallId) return;
                  setLinked({
                    keys: Object.values<string>(content.published ?? {}),
                    calls: [content.toolCallId],
                    activities: activitiesOf(messages, content.toolCallId),
                  });
                }}
                onMouseLeave={() => setLinked(NOTHING)}
              >
                <summary>
                  <em>{(message as any).activityType}</em>
                  {(message as any).content?.tool ? (
                    <>
                      {" "}
                      <code>{(message as any).content.tool}</code>
                    </>
                  ) : null}
                </summary>
                <span>{shown((message as any).content)}</span>
                {(message as any).content?.uri ? (
                  <View
                    uri={(message as any).content.uri}
                    data={(message as any).content.data}
                    // `ui/message` starts the turn rather than filling the
                    // box: the bundled Chainlit host sends it, and a button
                    // that only types for you is a view that cannot act.
                    onMessage={run}
                  />
                ) : null}
                <pre className="dim">
                  {JSON.stringify((message as any).content, null, 2)}
                </pre>
              </details>
            ) : null,
          )}
          {messages.length === 0 ? (
            <p className="dim">
              Two MCP servers are connected: <b>duckdb-analyst</b> (Overture
              place lookup, geodesic buffers and spatial SQL, with table, chart
              and map <code>ui://</code> views) and <b>naip-imagery</b> (NAIP
              aerial imagery with an image view). Try{" "}
              <b>find cafes within 500 m of the Space Needle</b>, then{" "}
              <b>fetch a NAIP image of that area</b>.
            </p>
          ) : null}
        </div>

        <form onSubmit={ask}>
          <input
            value={question}
            onChange={(changed) => setQuestion(changed.target.value)}
            placeholder="ask something"
            autoFocus
          />
          <button disabled={running || !question.trim()}>
            {running ? "…" : "send"}
          </button>
        </form>
      </div>

      <aside>
        <h2>session state</h2>

        {turns.length > 0 ? (
          <>
            {/* State is cumulative, so a turn is a position in it rather than
                a slice of it. Switching turns rewinds the panel to what the
                thread held then. */}
            <div className="turns">
              {turns.map((each, index) => (
                <button
                  key={each.n}
                  className={index === showing ? "turn on" : "turn"}
                  title={each.question}
                  onClick={() => {
                    setShowing(index);
                    pinned.current = index !== turns.length - 1;
                    setLinked(NOTHING);
                    scrollToTurn(each);
                  }}
                >
                  {each.n}
                </button>
              ))}
            </div>
            <p className="asked dim">
              {turn?.question}
              {latest ? null : " · past turn"}
            </p>
          </>
        ) : (
          <p className="dim">
            What the tools exchanged without the model reading it. The stream
            carries this much per key and no payload; the value is a fetch away.
          </p>
        )}

        {entries.map(([key, entry]) => {
          const origin = turn?.published[key];
          return (
            <div
              key={key}
              className={`slot ${linked.keys.includes(key) ? "lit" : ""}`}
              onMouseEnter={() => litByKey(key)}
              onMouseLeave={() => setLinked(NOTHING)}
            >
              <div className="card">
                <button
                  className="key"
                  title={`GET /threads/…/state/${key}?turn=${turn?.n}`}
                  onClick={() => void open(key)}
                >
                  <code>
                    {origin ? <b className="new">new</b> : null}{" "}
                    <Key value={key} />
                  </code>
                  <span className="dim">
                    {bytes(entry.bytes)} · from {entry.tool}
                  </span>
                </button>
                {producedBy(entry).length > 0 ? (
                  <>
                    <p className="inputs-label">
                      inputs to <code>{entry.tool}</code>
                    </p>
                    <ul className="inputs">
                      {producedBy(entry).map(([parameter, from]) => (
                        <li key={parameter}>
                          <code className="param">{parameter}</code>
                          <span className="rel">
                            {from === "model" ? " = " : " ← "}
                          </span>
                          {from === "model" ? (
                            <Wrote value={wroteFor[key]?.[parameter]} />
                          ) : (
                            <button
                              className="from"
                              title={`from ${from} — click to open it`}
                              onMouseEnter={() => litByKey(from)}
                              onClick={() => void open(from)}
                            >
                              <Key value={from} />
                            </button>
                          )}
                        </li>
                      ))}
                    </ul>
                  </>
                ) : null}
              </div>
            </div>
          );
        })}

        {turns.length > 0 && entries.length === 0 ? (
          <p className="dim">nothing published yet</p>
        ) : null}
      </aside>

      {opened ? (
        <section className="value">
          <header>
            <button
              className="fold"
              onClick={() => setFolded(!folded)}
              title={folded ? "expand" : "collapse"}
            >
              {folded ? "›" : "‹"}
            </button>
            {folded ? null : (
              <>
                <code>{opened.key}</code>
                <button className="fold" onClick={() => setOpened(null)}>
                  ✕
                </button>
              </>
            )}
          </header>
          {folded ? null : (
            <>
              <p className="dim">
                <b>
                  GET /threads/…/state/{opened.key}
                  {opened.turn === null ? "" : `?turn=${opened.turn}`}
                </b>
                <br />
                {/* Which turn this is, said plainly: several of these panels
                    over a conversation are otherwise indistinguishable, and
                    the value genuinely differs between turns. */}
                {opened.turn === null
                  ? "as state stands now"
                  : `as it stood at the end of turn ${opened.turn}`}
              </p>
              {opened.error ? (
                <p className="error">{opened.error}</p>
              ) : (
                <pre>{JSON.stringify(opened.value, null, 2)}</pre>
              )}
            </>
          )}
        </section>
      ) : null}
    </main>
  );
}
