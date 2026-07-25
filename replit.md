# Collaborative AI Dashboard

A collaborative full-stack workspace with a dark four-panel dashboard grid — AI concept generation, Python code execution driving a 3D scene, an image prompt display, and a real-time WebSocket chat overlay.

## Run & Operate

- `pnpm --filter @workspace/dashboard run dev` — React frontend (port 23183, served at `/`)
- `pnpm --filter @workspace/api-server run dev` — Node.js Express API (port 8080, served at `/api`)
- `python3 backend/main.py` — Python FastAPI backend with WebSockets (port 8001, served at `/ws` and `/python-api`)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- **Frontend:** React + Vite + Tailwind CSS, @monaco-editor/react, three.js, yjs
- **API:** Express 5 (Node.js) + FastAPI (Python) with WebSockets
- **DB:** PostgreSQL + Drizzle ORM
- **Validation:** Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `artifacts/dashboard/src/` — React frontend
  - `src/pages/Dashboard.tsx` — main four-panel grid layout
  - `src/components/panels/ConceptPanel.tsx` — AI prompt panel (top-left)
  - `src/components/panels/EditorPanel.tsx` — Monaco Python editor (bottom-left)
  - `src/components/panels/ImagePanel.tsx` — generated image display (top-right)
  - `src/components/panels/ViewportPanel.tsx` — Three.js 3D viewport (bottom-right)
  - `src/components/ChatBox.tsx` — collapsible chat overlay (fixed bottom-left)
- `artifacts/api-server/src/routes/` — Express route handlers (ai.ts, code.ts, chat.ts)
- `backend/main.py` — Python FastAPI: WebSocket chat + code execution endpoint
- `lib/api-spec/openapi.yaml` — OpenAPI spec (source of truth)
- `lib/db/src/schema/` — Drizzle ORM schema (concepts.ts, chat.ts)

## Architecture decisions

- **Dual backend:** Node.js Express handles REST (concepts, chat messages) while Python FastAPI handles WebSockets and code execution — `/api` routes to Express, `/ws` and `/python-api` route to FastAPI
- **WebSocket chat:** Python FastAPI broadcasts code execution events alongside chat messages so all clients see run results in real time
- **Code execution:** Python code runs in a subprocess with a 10-second timeout; `emit_scene()` helper writes JSON scene data to stderr for extraction
- **Three.js viewport:** gracefully degrades to an icon fallback when WebGL is unavailable (sandboxed environments)
- **OpenAPI-first:** all API contracts defined in `lib/api-spec/openapi.yaml`, codegen produces React Query hooks and Zod validators

## Product

- **Top-Left Panel:** Type a concept prompt → click "Generate AI Concept" → description appears with past concept history
- **Bottom-Left Panel:** Monaco editor (Python) → click "Run 3D Engine" → stdout/stderr shown, scene data sent to 3D viewport
- **Top-Right Panel:** Displays the AI-generated image prompt as a styled creative brief card
- **Bottom-Right Panel:** Three.js scene (rotating icosahedron by default; custom scene from `emit_scene()` data)
- **Chat Overlay:** Fixed bottom-left collapsible chat connecting via WebSocket to Python backend

## User preferences

_Populate as you build._

## Gotchas

- After any OpenAPI spec change, re-run codegen before modifying routes: `pnpm --filter @workspace/api-spec run codegen`
- Python backend runs from workspace root; the workflow uses `sh -c 'cd /home/runner/workspace && python3 backend/main.py'`
- The `/ws` and `/python-api` paths are served by the Python FastAPI backend (port 8001), not Express
- Three.js `WebGLRenderer` logs errors to console even when caught — normal in sandboxed environments

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
