import { useState, useEffect, useRef, useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  useListChatMessages,
  useSendChatMessage,
  getListChatMessagesQueryKey,
} from "@workspace/api-client-react";
import type { ChatMessage } from "@workspace/api-client-react";
import { MessageCircle, Minus, Send, Wifi, WifiOff, Terminal } from "lucide-react";

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

export default function ChatBox() {
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
  const queryClient = useQueryClient();

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

    // Start with REST messages
    if (restMessages) {
      restMessages.forEach((m) => {
        seen.add(m.id);
        merged.push(m);
      });
    }

    // Add WS messages not already in REST
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

  // WebSocket setup
  useEffect(() => {
    let ws: WebSocket;
    let reconnectTimer: ReturnType<typeof setTimeout>;

    const connect = () => {
      setWsStatus("connecting");
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const wsUrl = protocol + "//" + window.location.host + "/ws/chat";

      try {
        ws = new WebSocket(wsUrl);
        wsRef.current = ws;

        ws.onopen = () => {
          setWsStatus("connected");
        };

        ws.onmessage = (event) => {
          try {
            const msg = JSON.parse(event.data);

            if (msg.type === "history" && Array.isArray(msg.messages)) {
              setWsMessages((prev) => {
                const ids = new Set(msg.messages.map((m: ChatMessage) => m.id));
                const nonHist = prev.filter(
                  (m) => "kind" in m && (m as SystemEvent).kind === "system" ? true : !ids.has((m as ChatMessage).id)
                );
                return [...msg.messages, ...nonHist];
              });
            } else if (msg.type === "message" && msg.message) {
              setWsMessages((prev) => [...prev, msg.message]);
              if (!isExpanded) {
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
              const r = msg.result;
              const sysMsg: SystemEvent = {
                id: `sys-${Date.now()}`,
                kind: "system",
                content: `Code executed — ${r.success ? "success" : "error"} (${r.executionTime}ms)`,
                createdAt: new Date().toISOString(),
              };
              setWsMessages((prev) => [...prev, sysMsg]);
            }
          } catch {
            // ignore parse errors
          }
        };

        ws.onclose = () => {
          setWsStatus("disconnected");
          reconnectTimer = setTimeout(connect, 4000);
        };

        ws.onerror = () => {
          setWsStatus("disconnected");
          ws.close();
        };
      } catch {
        setWsStatus("disconnected");
        reconnectTimer = setTimeout(connect, 4000);
      }
    };

    connect();

    return () => {
      clearTimeout(reconnectTimer);
      wsRef.current?.close();
    };
  }, [isExpanded]);

  // Auto-scroll
  useEffect(() => {
    if (isExpanded) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [allChatMessages, isExpanded]);

  // Clear unread when expanded
  useEffect(() => {
    if (isExpanded) setUnread(0);
  }, [isExpanded]);

  const handleSend = useCallback(() => {
    if (!input.trim()) return;
    const content = input.trim();
    setInput("");

    // Try WebSocket first
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(
        JSON.stringify({ type: "message", content, author })
      );
    } else {
      // REST fallback
      sendMessage.mutate({ data: { content, author } });
    }
  }, [input, author, sendMessage]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  if (!isExpanded) {
    return (
      <button
        data-testid="button-chat-toggle"
        onClick={() => setIsExpanded(true)}
        className="fixed bottom-4 left-4 z-50 flex items-center gap-2 px-3 py-2 rounded-full text-xs font-medium transition-all duration-200"
        style={{
          background: "hsl(215 24% 11%)",
          border: "1px solid rgba(0, 212, 255, 0.25)",
          color: "hsl(200 20% 80%)",
          boxShadow: "0 4px 16px rgba(0,0,0,0.6)",
        }}
        onMouseEnter={(e) => {
          (e.currentTarget as HTMLButtonElement).style.borderColor = "rgba(0, 212, 255, 0.5)";
          (e.currentTarget as HTMLButtonElement).style.boxShadow = "0 0 16px rgba(0, 212, 255, 0.15), 0 4px 16px rgba(0,0,0,0.6)";
        }}
        onMouseLeave={(e) => {
          (e.currentTarget as HTMLButtonElement).style.borderColor = "rgba(0, 212, 255, 0.25)";
          (e.currentTarget as HTMLButtonElement).style.boxShadow = "0 4px 16px rgba(0,0,0,0.6)";
        }}
      >
        <MessageCircle size={14} style={{ color: "#00d4ff" }} />
        <span>Chat</span>
        {unread > 0 && (
          <span
            className="flex items-center justify-center rounded-full text-xs font-bold min-w-[18px] h-[18px] px-1"
            style={{
              background: "#00d4ff",
              color: "hsl(215 28% 7%)",
              fontSize: "10px",
            }}
          >
            {unread > 9 ? "9+" : unread}
          </span>
        )}
        <span
          className="w-1.5 h-1.5 rounded-full"
          style={{
            background:
              wsStatus === "connected"
                ? "#34d399"
                : wsStatus === "connecting"
                ? "#f59e0b"
                : "#ef4444",
          }}
        />
      </button>
    );
  }

  return (
    <div
      data-testid="chat-panel"
      className="fixed bottom-4 left-4 z-50 flex flex-col rounded-lg overflow-hidden"
      style={{
        width: "320px",
        height: "420px",
        background: "hsl(215 24% 9%)",
        border: "1px solid rgba(0, 212, 255, 0.2)",
        boxShadow: "0 8px 40px rgba(0,0,0,0.8), 0 0 24px rgba(0, 212, 255, 0.06)",
      }}
    >
      {/* Header */}
      <div
        className="flex items-center gap-2 px-3 py-2.5 flex-shrink-0 relative panel-header-accent"
        style={{
          background: "hsl(215 24% 11%)",
          borderBottom: "1px solid hsl(213 18% 16%)",
        }}
      >
        <div
          className="w-5 h-5 rounded flex items-center justify-center"
          style={{
            background: "rgba(0, 212, 255, 0.1)",
            border: "1px solid rgba(0, 212, 255, 0.2)",
          }}
        >
          <MessageCircle size={11} style={{ color: "#00d4ff" }} />
        </div>
        <span
          className="text-xs font-semibold tracking-wider uppercase flex-1"
          style={{ color: "hsl(200 20% 82%)" }}
        >
          Live Chat
        </span>

        {/* WS status indicator */}
        <div className="flex items-center gap-1.5 mr-1">
          {wsStatus === "connected" ? (
            <Wifi size={11} style={{ color: "#34d399" }} />
          ) : wsStatus === "connecting" ? (
            <Wifi size={11} style={{ color: "#f59e0b" }} className="animate-pulse" />
          ) : (
            <WifiOff size={11} style={{ color: "#ef4444" }} />
          )}
          <span className="text-xs" style={{
            color: wsStatus === "connected" ? "#34d399" : wsStatus === "connecting" ? "#f59e0b" : "#ef4444",
            fontSize: "10px",
          }}>
            {wsStatus}
          </span>
        </div>

        <button
          data-testid="button-chat-minimize"
          onClick={() => setIsExpanded(false)}
          className="p-1 rounded transition-colors"
          style={{ color: "hsl(200 15% 45%)" }}
          onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.color = "#00d4ff"; }}
          onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.color = "hsl(200 15% 45%)"; }}
        >
          <Minus size={12} />
        </button>
      </div>

      {/* Author config row */}
      <div
        className="flex items-center gap-2 px-3 py-1.5 flex-shrink-0"
        style={{ borderBottom: "1px solid hsl(213 18% 14%)" }}
      >
        <span className="text-xs" style={{ color: "hsl(200 15% 40%)" }}>You:</span>
        <input
          data-testid="input-chat-author"
          type="text"
          value={author}
          onChange={(e) => {
            setAuthor(e.target.value);
            localStorage.setItem(AUTHOR_KEY, e.target.value);
          }}
          className="flex-1 bg-transparent text-xs outline-none"
          style={{
            color: "#00d4ff",
            fontFamily: "var(--app-font-mono)",
          }}
        />
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-2 min-h-0">
        {allChatMessages.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full gap-2">
            <MessageCircle size={20} style={{ color: "hsl(200 15% 28%)" }} />
            <p className="text-xs text-center" style={{ color: "hsl(200 15% 38%)" }}>
              No messages yet.
              <br />Say something to start.
            </p>
          </div>
        ) : (
          allChatMessages.map((msg, idx) => {
            if ("kind" in msg && (msg as SystemEvent).kind === "system") {
              const sysMsg = msg as SystemEvent;
              return (
                <div
                  key={sysMsg.id}
                  className="flex items-center gap-2"
                >
                  <Terminal size={10} style={{ color: "hsl(200 15% 35%)" }} className="flex-shrink-0" />
                  <span className="text-xs" style={{ color: "hsl(200 15% 38%)", fontFamily: "var(--app-font-mono)", fontSize: "10px" }}>
                    {sysMsg.content}
                  </span>
                </div>
              );
            }
            const chatMsg = msg as ChatMessage;
            return (
              <div key={`chat-${chatMsg.id}-${idx}`} className="group" data-testid={`message-${chatMsg.id}`}>
                <div className="flex items-baseline gap-1.5 mb-0.5">
                  <span
                    className="text-xs font-semibold"
                    style={{ color: getAuthorColor(chatMsg.author), fontFamily: "var(--app-font-mono)", fontSize: "11px" }}
                  >
                    {chatMsg.author}
                  </span>
                  <span className="text-xs" style={{ color: "hsl(200 15% 30%)", fontSize: "10px" }}>
                    {formatRelTime(chatMsg.createdAt)}
                  </span>
                </div>
                <p
                  className="text-xs leading-relaxed pl-0"
                  style={{ color: "hsl(200 15% 70%)" }}
                >
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
        className="flex-shrink-0 flex items-center gap-2 px-3 py-2"
        style={{ borderTop: "1px solid hsl(213 18% 16%)" }}
      >
        <input
          data-testid="input-chat-message"
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Type a message..."
          className="flex-1 bg-transparent text-xs outline-none placeholder:text-muted-foreground"
          style={{ color: "hsl(200 20% 82%)" }}
        />
        <button
          data-testid="button-chat-send"
          onClick={handleSend}
          disabled={!input.trim()}
          className="flex items-center justify-center w-7 h-7 rounded transition-all duration-150 disabled:opacity-30"
          style={{
            background: "rgba(0, 212, 255, 0.1)",
            border: "1px solid rgba(0, 212, 255, 0.25)",
            color: "#00d4ff",
          }}
          onMouseEnter={(e) => {
            if (input.trim()) {
              (e.currentTarget as HTMLButtonElement).style.background = "rgba(0, 212, 255, 0.2)";
            }
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLButtonElement).style.background = "rgba(0, 212, 255, 0.1)";
          }}
        >
          <Send size={12} />
        </button>
      </div>
    </div>
  );
}
