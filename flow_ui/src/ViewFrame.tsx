import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AppBridge, PostMessageTransport } from "@modelcontextprotocol/ext-apps/app-bridge";
import { fetchViewHtml } from "./api";

interface Props {
  uri: string;
  /** The tool's structured result. Sent again whenever its content changes. */
  data: unknown;
  title: string;
  /** Fixed height in px. Without it the frame fills its container. */
  height?: number;
  /** Called with the height the view asks for, as it changes. */
  onHeight?: (height: number) => void;
}

/** A view's HTML is large and does not change, so each one is fetched once. */
const htmlCache = new Map<string, Promise<string>>();

function viewHtml(uri: string): Promise<string> {
  let html = htmlCache.get(uri);
  if (!html) {
    html = fetchViewHtml(uri);
    htmlCache.set(uri, html);
    html.catch(() => htmlCache.delete(uri));
  }
  return html;
}

/**
 * A tool's ui:// view in a sandboxed frame. This is the host end of the MCP
 * Apps handshake the chat page runs. The view gets its tool input once, then
 * each new result as the data changes, so a poll that re-reads the same
 * result does not redraw the view.
 */
export function ViewFrame({ uri, data, title, height, onHeight }: Props) {
  const [html, setHtml] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const bridge = useRef<AppBridge | null>(null);
  const ready = useRef(false);
  const sentKey = useRef("");
  const latest = useRef<unknown>(data);
  latest.current = data;
  const heightRef = useRef(onHeight);
  heightRef.current = onHeight;
  const key = useMemo(() => JSON.stringify(data) ?? "", [data]);

  const sendResult = useCallback(() => {
    const host = bridge.current;
    const payload = latest.current;
    if (!host || !ready.current || payload == null) return;
    const payloadKey = JSON.stringify(payload);
    if (payloadKey === sentKey.current) return;
    sentKey.current = payloadKey;
    void host.sendToolResult({ content: [], structuredContent: payload as Record<string, unknown> });
  }, []);

  useEffect(() => {
    let live = true;
    viewHtml(uri)
      .then((text) => live && setHtml(text))
      .catch((err: unknown) => live && setError(String(err)));
    return () => {
      live = false;
    };
  }, [uri]);

  useEffect(() => {
    const iframe = frame.current;
    if (!html || !iframe?.contentWindow) return;
    const host = new AppBridge(null, { name: "geo-assistant-flow", version: "0.1.0" }, {});
    bridge.current = host;
    host.oninitialized = () => {
      ready.current = true;
      void host.sendToolInput({ arguments: {} });
      sendResult();
    };
    host.onsizechange = (params) => {
      if (params.height) heightRef.current?.(params.height);
    };
    void host.connect(new PostMessageTransport(iframe.contentWindow, iframe.contentWindow));
    return () => {
      ready.current = false;
      sentKey.current = "";
      bridge.current = null;
      void host.close();
    };
  }, [html, sendResult]);

  useEffect(() => {
    sendResult();
  }, [key, sendResult]);

  if (error) return <p className="error">{error}</p>;
  if (!html) return <p className="dim">loading view…</p>;
  return (
    <iframe
      ref={frame}
      title={title}
      srcDoc={html}
      sandbox="allow-scripts"
      style={height === undefined ? undefined : { height }}
    />
  );
}
