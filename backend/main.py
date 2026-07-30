"""
Collaborative AI Dashboard – Python FastAPI Backend  v2.0
==========================================================
Integrates:
  • connection_manager.py – room-aware WebSocket manager with per-tier caps
  • sandbox.py            – multiprocessing sandboxed code execution
  • models.py             – SQLAlchemy ORM (UserRegistry, SubscriptionState, …)

Rate limits enforced here:
  • 3 renders / 60 s  per IP  (in-memory sliding window)
  • 300 renders / month per user  (DB-backed via SubscriptionState)

WebSocket capacity by tier:
  • solo_trial  → 1 connection per room
  • *_paid      → 6 connections per room
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
from collections import defaultdict, deque
from typing import Any, Generator, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from connection_manager import ConnectionManager
from models import Base, SubscriptionState
from sandbox import execute_user_spatial_math


# ─────────────────────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────────────────────

_DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

if _DATABASE_URL:
    _engine = create_engine(_DATABASE_URL, pool_pre_ping=True)
    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    # Create tables owned by models.py if they don't exist yet.
    Base.metadata.create_all(bind=_engine)
else:
    _engine = None
    _SessionLocal = None


def get_db() -> Generator[Optional[Session], None, None]:
    """FastAPI dependency – yields a DB session or None when no DB is configured."""
    if _SessionLocal is None:
        yield None
        return
    db: Session = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Rate-limiting constants & state
# ─────────────────────────────────────────────────────────────────────────────

RENDER_MAX_PER_MINUTE: int = 3
RENDER_WINDOW_SECONDS: int = 60
MONTHLY_TOKEN_CAP: int = 300

# Sliding window per IP: maps ip → deque of monotonic timestamps.
_render_windows: dict[str, deque] = defaultdict(deque)


def _check_render_rate_limit(client_ip: str) -> None:
    """
    Enforce 3-renders-per-60-second sliding window per IP.
    Raises HTTP 429 with a Retry-After header if the limit is exceeded.
    """
    now = time.monotonic()
    window = _render_windows[client_ip]

    # Drop timestamps older than the window.
    while window and now - window[0] > RENDER_WINDOW_SECONDS:
        window.popleft()

    if len(window) >= RENDER_MAX_PER_MINUTE:
        retry_after = int(RENDER_WINDOW_SECONDS - (now - window[0])) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit: max {RENDER_MAX_PER_MINUTE} renders per minute.",
            headers={"Retry-After": str(retry_after)},
        )

    window.append(now)


def _check_monthly_cap(user_id: str, db: Optional[Session]) -> None:
    """Raise HTTP 429 if the user has exhausted their monthly render tokens."""
    if not db or not user_id:
        return
    sub = db.query(SubscriptionState).filter(
        SubscriptionState.user_id == user_id
    ).first()
    if sub and sub.total_generated_this_month >= MONTHLY_TOKEN_CAP:
        raise HTTPException(
            status_code=429,
            detail=f"Monthly token cap of {MONTHLY_TOKEN_CAP} renders reached.",
        )


def _increment_monthly_counter(user_id: str, db: Optional[Session]) -> None:
    """Increment the per-user monthly render counter on successful generation."""
    if not db or not user_id:
        return
    sub = db.query(SubscriptionState).filter(
        SubscriptionState.user_id == user_id
    ).first()
    if sub:
        sub.total_generated_this_month += 1
        db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Tier → WebSocket capacity map
# ─────────────────────────────────────────────────────────────────────────────

_TIER_CAP: dict[str, int] = {
    "solo_trial":     1,   # standalone – single connection only
    "solo_paid":      6,   # team seat cap
    "team_paid":      6,
    "enterprise_paid": 6,
}
_DEFAULT_CAP: int = 1  # fail-closed: unknown tier → trial capacity


# ─────────────────────────────────────────────────────────────────────────────
# Application
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="AI Dashboard Python Backend", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = ConnectionManager()

# In-memory chat history (capped at 100 messages).
_chat_history: list[dict[str, Any]] = [
    {
        "id": 1,
        "content": "Welcome to the Collaborative AI Dashboard! Use the prompt panel to generate concepts.",
        "author": "System",
        "createdAt": "2026-07-25T00:00:00Z",
        "type": "system",
    }
]
_next_message_id: int = 2


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket endpoint
# ─────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws/chat")
async def websocket_chat(
    websocket: WebSocket,
    room_id: str = Query(default="global"),
    tier: str = Query(default="solo_trial"),
) -> None:
    """
    Room-aware chat WebSocket.

    Query params:
      room_id – which room to join (default: "global")
      tier    – caller's subscription tier; controls the hard cap enforced by
                ConnectionManager (solo_trial → 1, paid tiers → 6).
    """
    global _next_message_id

    cap = _TIER_CAP.get(tier, _DEFAULT_CAP)
    connected = await manager.connect(room_id, websocket, max_cap=cap)
    if not connected:
        # ConnectionManager already sent the system_error frame and closed the socket.
        return

    # Send history to the newly connected client.
    await websocket.send_json({"type": "history", "messages": _chat_history})

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "message":
                message: dict[str, Any] = {
                    "id": _next_message_id,
                    "content": data.get("content", ""),
                    "author": data.get("author", "Anonymous"),
                    "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "type": "user",
                }
                _next_message_id += 1
                _chat_history.append(message)
                if len(_chat_history) > 100:
                    _chat_history.pop(0)
                await manager.broadcast_to_room(room_id, {"type": "message", "message": message})

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        await manager.disconnect(room_id, websocket)


# ─────────────────────────────────────────────────────────────────────────────
# Concept generation endpoint  (rate-limited, sandboxed)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/python-api/generate")
async def generate_concept(
    request: Request,
    db: Optional[Session] = Depends(get_db),
) -> dict:
    """
    Generate a 2D concept from a text prompt.

    Rate limits applied (both raise HTTP 429 on violation):
      • 3 requests / 60 s  per client IP
      • 300 requests / month per user_id  (requires DB)

    The generation itself runs inside the multiprocessing sandbox so it
    can never block or escape the event loop.
    """
    body: dict = await request.json()
    prompt: str = body.get("prompt", "").strip()
    user_id: str = body.get("user_id", "")

    if not prompt:
        raise HTTPException(status_code=400, detail="'prompt' is required.")

    client_ip: str = (request.client.host if request.client else "unknown")

    # ── 1. Per-IP rate limit ──────────────────────────────────────────────
    _check_render_rate_limit(client_ip)

    # ── 2. Monthly token cap ─────────────────────────────────────────────
    _check_monthly_cap(user_id, db)

    # ── 3. Run inside the multiprocessing sandbox ─────────────────────────
    #    execute_user_spatial_math is blocking (uses Process.join); run it
    #    in the default thread-pool executor so the event loop stays free.
    generation_code = (
        "import json, math\n"
        f"prompt = {json.dumps(prompt)}\n"
        "result = {\n"
        '    "prompt": prompt,\n'
        '    "description": f"AI concept derived from: {prompt}",\n'
        '    "imagePrompt": (\n'
        '        f"photorealistic {prompt}, cinematic composition, "\n'
        '        "dramatic lighting, 8k ultra-detailed, sharp focus"\n'
        "    ),\n"
        "}\n"
        "print(json.dumps(result))\n"
    )

    loop = asyncio.get_event_loop()
    result: dict = await loop.run_in_executor(
        None, execute_user_spatial_math, generation_code, 5.0
    )

    if not result["success"]:
        raise HTTPException(status_code=500, detail=result["error"])

    # ── 4. Persist the counter increment ─────────────────────────────────
    _increment_monthly_counter(user_id, db)

    return result["data"]


# ─────────────────────────────────────────────────────────────────────────────
# Image generation endpoint  (Pollinations.ai – no API key required)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/python-api/generate-image")
async def generate_image(request: Request) -> dict:
    """
    Return a Pollinations.ai image URL for the given prompt.

    The URL itself is the image – Pollinations renders on first GET so the
    frontend can drop it straight into an <img> src without any extra fetch.

    Request body  (JSON):
      { "prompt": "<text describing the image>" }

    Response:
      { "imageUrl": "https://image.pollinations.ai/prompt/<encoded>?..." }
    """
    body: dict = await request.json()
    raw_prompt: str = body.get("prompt", "").strip()

    if not raw_prompt:
        raise HTTPException(status_code=400, detail="'prompt' is required.")

    # URL-encode the prompt so spaces and special chars are path-safe.
    encoded_prompt: str = urllib.parse.quote(raw_prompt, safe="")

    image_url = (
        f"https://image.pollinations.ai/prompt/{encoded_prompt}"
        "?width=1024&height=1024&nologo=true"
    )

    return {"imageUrl": image_url}


# ─────────────────────────────────────────────────────────────────────────────
# Code execution endpoint  (non-blocking subprocess with emit_scene support)
# ─────────────────────────────────────────────────────────────────────────────

def _run_user_code_subprocess(code: str, timeout: float = 10.0) -> dict[str, Any]:
    """
    Synchronous helper – runs user code in a subprocess and returns the result.

    Designed to be called via run_in_executor so it never blocks the event loop.
    The emit_scene() helper is injected before the user code and communicates
    via a stderr sentinel that this function extracts.
    """
    emit_helper = (
        "import json as _json, sys as _sys\n"
        "def emit_scene(data):\n"
        '    print(f"__SCENE_DATA__:{_json.dumps(data)}:__SCENE_DATA__", file=_sys.stderr)\n'
        "\n"
    )
    full_code = emit_helper + code

    tmp_path: Optional[str] = None
    scene_data: Optional[str] = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(full_code)
            tmp_path = f.name

        proc = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        stdout = proc.stdout
        stderr_raw = proc.stderr

        marker = "__SCENE_DATA__:"
        if marker in stderr_raw:
            parts = stderr_raw.split(marker)
            if len(parts) >= 3:
                try:
                    scene_data = parts[1]
                    stderr_raw = parts[0] + (parts[2] if len(parts) > 2 else "")
                except Exception:
                    scene_data = None

        return {
            "success": proc.returncode == 0,
            "stdout": stdout,
            "stderr": stderr_raw,
            "sceneData": scene_data,
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Execution timed out after {timeout}s.",
            "sceneData": None,
        }
    except Exception as exc:
        return {
            "success": False,
            "stdout": "",
            "stderr": str(exc),
            "sceneData": None,
        }
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@app.post("/python-api/execute")
async def execute_code(request: Request) -> dict:
    """
    Execute arbitrary Python code submitted by the frontend.

    The blocking subprocess call is dispatched to the thread-pool executor so
    it can never stall the async event loop, satisfying the same isolation goal
    as the multiprocessing sandbox while retaining emit_scene() stderr support.
    """
    body: dict = await request.json()
    code: str = body.get("code", "")
    if not code.strip():
        return {
            "success": False,
            "stdout": "",
            "stderr": "No code provided.",
            "executionTime": 0,
            "sceneData": None,
        }

    await manager.broadcast_to_room("global", {"type": "code_executing", "code": code})

    start = time.perf_counter()

    loop = asyncio.get_event_loop()
    raw = await loop.run_in_executor(None, _run_user_code_subprocess, code, 10.0)

    elapsed = round((time.perf_counter() - start) * 1000, 2)
    payload: dict[str, Any] = {**raw, "executionTime": elapsed}

    await manager.broadcast_to_room("global", {"type": "code_result", "result": payload})
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# Health & stats
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/python-api/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "service": "python-fastapi",
        "version": "2.0.0",
        "db_connected": _engine is not None,
    }


@app.get("/python-api/ws-stats")
async def ws_stats() -> dict:
    total = sum(len(conns) for conns in manager.active_rooms.values())
    return {
        "rooms": {rid: len(conns) for rid, conns in manager.active_rooms.items()},
        "totalConnections": total,
        "messageCount": len(_chat_history),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PYTHON_PORT", "8001"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
