import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { onData, sendMessage } from "@developmentseed/mcp-view";

import "./styles.css";

// Mirror the tool's ToolResult (FetchNaipImageResult in tools.py). The host
// injects the whole structuredContent as this view's payload.
interface NaipImage {
  media_type: string;
  base64: string;
  width: number;
  height: number;
  item_id: string;
  item_datetime: string;
  resolution_m: number;
}

interface FetchNaipImageResult {
  message: string;
  naip_image?: NaipImage;
}

function App() {
  const [data, setData] = useState<FetchNaipImageResult | null>(null);
  useEffect(() => {
    onData<FetchNaipImageResult>(setData);
  }, []);

  if (!data) return <div className="panel">Loading…</div>;
  const image = data.naip_image;
  if (!image) return <div className="panel">{data.message}</div>;

  return (
    <div className="panel">
      <img
        src={`data:${image.media_type};base64,${image.base64}`}
        alt={`NAIP acquisition ${image.item_id}`}
        width={image.width}
        height={image.height}
      />
      <p className="caption">
        NAIP · {image.item_id} · {image.item_datetime} · {image.resolution_m} m/px
      </p>
      {/* Interactions advance the chat: this becomes a user message, which the
          model answers by calling interpret_image. */}
      <button onClick={() => sendMessage("Interpret the NAIP image")}>
        Interpret this image
      </button>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
