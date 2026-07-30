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
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from typing import Any, Generator, Optional

try:
    from PIL import Image as _PILImage
    _PIL_AVAILABLE = True
except ImportError:
    _PILImage = None  # type: ignore[assignment]
    _PIL_AVAILABLE = False

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
# Image → 3D point-cloud pipeline
# ─────────────────────────────────────────────────────────────────────────────

import math as _math


def _make_fallback_points() -> list[dict[str, Any]]:
    """
    Geometric fallback broadcast when the image fetch fails or returns a
    blank frame.  Produces a rainbow double-helix (~300 points) that looks
    intentional rather than broken, and confirms the WebSocket pipeline is
    alive while Pollinations finishes rendering.
    """
    points: list[dict[str, Any]] = []
    N = 300
    for i in range(N):
        try:
            t = i / (N - 1)
            angle = t * 6.0 * _math.pi
            radius = 0.5 + 0.5 * _math.sin(t * _math.pi)
            # Two interleaved strands
            for strand_offset in (0.0, _math.pi):
                a = angle + strand_offset
                x = round(radius * _math.cos(a) * 2.2, 4)
                y = round(t * 3.0 - 1.5, 4)
                z = round(radius * _math.sin(a) * 2.0, 4)
                # Rainbow hue cycling
                h6 = (t + strand_offset / (2.0 * _math.pi)) * 6.0 % 6.0
                hi = int(h6)
                f = h6 - hi
                rgb_table = [
                    (1.0, f, 0.0), (1.0 - f, 1.0, 0.0),
                    (0.0, 1.0, f), (0.0, 1.0 - f, 1.0),
                    (f, 0.0, 1.0), (1.0, 0.0, 1.0 - f),
                ]
                rv, gv, bv = rgb_table[hi % 6]
                color = f"#{int(rv * 255):02x}{int(gv * 255):02x}{int(bv * 255):02x}"
                points.append({"x": x, "y": y, "z": z, "color": color})
        except Exception:
            continue  # skip any single bad iteration, keep going
    return points


def _flood_fill_fg_mask(
    px: Any, gw: int, gh: int, threshold: int = 35
) -> list[list[bool]]:
    """
    BFS flood-fill from the 4 corner pixels to identify background.

    Seeds from all four corners, growing to 4-connected neighbours whose
    colour is within ``threshold`` (per-channel Euclidean) of the average
    corner colour.  Returns ``fg[row][col] = True`` for foreground pixels.

    Works well for Pollinations images whose backgrounds are solid or
    near-white / soft-gradient — the typical case for AI concept art.
    Falls back gracefully: if the corners land on a non-white subject the
    threshold won't propagate far and most pixels remain foreground.
    """
    corner_coords = [(0, 0), (gw - 1, 0), (0, gh - 1), (gw - 1, gh - 1)]
    sr, sg, sb = 0, 0, 0
    for cx, cy in corner_coords:
        try:
            r, g, b = px[cx, cy]
        except Exception:
            r, g, b = 255, 255, 255
        sr += r; sg += g; sb += b
    sr //= 4; sg //= 4; sb //= 4

    thr_sq = threshold * threshold * 3   # sum of squared per-channel deltas

    is_bg  = [[False] * gw for _ in range(gh)]
    visited = [[False] * gw for _ in range(gh)]
    queue: list[tuple[int, int]] = []

    for cx, cy in corner_coords:
        if not visited[cy][cx]:
            is_bg[cy][cx] = visited[cy][cx] = True
            queue.append((cx, cy))

    head = 0
    while head < len(queue):
        x, y = queue[head]; head += 1
        for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < gw and 0 <= ny < gh and not visited[ny][nx]:
                visited[ny][nx] = True
                try:
                    r, g, b = px[nx, ny]
                except Exception:
                    continue
                if (r - sr) ** 2 + (g - sg) ** 2 + (b - sb) ** 2 <= thr_sq:
                    is_bg[ny][nx] = True
                    queue.append((nx, ny))

    return [[not is_bg[row][col] for col in range(gw)] for row in range(gh)]


def _image_to_mesh(image_url: str) -> dict[str, Any] | None:
    """
    Synchronous helper (runs in a thread-pool executor).

    Converts the rendered Pollinations image into a **solid closed 3D mesh**
    of the main subject only — background pixels are excluded entirely.

    Pipeline
    --------
    1. Fetch + resize to GRID_W × GRID_H.
    2. BFS flood-fill from the 4 corners to build a foreground mask that
       isolates the main subject (works well for AI concept art with white
       or soft-gradient backgrounds).  Falls back to the full grid if fewer
       than 10 % of pixels are detected as foreground (robust against edge
       cases where the subject touches all corners).
    3. Remap vertex indices — only foreground pixels become vertices so the
       output mesh contains zero background geometry.
    4. Depth driver per foreground vertex:
           depth = 0.6 × (1 − luma) + 0.4 × saturation
       Subject colour and dark edges protrude forward; no white floor.
    5. Bottom plate + boundary walls are added only for foreground cells,
       sealing the subject into a fully closed solid manifold.

    Colors: packed (r<<16|g<<8|b); frontend unpacks to BufferAttribute.
    Returns None when the image is unavailable / not yet rendered.
    """
    GRID_W, GRID_H = 64, 40
    Z_SCALE  = 4.0   # surface z range: [−2.0, +2.0]
    Z_BOTTOM = -2.8  # bottom plate z

    if not _PIL_AVAILABLE:
        return None

    # ── Fetch ─────────────────────────────────────────────────────────────
    try:
        req = urllib.request.Request(
            image_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; CospaSpatial/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=38) as resp:
            raw_bytes = resp.read()
        if len(raw_bytes) < 1024:
            return None  # stub / not ready → caller retries
    except Exception:
        return None

    # ── Decode & resample ─────────────────────────────────────────────────
    try:
        img = _PILImage.open(io.BytesIO(raw_bytes)).convert("RGB")
        img = img.resize((GRID_W, GRID_H), _PILImage.LANCZOS)
        px = img.load()
    except Exception:
        return None

    # ── Sample pixel grid ─────────────────────────────────────────────────
    surface_rgb: list[list[tuple[int, int, int]]] = []
    for row in range(GRID_H):
        row_rgb: list[tuple[int, int, int]] = []
        for col in range(GRID_W):
            try:
                row_rgb.append(px[col, row])
            except Exception:
                row_rgb.append((100, 100, 120))
        surface_rgb.append(row_rgb)

    # ── Foreground mask ───────────────────────────────────────────────────
    fg = _flood_fill_fg_mask(px, GRID_W, GRID_H, threshold=35)

    # Fallback: if fewer than 10 % of pixels are foreground, the corners
    # probably landed on the subject — disable masking for this image.
    fg_count = sum(fg[r][c] for r in range(GRID_H) for c in range(GRID_W))
    if fg_count < GRID_W * GRID_H * 0.10:
        fg = [[True] * GRID_W for _ in range(GRID_H)]

    # ── Build foreground-only vertex arrays (remapped indices) ─────────────
    # surf_idx[(row,col)] → new vertex index  (top surface, fg only)
    # bot_idx [(row,col)] → new vertex index  (bottom plate, fg only)
    verts:    list[float] = []
    colors:   list[int]   = []
    surf_idx: dict[tuple[int, int], int] = {}
    bot_idx:  dict[tuple[int, int], int] = {}

    for row in range(GRID_H):
        for col in range(GRID_W):
            if not fg[row][col]:
                continue
            r, g, b = surface_rgb[row][col]
            r_f, g_f, b_f = r / 255.0, g / 255.0, b / 255.0
            luma  = 0.299 * r_f + 0.587 * g_f + 0.114 * b_f
            cmax  = max(r_f, g_f, b_f)
            cmin  = min(r_f, g_f, b_f)
            sat   = (cmax - cmin) / cmax if cmax > 1e-6 else 0.0
            depth = 0.6 * (1.0 - luma) + 0.4 * sat
            x = round((col / (GRID_W - 1) - 0.5) * 4.8, 3)
            y = round((0.5 - row / (GRID_H - 1)) * 3.0, 3)
            z = round(depth * Z_SCALE - Z_SCALE / 2.0, 3)
            surf_idx[(row, col)] = len(verts) // 3
            verts.extend([x, y, z])
            colors.append((r << 16) | (g << 8) | b)

    for row in range(GRID_H):
        for col in range(GRID_W):
            if not fg[row][col]:
                continue
            r, g, b = surface_rgb[row][col]
            x = round((col / (GRID_W - 1) - 0.5) * 4.8, 3)
            y = round((0.5 - row / (GRID_H - 1)) * 3.0, 3)
            bot_idx[(row, col)] = len(verts) // 3
            verts.extend([x, y, Z_BOTTOM])
            dr, dg, db = r >> 2, g >> 2, b >> 2   # 25 % darkened
            colors.append((dr << 16) | (dg << 8) | db)

    if not verts:
        return None   # nothing to render

    # ── Build face index array ────────────────────────────────────────────
    faces: list[int] = []

    # Top surface: only quads where all 4 corners are foreground
    for row in range(GRID_H - 1):
        for col in range(GRID_W - 1):
            rc = [(row, col), (row, col + 1), (row + 1, col), (row + 1, col + 1)]
            if all(k in surf_idx for k in rc):
                v00, v10, v01, v11 = (surf_idx[k] for k in rc)
                faces.extend([v00, v10, v11,  v00, v11, v01])

    # Bottom plate: same quads, reversed winding (faces downward)
    for row in range(GRID_H - 1):
        for col in range(GRID_W - 1):
            rc = [(row, col), (row, col + 1), (row + 1, col), (row + 1, col + 1)]
            if all(k in bot_idx for k in rc):
                b00, b10, b01, b11 = (bot_idx[k] for k in rc)
                faces.extend([b00, b11, b10,  b00, b01, b11])

    # Boundary walls: seal the subject's silhouette edge to the bottom plate.
    # A wall segment is needed wherever a foreground vertex-pair borders a
    # background cell (or the image edge).
    for row in range(GRID_H - 1):
        for col in range(GRID_W):
            if (row, col) not in surf_idx or (row + 1, col) not in surf_idx:
                continue
            t0, t1 = surf_idx[(row, col)],     surf_idx[(row + 1, col)]
            b0, b1 = bot_idx [(row, col)],     bot_idx [(row + 1, col)]
            # Wall on the left side of this column
            if col == 0 or (row, col - 1) not in surf_idx:
                faces.extend([t0, b0, b1,  t0, b1, t1])
            # Wall on the right side of this column
            if col == GRID_W - 1 or (row, col + 1) not in surf_idx:
                faces.extend([t0, t1, b1,  t0, b1, b0])

    for row in range(GRID_H):
        for col in range(GRID_W - 1):
            if (row, col) not in surf_idx or (row, col + 1) not in surf_idx:
                continue
            t0, t1 = surf_idx[(row, col)],     surf_idx[(row, col + 1)]
            b0, b1 = bot_idx [(row, col)],     bot_idx [(row, col + 1)]
            # Wall on the top edge of this row
            if row == 0 or (row - 1, col) not in surf_idx:
                faces.extend([t0, t1, b1,  t0, b1, b0])
            # Wall on the bottom edge of this row
            if row == GRID_H - 1 or (row + 1, col) not in surf_idx:
                faces.extend([t0, b0, b1,  t0, b1, t1])

    # ── Bounding-box normalisation ────────────────────────────────────────
    # Fit the final mesh into a well-proportioned bounding volume:
    #   • XY axes scaled uniformly so the widest axis fills [-2.0, +2.0].
    #   • Z axis scaled independently so depth ≤ 12 % of XY width,
    #     preventing the depth-displacement from stretching the model.
    # All coordinates are re-centred at the origin first.
    TARGET   = 2.0          # half-extent target on each axis
    Z_PCT    = 0.12         # max Z depth as fraction of normalised XY span

    xs = verts[0::3]
    ys = verts[1::3]
    zs = verts[2::3]

    x_ctr = (max(xs) + min(xs)) / 2.0
    y_ctr = (max(ys) + min(ys)) / 2.0
    z_ctr = (max(zs) + min(zs)) / 2.0

    xy_half   = max((max(xs) - min(xs)) / 2.0,
                    (max(ys) - min(ys)) / 2.0,
                    1e-6)
    xy_scale  = TARGET / xy_half               # maps largest XY axis → ±2.0

    z_raw_half   = max((max(zs) - min(zs)) / 2.0, 1e-6)
    z_max_half   = TARGET * 2.0 * Z_PCT        # 12 % of 4.0 = 0.48 → ±0.24
    z_scale      = min(z_max_half / z_raw_half, xy_scale)  # never exceed XY scale

    for i in range(len(verts) // 3):
        verts[i * 3]     = round((verts[i * 3]     - x_ctr) * xy_scale, 3)
        verts[i * 3 + 1] = round((verts[i * 3 + 1] - y_ctr) * xy_scale, 3)
        verts[i * 3 + 2] = round((verts[i * 3 + 2] - z_ctr) * z_scale,  3)

    return {"type": "mesh", "vertices": verts, "faces": faces, "colors": colors}


async def _process_image_to_scene(image_url: str, room_id: str = "global") -> None:
    """
    Background async task: retry-fetch → build mesh → broadcast.

    Tries up to MAX_ATTEMPTS times with RETRY_DELAY_S seconds between
    attempts (Pollinations lazy-renders; typically ready in 15–30 s).
    On success, broadcasts a ``type: "mesh"`` payload with vertices, faces,
    and packed per-vertex colors so Three.js renders a solid textured object.
    After all retries, falls back to the rainbow-helix point cloud so the
    WebSocket bridge is always confirmed live.  Never propagates exceptions.
    """
    MAX_ATTEMPTS  = 4
    RETRY_DELAY_S = 12.0   # attempts at 0 s, 12 s, 24 s, 36 s

    try:
        loop = asyncio.get_event_loop()
        t0 = time.perf_counter()
        mesh: dict[str, Any] | None = None

        for attempt in range(MAX_ATTEMPTS):
            if attempt > 0:
                await asyncio.sleep(RETRY_DELAY_S)
            try:
                mesh = await loop.run_in_executor(None, _image_to_mesh, image_url)
            except Exception:
                mesh = None
            if mesh is not None:
                break

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

        if mesh is not None:
            # Solid mesh from image
            scene_json = json.dumps(mesh)
            face_count  = len(mesh["faces"]) // 3
        else:
            # Geometric fallback — confirms WebSocket is alive
            fallback_pts = _make_fallback_points()
            scene_json   = json.dumps({"type": "points", "points": fallback_pts})
            face_count   = len(fallback_pts)

        await manager.broadcast_to_room(
            room_id,
            {
                "type": "image_scene",
                "sceneData": scene_json,
                "pointCount": face_count,
                "executionTime": elapsed_ms,
            },
        )

    except Exception:
        pass  # background task — never propagate to the event loop


# ─────────────────────────────────────────────────────────────────────────────
# Image generation endpoint  (Pollinations.ai – no API key required)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/python-api/generate-image")
async def generate_image(request: Request) -> dict:
    """
    Return a Pollinations.ai image URL for the given prompt, then kick off a
    background task that fetches the rendered image, extracts a luminance-
    depth point cloud, and broadcasts it as an ``image_scene`` WebSocket
    frame so the Three.js viewport materialises the 2D image as a 3D model.

    Request body  (JSON):
      { "prompt": "<text>", "room_id": "<room>" }   (room_id defaults to "global")

    Response (immediate, before the background task completes):
      { "imageUrl": "https://image.pollinations.ai/prompt/<encoded>?..." }
    """
    body: dict = await request.json()
    raw_prompt: str = body.get("prompt", "").strip()
    room_id: str = body.get("room_id", "global")

    if not raw_prompt:
        raise HTTPException(status_code=400, detail="'prompt' is required.")

    # Normalise the prompt: lowercase, spaces → hyphens, then percent-encode
    # any remaining special characters so the path is always valid.
    normalised: str = raw_prompt.lower().replace(" ", "-")
    encoded_prompt: str = urllib.parse.quote(normalised, safe="-")

    image_url = (
        f"https://image.pollinations.ai/prompt/{encoded_prompt}"
        "?width=1024&height=1024&nologo=true"
    )

    # Fire-and-forget: fetch + extract + broadcast runs after we respond.
    # Pollinations renders the image on first GET, so the background task
    # uses the same URL the frontend will display in <img src=...>.
    asyncio.create_task(_process_image_to_scene(image_url, room_id))

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
        '    print(f"__SCENE_DATA__:{_json.dumps(data)}", file=_sys.stderr)\n'
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

        # Extract __SCENE_DATA__:<json> lines written by emit_scene() to stderr.
        # Each such line is stripped from the visible stderr so it doesn't appear
        # in the console output.  If emit_scene() is called multiple times, the
        # last call wins (most recent scene replaces earlier ones).
        marker = "__SCENE_DATA__:"
        if marker in stderr_raw:
            scene_lines: list[str] = []
            other_lines: list[str] = []
            for line in stderr_raw.splitlines(keepends=True):
                stripped = line.rstrip("\r\n")
                if stripped.startswith(marker):
                    scene_lines.append(stripped[len(marker):])
                else:
                    other_lines.append(line)
            if scene_lines:
                scene_data = scene_lines[-1]   # last emit_scene() call wins
            stderr_raw = "".join(other_lines)

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
