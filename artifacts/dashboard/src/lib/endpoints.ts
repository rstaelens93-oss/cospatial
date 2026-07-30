/**
 * endpoints.ts
 * ─────────────────────────────────────────────────────────────────────────────
 * Single source of truth for API and WebSocket base URLs.
 *
 * Web build  (VITE_API_BASE_URL not set):
 *   apiBase  → ""          → all fetch calls remain relative (/api/…)
 *   wsBase   → derived from window.location at runtime
 *
 * Native / Capacitor build  (env vars injected by CI):
 *   apiBase  → e.g. "https://cospatial.replit.app"
 *   wsBase   → e.g. "wss://cospatial.replit.app"
 *
 * Usage:
 *   import { apiUrl, wsUrl } from "@/lib/endpoints";
 *   fetch(apiUrl("/api/image/generate"), { … })
 *   new WebSocket(wsUrl("/ws/chat"))
 */

/** Absolute or empty API origin injected at Vite build time. */
const API_ORIGIN: string = import.meta.env.VITE_API_BASE_URL ?? "";

/** Absolute WebSocket origin injected at Vite build time (e.g. "wss://…"). */
const WS_ORIGIN: string = import.meta.env.VITE_WS_BASE_URL ?? "";

/**
 * Resolve a path against the configured API origin.
 * - Native build: prepends the absolute origin.
 * - Web build:    returns the path unchanged (relative URL).
 */
export function apiUrl(path: string): string {
  return API_ORIGIN ? `${API_ORIGIN.replace(/\/$/, "")}${path}` : path;
}

/**
 * Resolve a WebSocket path to a full URL.
 * - Native build: prepends the absolute WS origin.
 * - Web build:    derives the scheme and host from window.location at runtime.
 */
export function wsUrl(path: string): string {
  if (WS_ORIGIN) {
    return `${WS_ORIGIN.replace(/\/$/, "")}${path}`;
  }
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}${path}`;
}
