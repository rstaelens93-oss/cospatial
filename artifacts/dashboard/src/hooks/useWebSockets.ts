/**
 * useWebSocket
 * ============
 * Room-aware WebSocket hook for the Collaborative AI Dashboard.
 *
 * Features:
 *  • SEAT_COLORS – six hex values, one per team seat, used to tint every
 *    incoming chat message by the author's dynamically assigned seat index.
 *  • Scene-data bridge – parses code_result.sceneData JSON and forwards
 *    the structured payload to the viewport via onSceneData().
 *  • Mobile-safe ref state – the WebSocket object and the reconnect timer
 *    are held in useRef, never in useState, so GC can reclaim them
 *    immediately after the component unmounts without waiting for a render.
 *  • onSystemError – called on over-capacity (WS close 4003) or any
 *    server-side trial-boundary rejection before the socket is closed.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatMessage } from "@workspace/api-client-react";

// ─────────────────────────────────────────────────────────────────────────────
// Seat colors
// ─────────────────────────────────────────────────────────────────────────────

/**
 * One color per seat slot in a 6-person team room.
 * Authors are assigned an index in arrival order; solo_trial users always
 * receive index 0 (the only seat they are permitted to occupy).
 */
export const SEAT_COLORS: readonly string[] = [
  "#00d4ff", // seat 0 – cyan     (default for solo_trial)
  "#34d399", // seat 1 – emerald
  "#a78bfa", // seat 2 – violet
  "#f59e0b", // seat 3 – amber
  "#f87171", // seat 4 – rose
  "#60a5fa", // seat 5 – sky
] as const;

// ─────────────────────────────────────────────────────────────────────────────
// Public types
// ─────────────────────────────────────────────────────────────────────────────

export type WsStatus = "connecting" | "connected" | "disconnected";

/** A chat message enriched with a resolved seat color for rendering. */
export interface ColoredMessage extends ChatMessage {
  seatColor: string;
}

/** 3D scene payload forwarded to the viewport from code_result.sceneData. */
export interface SceneDataPayload {
  type: "points" | "mesh";
  points?: { x: number; y: number; z: number }[];
  color?: string;
  vertices?: number[];
  faces?: number[];
}

export interface UseWebSocketOptions {
  /**
   * Room identifier passed to the Python backend.
   * Defaults to "global".
   */
  roomId?: string;
  /**
   * Subscription tier forwarded as a query param so the server enforces
   * the correct per-room capacity:
   *   solo_trial  → 1 seat
   *   *_paid      → 6 seats
   * Defaults to "solo_trial".
   */
  tier?: string;
  /**
   * Invoked when the server sends a system_error frame or closes the socket
   * with code 4003 (room full / trial boundary exceeded).
   * The hook does NOT attempt to reconnect after a 4003 close.
   */
  onSystemError?: (message: string) => void;
  /**
   * Invoked whenever a code_result frame contains valid sceneData JSON.
   * Pass the returned payload directly to ViewportPanel as the sceneData prop.
   */
  onSceneData?: (data: SceneDataPayload) => void;
}

export interface UseWebSocketReturn {
  /** Current connection state. Updates trigger a re-render. */
  status: WsStatus;
  /** All received chat messages enriched with seat colors, in arrival order. */
  messages: ColoredMessage[];
  /**
   * Send a chat message. Silently no-ops if the socket is not OPEN so callers
   * never need to guard the call themselves.
   */
  send: (content: string, author: string) => void;
  /**
   * Return the hex seat color assigned to a given author string.
   * Safe to call for any author at any time, including before they have
   * sent a message (they will be assigned the next available slot).
   */
  getSeatColor: (author: string) => string;
}

// ─────────────────────────────────────────────────────────────────────────────
// Hook
// ─────────────────────────────────────────────────────────────────────────────

export function useWebSocket({
  roomId = "global",
  tier = "solo_trial",
  onSystemError,
  onSceneData,
}: UseWebSocketOptions = {}): UseWebSocketReturn {
  // ── Render-triggering state ───────────────────────────────────────────────
  const [status, setStatus] = useState<WsStatus>("connecting");
  const [messages, setMessages] = useState<ColoredMessage[]>([]);

  // ── Refs – never cause re-renders, safe to read/write at any time ─────────

  /** The live WebSocket instance. */
  const wsRef = useRef<WebSocket | null>(null);

  /**
   * Tracks whether the consuming component is still mounted.
   * Checked before every state update and reconnect timer so that:
   *  (a) setState calls on unmounted components are silently skipped, and
   *  (b) the reconnect timer cannot fire after the component is gone —
   *      preventing memory leaks on low-memory mobile browsers that keep
   *      detached component trees alive longer than desktop browsers do.
   */
  const mountedRef = useRef(true);

  /** Active reconnect timer handle — held in a ref so cleanup is always exact. */
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /**
   * Author → seat-index map.
   * Assigned in first-seen order; wraps mod SEAT_COLORS.length so it stays
   * valid even if more authors join than there are defined colors.
   */
  const seatMapRef = useRef<Map<string, number>>(new Map());

  /**
   * Callback refs — store the latest prop values without listing the callbacks
   * as WebSocket effect dependencies.  This prevents the socket from being
   * torn down and rebuilt every time the parent component re-renders with a
   * new inline function reference.
   */
  const onSystemErrorRef = useRef(onSystemError);
  const onSceneDataRef = useRef(onSceneData);

  useEffect(() => { onSystemErrorRef.current = onSystemError; }, [onSystemError]);
  useEffect(() => { onSceneDataRef.current = onSceneData; }, [onSceneData]);

  // ── Mounted guard lifecycle ───────────────────────────────────────────────
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // ── Seat color helpers ────────────────────────────────────────────────────

  /** Assign (or recall) a seat index for an author and return their hex color. */
  const resolveSeatColor = useCallback((author: string): string => {
    const map = seatMapRef.current;
    if (!map.has(author)) {
      map.set(author, map.size % SEAT_COLORS.length);
    }
    return SEAT_COLORS[map.get(author)!];
  }, []);

  const colorMessage = useCallback(
    (msg: ChatMessage): ColoredMessage => ({
      ...msg,
      seatColor: resolveSeatColor(msg.author),
    }),
    [resolveSeatColor],
  );

  // ── WebSocket lifecycle ───────────────────────────────────────────────────
  useEffect(() => {
    const clearReconnect = () => {
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };

    const connect = () => {
      if (!mountedRef.current) return;
      clearReconnect();

      if (mountedRef.current) setStatus("connecting");

      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const url =
        `${protocol}//${window.location.host}/ws/chat` +
        `?room_id=${encodeURIComponent(roomId)}` +
        `&tier=${encodeURIComponent(tier)}`;

      let ws: WebSocket;
      try {
        ws = new WebSocket(url);
        wsRef.current = ws;
      } catch {
        if (!mountedRef.current) return;
        setStatus("disconnected");
        reconnectTimerRef.current = setTimeout(() => {
          if (mountedRef.current) connect();
        }, 4000);
        return;
      }

      ws.onopen = () => {
        if (!mountedRef.current) return;
        setStatus("connected");
      };

      ws.onmessage = (event: MessageEvent) => {
        if (!mountedRef.current) return;

        let payload: Record<string, unknown>;
        try {
          payload = JSON.parse(event.data as string) as Record<string, unknown>;
        } catch {
          return; // ignore non-JSON frames
        }

        // ── Over-capacity / trial rejection (sent before close) ───────────
        if (payload.event_type === "system_error") {
          onSystemErrorRef.current?.(
            typeof payload.message === "string"
              ? payload.message
              : "Connection rejected by server.",
          );
          return;
        }

        // ── Full chat history sent on connect ─────────────────────────────
        if (payload.type === "history" && Array.isArray(payload.messages)) {
          setMessages(
            (payload.messages as ChatMessage[]).map(colorMessage),
          );
          return;
        }

        // ── Single incoming chat message ──────────────────────────────────
        if (payload.type === "message" && payload.message) {
          setMessages((prev) => [
            ...prev,
            colorMessage(payload.message as ChatMessage),
          ]);
          return;
        }

        // ── 3D mesh / point-cloud scene data ──────────────────────────────
        // Emitted by the Python backend when user code calls emit_scene().
        // The sceneData field is a JSON-encoded SceneDataPayload string.
        if (payload.type === "code_result" && payload.result) {
          const result = payload.result as Record<string, unknown>;
          if (typeof result.sceneData === "string" && result.sceneData) {
            try {
              const parsed = JSON.parse(result.sceneData) as SceneDataPayload;
              onSceneDataRef.current?.(parsed);
            } catch {
              // malformed scene data — discard silently
            }
          }
          return;
        }
      };

      ws.onclose = (event: CloseEvent) => {
        if (!mountedRef.current) return;
        setStatus("disconnected");

        // 4003 – room full or tier capacity exceeded.
        // Do NOT reconnect: the server will reject us again immediately.
        if (event.code === 4003) {
          onSystemErrorRef.current?.(
            event.reason ||
              "Connection rejected: room is at capacity or trial limit reached.",
          );
          return;
        }

        // All other close reasons: schedule a reconnect.
        reconnectTimerRef.current = setTimeout(() => {
          if (mountedRef.current) connect();
        }, 4000);
      };

      ws.onerror = () => {
        // Don't re-throw — a throw inside an event handler escapes to
        // window.onerror and triggers the Vite error overlay.
        // Close cleanly; onclose will schedule the reconnect.
        ws.close();
      };
    };

    connect();

    // ── Cleanup: null out all handlers before closing ─────────────────────
    // Nulling handlers ensures that onclose cannot queue a new reconnect
    // timer after we've already cleared the existing one — the root cause of
    // the stale-timer memory leak we fixed in ChatBox.tsx.
    return () => {
      clearReconnect();
      const current = wsRef.current;
      if (current) {
        current.onopen = null;
        current.onmessage = null;
        current.onclose = null;
        current.onerror = null;
        current.close();
        wsRef.current = null;
      }
    };
  }, [roomId, tier, colorMessage]);

  // ── Public API ────────────────────────────────────────────────────────────

  const send = useCallback((content: string, author: string): void => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "message", content, author }));
  }, []);

  const getSeatColor = useCallback(
    (author: string): string => resolveSeatColor(author),
    [resolveSeatColor],
  );

  return { status, messages, send, getSeatColor };
}
