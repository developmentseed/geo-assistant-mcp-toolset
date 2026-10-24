import { useEffect } from "react";
import type { ViewRef } from "./graph";
import { ViewFrame } from "./ViewFrame";

interface Props {
  tool: string;
  view: ViewRef;
  onClose: () => void;
}

/** A tool's view at full size, over the canvas. */
export function ViewModal({ tool, view, onClose }: Props) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" role="dialog" aria-label={`view of ${tool}`} onClick={(e) => e.stopPropagation()}>
        <header>
          <code>{tool}</code>
          <span className="dim">{view.uri}</span>
          <button onClick={onClose} aria-label="close">close</button>
        </header>
        <ViewFrame uri={view.uri} data={view.data} title={view.uri} />
      </div>
    </div>
  );
}
