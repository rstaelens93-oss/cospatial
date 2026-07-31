import { useState, useEffect, useRef, useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  useListChatMessages,
  useSendChatMessage,
  getListChatMessagesQueryKey,
} from "@workspace/api-client-react";
import type { ChatMessage } from "@workspace/api-client-react";
import { MessageCircle, ChevronDown, Send, Wifi, WifiOff, Terminal } from "lucide-react";
import { wsUrl as buildWsUrl } from "@/lib/endpoints";
import type { SceneDataPayload } from "@/hooks/useWebSockets";

function formatRelTime(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  return `${Math.floor(mins / 60)}h`;
}

const AUTHOR_KEY = "dashboard_author";

const AUTHOR_COLORS: Record<string, string> = {};
const PALETTE = [
  "#00d4ff",
  "#34d399",
  "#a78bfa",
  "#f59e0b",
  "#f87171",
  "#60a5fa",
  "#fb7185",
];
let colorIdx = 0;
function getAuthorColor(author: string): string {
  if (!AUTHOR_COLORS[author]) {
    AUTHOR_COLORS[author] = PALETTE[colorIdx % PALETTE.length];
    colorIdx++;
  }
  return AUTHOR_COLORS[author];
}

interface SystemEvent {
  id: string | number;
  kind: "system";
  content: string;
  createdAt: string;
}

type MessageItem = ChatMessage | SystemEvent;

interface ChatBoxProps {
  /** Called whenever the backend broadcasts an image_scene frame with a
   *  parsed SceneDataPayload so the dashboard can forward it to the viewport. */
  onSceneData?: (data: SceneDataPayload) => void;
  /** Called when the backend broadcasts an update_editor_text frame so the
   *  dashboard can pre-fill the Python Engine editor. */
  onEditorText?: (code: string) => void;
}

// Height of the expanded chat body in px — must match the CSS value below.
const BODY_HEIGHT = 380;
// Height of the always-visible handle bar in px — exported so Dashboard can
// push the grid up by exactly this amount.
export const CHAT_BAR_HEIGHT = 38;

export default function ChatBox({ onSceneData, onEditorText }: ChatBoxProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const [unread, setUnread] = useState(0);
  const [input, setInput] = useState("");
  const [author, setAuthor] = useState<string>(() => {
    return localStorage.getItem(AUTHOR_KEY) ?? "User";
  });
  const [wsStatus, setWsStatus] = useState<"connected" | "disconnected" | "connecting">("connecting");
  const [wsMessages, setWsMessages] = useState<MessageItem[]>([]);
  const wsRef = useRef<WebSocket | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(true);
  const isExpandedRef = useRef(isExpanded);
  const onSceneDataRef = useRef(onSceneData);
  useEffect(() => { onSceneDataRef.current = onSceneData; }, [onSceneData]);
  const onEditorTextRef = useRef(onEditorText);
  useEffect(() => { onEditorTextRef.current = onEditorText; }, [onEditorText]);
  const queryClient = useQueryClient();

  useEffect(() => {
    isExpandedRef.current = isExpanded;
  }, [isExpanded]);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // REST fallback
  const { data: restMessages } = useListChatMessages({
    query: {
      queryKey: getListChatMessagesQueryKey(),
      refetchInterval: 15000,
    },
  });

  const sendMessage = useSendChatMessage({
    mutation: {
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: getListChatMessagesQueryKey() });
      },
    },
  });

  // Merge REST and WS messages
  const allChatMessages: MessageItem[] = (() => {
    const seen = new Set<number>();
    const merged: MessageItem[] = [];

    if (restMessages) {
      restMessages.forEach((m) => {
        seen.add(m.id);
        merged.push(m);
      });
    }

    wsMessages.forEach((m) => {
      if ("kind" in m && (m as SystemEvent).kind === "system") {
        merged.push(m);
      } else if (!seen.has((m as ChatMessage).id)) {
        merged.push(m);
      }
    });

    return merged.sort((a, b) => {
      const getTime = (m: MessageItem) => {
        if ("kind" in m && (m as SystemEvent).kind === "system") {
          return new Date((m as SystemEvent).createdAt).getTime();
        }
        return new Date((m as ChatMessage).createdAt).getTime();
      };
      return getTime(a) - getTime(b);
    });
  })();

  // WebSocket setup — mounts once only ([]).
  useEffect(() => {
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      if (!mountedRef.current) return;
      if (mountedRef.current) setWsStatus("connecting");

      const wsUrl = buildWsUrl("/ws/chat");

      try {
        ws = new WebSocket(wsUrl);
        wsRef.current = ws;

        ws.onopen = () => {
          if (!mountedRef.current) return;
          setWsStatus("connected");
        };

        ws.onmessage = (event) => {
          if (!mountedRef.current) return;
          try {
            const msg = JSON.parse(event.data as string);

            if (msg.type === "history" && Array.isArray(msg.messages)) {
              setWsMessages((prev) => {
                const ids = new Set(msg.messages.map((m: ChatMessage) => m.id));
                const nonHist = prev.filter((m) =>
                  "kind" in m && (m as SystemEvent).kind === "system"
                    ? true
                    : !ids.has((m as ChatMessage).id)
                );
                return [...msg.messages, ...nonHist];
              });
            } else if (msg.type === "message" && msg.message) {
              setWsMessages((prev) => [...prev, msg.message]);
              if (!isExpandedRef.current) {
                setUnread((u) => u + 1);
              }
            } else if (msg.type === "code_executing") {
              const sysMsg: SystemEvent = {
                id: `sys-${Date.now()}`,
                kind: "system",
                content: "Code execution started...",
                createdAt: new Date().toISOString(),
              };
              setWsMessages((prev) => [...prev, sysMsg]);
            } else if (msg.type === "code_result" && msg.result) {
              const r = msg.result as { success: boolean; executionTime: number };
              const sysMsg: SystemEvent = {
                id: `sys-${Date.now()}`,
                kind: "system",
                content: `Code executed — ${r.success ? "success" : "error"} (${r.executionTime}ms)`,
                createdAt: new Date().toISOString(),
              };
              setWsMessages((prev) => [...prev, sysMsg]);
            } else if (msg.type === "image_scene" && typeof msg.sceneData === "string") {
              try {
                const parsed = JSON.parse(msg.sceneData) as SceneDataPayload;
                onSceneDataRef.current?.(parsed);
                const sysMsg: SystemEvent = {
                  id: `sys-${Date.now()}`,
                  kind: "system",
                  content: `Image → 3D: ${(msg.pointCount as number) ?? "?"} points materialised in viewport (${msg.executionTime}ms)`,
                  createdAt: new Date().toISOString(),
                };
                setWsMessages((prev) => [...prev, sysMsg]);
              } catch {
                // malformed payload — discard silently
              }
            } else if (msg.type === "update_editor_text" && typeof msg.code === "string" && msg.code) {
              onEditorTextRef.current?.(msg.code as string);
              if (msg.partial === false || msg.partial === undefined) {
                const sysMsg: SystemEvent = {
                  id: `sys-${Date.now()}`,
                  kind: "system",
                  content: "Python Engine pre-filled — inspect and run when ready.",
                  createdAt: new Date().toISOString(),
                };
                setWsMessages((prev) => [...prev, sysMsg]);
              }
            }
          } catch {
            // ignore JSON parse errors
          }
        };

        ws.onclose = () => {
          if (!mountedRef.current) return;
          setWsStatus("disconnected");
          reconnectTimer = setTimeout(() => {
            if (mountedRef.current) connect();
          }, 4000);
        };

        ws.onerror = () => {
          ws?.close();
        };
      } catch {
        if (!mountedRef.current) return;
        setWsStatus("disconnected");
        reconnectTimer = setTimeout(() => {
          if (mountedRef.current) connect();
        }, 4000);
      }
    };

    connect();

    return () => {
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      const current = wsRef.current;
      if (current) {
        current.onclose = null;
        current.onerror = null;
        current.onmessage = null;
        current.onopen = null;
        current.close();
        wsRef.current = null;
      }
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-scroll when messages change or panel opens
  useEffect(() => {
    if (isExpanded) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [allChatMessages, isExpanded]);

  // Clear unread badge when user opens the panel
  useEffect(() => {
    if (isExpanded) setUnread(0);
  }, [isExpanded]);

  const handleSend = useCallback(() => {
    if (!input.trim()) return;
    const content = input.trim();
    setInput("");

    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(
        JSON.stringify({ type: "message", content, author })
      );
    } else {
      sendMessage.mutate({ data: { content, author } });
    }
  }, [input, author, sendMessage]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  // ─── Single render — no early return ────────────────────────────────────────
  // The component is always in the DOM. The chat body slides up/down via a
  // maxHeight transition; only the handle bar is visible when collapsed.
  return (
    <div
      style={{
        position: "fixed",
        bottom: 0,
        left: 0,
        width: "100%",
        zIndex: 1000,
        display: "flex",
        flexDirection: "column",
      }}
    >
      {/* ── Chat body — maxHeight drives the slide animation ── */}
      <div
        style={{
          maxHeight: isExpanded ? `${BODY_HEIGHT}px` : "0px",
          transition: "max-height 0.35s cubic-bezier(0.4, 0, 0.2, 1)",
          overflow: "hidden",
        }}
      >
        {/* Fixed-height inner so flex children compute correctly */}
        <div
          data-testid="chat-panel"
          style={{
            height: `${BODY_HEIGHT}px`,
            display: "flex",
            flexDirection: "column",
            background: "hsl(215 24% 9%)",
            borderTop: "1px solid rgba(0, 212, 255, 0.25)",
            boxShadow: "0 -4px 32px rgba(0, 0, 0, 0.6), 0 -1px 0 rgba(0, 212, 255, 0.08)",
          }}
        >
          {/* Author config row */}
          <div
            style={{
              flexShrink: 0,
              display: "flex",
              alignItems: "center",
              gap: "8px",
              padding: "0 12px",
              height: "34px",
              borderBottom: "1px solid hsl(213 18% 14%)",
              background: "hsl(215 24% 10%)",
            }}
          >
            <span style={{ color: "hsl(200 15% 40%)", fontSize: "11px", fontFamily: "var(--app-font-mono)" }}>
              You:
            </span>
            <input
              data-testid="input-chat-author"
              type="text"
              value={author}
              onChange={(e) => {
                setAuthor(e.target.value);
                localStorage.setItem(AUTHOR_KEY, e.target.value);
              }}
              style={{
                flex: 1,
                background: "transparent",
                border: "none",
                outline: "none",
                color: "#00d4ff",
                fontSize: "11px",
                fontFamily: "var(--app-font-mono)",
              }}
            />
            {/* WS status */}
            <div style={{ display: "flex", alignItems: "center", gap: "5px" }}>
              {wsStatus === "connected" ? (
                <Wifi size={11} style={{ color: "#34d399" }} />
              ) : wsStatus === "connecting" ? (
                <Wifi size={11} style={{ color: "#f59e0b" }} className="animate-pulse" />
              ) : (
                <WifiOff size={11} style={{ color: "#ef4444" }} />
              )}
              <span style={{
                fontSize: "10px",
                fontFamily: "var(--app-font-mono)",
                color: wsStatus === "connected" ? "#34d399" : wsStatus === "connecting" ? "#f59e0b" : "#ef4444",
              }}>
                {wsStatus}
              </span>
            </div>
          </div>

          {/* Messages */}
          <div
            style={{
              flex: 1,
              overflowY: "auto",
              padding: "8px 12px",
              display: "flex",
              flexDirection: "column",
              gap: "6px",
              minHeight: 0,
            }}
          >
            {allChatMessages.length === 0 ? (
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: "8px" }}>
                <MessageCircle size={20} style={{ color: "hsl(200 15% 28%)" }} />
                <p style={{ color: "hsl(200 15% 38%)", fontSize: "12px", textAlign: "center", margin: 0 }}>
                  No messages yet.<br />Say something to start.
                </p>
              </div>
            ) : (
              allChatMessages.map((msg, idx) => {
                if ("kind" in msg && (msg as SystemEvent).kind === "system") {
                  const sysMsg = msg as SystemEvent;
                  return (
                    <div key={sysMsg.id} style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                      <Terminal size={10} style={{ color: "hsl(200 15% 35%)", flexShrink: 0 }} />
                      <span style={{
                        fontSize: "10px",
                        fontFamily: "var(--app-font-mono)",
                        color: "hsl(200 15% 38%)",
                      }}>
                        {sysMsg.content}
                      </span>
                    </div>
                  );
                }
                const chatMsg = msg as ChatMessage;
                return (
                  <div key={`chat-${chatMsg.id}-${idx}`} data-testid={`message-${chatMsg.id}`}>
                    <div style={{ display: "flex", alignItems: "baseline", gap: "6px", marginBottom: "2px" }}>
                      <span style={{
                        fontSize: "11px",
                        fontWeight: 600,
                        color: getAuthorColor(chatMsg.author),
                        fontFamily: "var(--app-font-mono)",
                      }}>
                        {chatMsg.author}
                      </span>
                      <span style={{ fontSize: "10px", color: "hsl(200 15% 30%)" }}>
                        {formatRelTime(chatMsg.createdAt)}
                      </span>
                    </div>
                    <p style={{ margin: 0, fontSize: "12px", lineHeight: "1.5", color: "hsl(200 15% 70%)" }}>
                      {chatMsg.content}
                    </p>
                  </div>
                );
              })
            )}
            <div ref={messagesEndRef} />
          </div>

          {/* Input bar */}
          <div
            style={{
              flexShrink: 0,
              display: "flex",
              alignItems: "center",
              gap: "8px",
              padding: "8px 12px",
              borderTop: "1px solid hsl(213 18% 16%)",
            }}
          >
            <input
              data-testid="input-chat-message"
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Type a message..."
              style={{
                flex: 1,
                background: "transparent",
                border: "none",
                outline: "none",
                fontSize: "12px",
                color: "hsl(200 20% 82%)",
              }}
            />
            <button
              data-testid="button-chat-send"
              onClick={handleSend}
              disabled={!input.trim()}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                width: "28px",
                height: "28px",
                borderRadius: "4px",
                border: "1px solid rgba(0, 212, 255, 0.25)",
                background: "rgba(0, 212, 255, 0.1)",
                color: "#00d4ff",
                cursor: input.trim() ? "pointer" : "default",
                opacity: input.trim() ? 1 : 0.3,
                flexShrink: 0,
              }}
            >
              <Send size={12} />
            </button>
          </div>
        </div>
      </div>

      {/* ── Handle bar — always visible, drives open/close ── */}
      <button
        data-testid="button-chat-toggle"
        onClick={() => setIsExpanded((e) => !e)}
        style={{
          width: "100%",
          height: `${CHAT_BAR_HEIGHT}px`,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: "8px",
          cursor: "pointer",
          background: "hsl(215 24% 11%)",
          borderTop: "1px solid rgba(0, 212, 255, 0.2)",
          borderLeft: "none",
          borderRight: "none",
          borderBottom: "none",
          borderRadius: 0,
          flexShrink: 0,
          transition: "background 0.15s",
          boxShadow: "0 -1px 0 rgba(0, 212, 255, 0.08)",
        }}
        onMouseEnter={(e) => {
          (e.currentTarget as HTMLButtonElement).style.background = "hsl(215 24% 14%)";
        }}
        onMouseLeave={(e) => {
          (e.currentTarget as HTMLButtonElement).style.background = "hsl(215 24% 11%)";
        }}
      >
        {isExpanded ? (
          <ChevronDown size={13} style={{ color: "#00d4ff" }} />
        ) : (
          <MessageCircle size={13} style={{ color: "#00d4ff" }} />
        )}

        <span
          style={{
            fontSize: "11px",
            fontFamily: "var(--app-font-mono)",
            letterSpacing: "0.08em",
            color: "hsl(200 20% 65%)",
            userSelect: "none",
          }}
        >
          {isExpanded ? "CLOSE CHAT" : "💬  Click to Open Chat"}
        </span>

        {/* Unread badge */}
        {!isExpanded && unread > 0 && (
          <span
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              minWidth: "18px",
              height: "18px",
              padding: "0 4px",
              borderRadius: "9px",
              background: "#00d4ff",
              color: "hsl(215 28% 7%)",
              fontSize: "10px",
              fontWeight: 700,
              fontFamily: "var(--app-font-mono)",
            }}
          >
            {unread > 9 ? "9+" : unread}
          </span>
        )}

        {/* WS status dot */}
        <span
          style={{
            width: "6px",
            height: "6px",
            borderRadius: "50%",
            flexShrink: 0,
            background:
              wsStatus === "connected"
                ? "#34d399"
                : wsStatus === "connecting"
                ? "#f59e0b"
                : "#ef4444",
          }}
        />
      </button>
    </div>
  );
}
