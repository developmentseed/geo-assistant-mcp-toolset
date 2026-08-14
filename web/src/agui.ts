/** The routes AG-UI does not cover.
 *
 * The run itself goes through `@ag-ui/client`'s `HttpAgent` — see `chat.tsx`.
 * This is the other half: the reads that exist *because* the stream carries a
 * description of a value rather than the value.
 */

/** One session-state value, as the state route returns it. */
export type StateValue = {
  key: string;
  kind: string | null;
  tool?: string;
  seq?: number | null;
  /** The turn it was read at, or `null` for "as state stands now". */
  turn: number | null;
  value: unknown;
};

/** One session-state value in full — the payload the stream left out.
 *
 * `STATE_SNAPSHOT` carries `{kind, tool, bytes}` per key. This is the route a
 * client follows once it has decided it wants the 39 kB geometry, and it is
 * outside the AG-UI vocabulary entirely: the protocol has a state channel but
 * no notion of a value too large to put on it.
 *
 * `turn` asks for the value **as it stood at the end of that turn** rather
 * than now. State holds one value per key, so a key a later turn overwrote
 * would otherwise read back as the later value — which is the wrong answer to
 * "what did this turn run on". The checkpoints have kept every version.
 */
export async function readState(
  threadId: string,
  key: string,
  turn?: number,
): Promise<StateValue> {
  const at = turn === undefined ? "" : `?turn=${turn}`;
  const response = await fetch(`/api/threads/${threadId}/state/${key}${at}`);
  if (!response.ok) {
    // The API's own wording, which distinguishes a turn that never existed
    // (404) from one the checkpointer has pruned (410) — a difference worth
    // showing a reader rather than flattening into "failed".
    const detail = await response
      .json()
      .then((body) => body?.detail)
      .catch(() => null);
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  return (await response.json()) as StateValue;
}

/** A thread's turns, and what session state held at the end of each.
 *
 * Not used by the live client — it builds turns from the events as they
 * arrive — but this is how a client that reloaded would get them back, and it
 * is the route the panel's per-turn view is really made of.
 */
export async function readTurns(threadId: string) {
  const response = await fetch(`/api/threads/${threadId}/turns`);
  if (!response.ok) throw new Error(`${response.status}`);
  return (await response.json()) as {
    threadId: string;
    turns: number;
    total: number;
    history: {
      turn: number;
      question: string;
      checkpointId: string | null;
      state: Record<string, { kind: string | null; tool?: string; bytes?: number }>;
    }[];
  };
}
