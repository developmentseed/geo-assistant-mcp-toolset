import { createContext, useContext, useState } from "react";
import { Handle, Position, useStore, type NodeProps, type Node } from "@xyflow/react";
import type { CallData, ModelData, ValueData } from "./graph";
import { ViewFrame } from "./ViewFrame";

/** What a node can ask the page to do. Passed by context so node data stays plain. */
export interface FlowActions {
  showValue: (key: string, turn: number) => void;
  showView: (tool: string, view: CallData["views"][number]) => void;
}

export const FlowActionsContext = createContext<FlowActions | null>(null);

function useActions(): FlowActions {
  const actions = useContext(FlowActionsContext);
  if (!actions) throw new Error("flow nodes need FlowActionsContext");
  return actions;
}

/** Below this zoom a preview is too small to read, so it is not mounted at all. */
const PREVIEW_MIN_ZOOM = 0.35;
const PREVIEW_MIN_HEIGHT = 140;
const PREVIEW_MAX_HEIGHT = 220;

const formatBytes = (n: number | undefined) =>
  n === undefined ? "" : n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} kB` : `${(n / 1048576).toFixed(1)} MB`;

/** The last segment of a key is its field: `toolset/tool/field`. */
const fieldOf = (key: string) => key.slice(key.lastIndexOf("/") + 1);

export function ValueNode({ data }: NodeProps<Node<ValueData>>) {
  const actions = useActions();
  return (
    <div className="node value" title={data.key}>
      <Handle type="target" position={Position.Left} />
      <div className="node-title">
        <code>{fieldOf(data.key)}</code>
      </div>
      <div className="node-meta">
        turn {data.turn}
        {data.tool ? <> · <span className="dim">{data.tool}</span></> : null}
      </div>
      <div className="chips">
        {data.bytes !== undefined ? <span className="chip">{formatBytes(data.bytes)}</span> : null}
        {data.replacedIn !== undefined ? (
          <span className="chip warn">replaced in turn {data.replacedIn}</span>
        ) : null}
      </div>
      <button className="node-action nodrag" onClick={() => actions.showValue(data.key, data.turn)}>
        show value
      </button>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

export function CallNode({ data }: NodeProps<Node<CallData>>) {
  const actions = useActions();
  const zoom = useStore((state) => state.transform[2]);
  const [height, setHeight] = useState(PREVIEW_MAX_HEIGHT);
  const shown = data.keys.slice(0, 4).map(fieldOf);
  const preview = data.views[0];
  return (
    <div className={`node call${preview ? " has-preview" : ""}`}>
      <Handle type="target" position={Position.Left} />
      <div className="node-title">
        <code>{data.tool}</code>
      </div>
      <div className="node-meta">turn {data.turn}</div>
      {shown.length > 0 ? (
        <ul className="published">
          {shown.map((field) => (
            <li key={field}>{field}</li>
          ))}
          {data.keys.length > shown.length ? <li className="dim">+{data.keys.length - shown.length} more</li> : null}
        </ul>
      ) : null}
      {preview ? (
        // nodrag and nowheel keep pointer and wheel input inside the view, not the canvas.
        <div className="preview nodrag nowheel">
          {zoom >= PREVIEW_MIN_ZOOM ? (
            <ViewFrame
              uri={preview.uri}
              data={preview.data}
              title={preview.uri}
              height={height}
              onHeight={(h) => setHeight(Math.min(PREVIEW_MAX_HEIGHT, Math.max(PREVIEW_MIN_HEIGHT, h)))}
            />
          ) : (
            <p className="dim">zoom in to load the view</p>
          )}
        </div>
      ) : null}
      {data.views.map((view) => (
        <button
          key={view.toolCallId}
          className="node-action view nodrag"
          onClick={() => actions.showView(data.tool, view)}
        >
          open view
        </button>
      ))}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

export function ModelNode({ data }: NodeProps<Node<ModelData>>) {
  return (
    <div className="node model">
      <div className="node-title">model</div>
      <div className="node-meta">wrote inputs · turn {data.turn}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

export const nodeTypes = { value: ValueNode, call: CallNode, model: ModelNode };
