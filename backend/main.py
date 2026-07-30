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
    # Wrapped in try/except: a UniqueViolation on pg_type can occur when the
    # table was partially committed in a prior session; safe to continue.
    try:
        Base.metadata.create_all(bind=_engine, checkfirst=True)
    except Exception as _e:
        print(f"[startup] schema create_all skipped (already exists): {_e}", flush=True)
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
    px: Any, gw: int, gh: int, threshold: int = 40
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

    Threshold raised slightly (35→40) to better capture soft shadow halos
    common in AI-generated art; morphological post-processing (see
    _open_mask) then cleans any resulting ragged edges.
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


def _open_mask(mask: list[list[bool]], gw: int, gh: int) -> list[list[bool]]:
    """
    Morphological opening (1-pixel erosion followed by 1-pixel dilation) using
    a 4-connected structuring element.

    Opening removes isolated single-pixel noise and ragged protrusions at the
    foreground/background boundary without shrinking the core subject area
    (the dilation pass restores what the erosion removed from the interior).

    Using 4-connectivity (not 8) keeps the operation tight and avoids
    over-smoothing fine subject features on the 64×40 grid.
    """
    NEIGHBOURS = ((0, 1), (0, -1), (1, 0), (-1, 0))

    # ── Erosion: a fg pixel stays fg only if all 4-connected in-bounds
    #    neighbours are also fg.  Out-of-bounds neighbours are treated as
    #    bg so edge pixels are always eroded (they are on the image border).
    eroded = [[False] * gw for _ in range(gh)]
    for row in range(gh):
        for col in range(gw):
            if not mask[row][col]:
                continue
            ok = True
            for dr, dc in NEIGHBOURS:
                nr, nc = row + dr, col + dc
                if not (0 <= nr < gh and 0 <= nc < gw) or not mask[nr][nc]:
                    ok = False
                    break
            eroded[row][col] = ok

    # ── Dilation: a pixel becomes fg if any 4-connected neighbour is fg in
    #    the eroded mask.
    dilated = [[False] * gw for _ in range(gh)]
    for row in range(gh):
        for col in range(gw):
            if eroded[row][col]:
                dilated[row][col] = True
                continue
            for dr, dc in NEIGHBOURS:
                nr, nc = row + dr, col + dc
                if 0 <= nr < gh and 0 <= nc < gw and eroded[nr][nc]:
                    dilated[row][col] = True
                    break

    return dilated


def _image_to_volumetric_points(image_url: str) -> list[dict[str, Any]] | None:
    """
    Geometric Hemisphere Projection — local, zero external dependencies.

    For every foreground pixel at pixel coordinate (col, row) the Z-depth
    is driven by the pixel's normalised radial distance from the foreground
    centroid:

        rn     = dist(pixel, centroid) / max_dist_to_silhouette
        geo_z  = sqrt(max(0, 1 − rn²))          ← hemisphere profile
        z_front = Z_MAX × geo_z × (0.8 + 0.2 × color_z)
        z_back  = −Z_MAX × 0.65 × geo_z         ← rear hemisphere

    color_z blends in a small luminance term (darker = slightly more
    prominent) to add surface variation without distorting the overall
    rounded shape.

    Each pixel contributes two points — a front-surface point and a
    rear-surface point (25 % darkened) — creating a thick lenticular
    volume that looks fully rounded from any camera angle.

    Returns a list of {x, y, z, color} dicts ready for JSON broadcast,
    or None if the image is unavailable / not yet rendered.
    """
    if not _PIL_AVAILABLE:
        return None

    GRID_W, GRID_H = 80, 50    # slightly higher res than old mesh grid
    Z_MAX          = 2.0        # max protrusion forward / backward

    # ── Fetch (Pollinations lazy-renders; one attempt, caller retries) ────
    try:
        req = urllib.request.Request(
            image_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; CospaSpatial/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=38) as resp:
            raw_bytes = resp.read()
        if len(raw_bytes) < 1024:
            return None  # stub not yet rendered
    except Exception:
        return None

    # ── Decode & resample ─────────────────────────────────────────────────
    try:
        img = (
            _PILImage.open(io.BytesIO(raw_bytes))
            .convert("RGB")
            .resize((GRID_W, GRID_H), _PILImage.LANCZOS)
        )
        px = img.load()
    except Exception:
        return None

    # ── Foreground mask (BFS flood-fill + morphological opening) ─────────
    fg = _flood_fill_fg_mask(px, GRID_W, GRID_H, threshold=40)
    fg = _open_mask(fg, GRID_W, GRID_H)   # removes boundary noise

    fg_pixels: list[tuple[int, int, int, int, int]] = []   # (col,row,r,g,b)
    for row in range(GRID_H):
        for col in range(GRID_W):
            if fg[row][col]:
                r, g, b = px[col, row]
                fg_pixels.append((col, row, r, g, b))

    # Safety: subject touches corners → disable masking, use full grid
    if len(fg_pixels) < GRID_W * GRID_H * 0.10:
        fg_pixels = []
        for row in range(GRID_H):
            for col in range(GRID_W):
                r, g, b = px[col, row]
                fg_pixels.append((col, row, r, g, b))

    n = len(fg_pixels)
    if n == 0:
        return None

    # ── Foreground centroid in pixel space ────────────────────────────────
    cx = sum(p[0] for p in fg_pixels) / n
    cy = sum(p[1] for p in fg_pixels) / n
    max_r = max(
        math.sqrt((p[0] - cx) ** 2 + (p[1] - cy) ** 2) for p in fg_pixels
    )
    if max_r < 1e-6:
        max_r = 1.0

    # ── Pixel → centroid-centred world coords ─────────────────────────────
    x_scale = 4.0 / (GRID_W - 1)    # col  → world X  (full range 4 units)
    y_scale = 2.5 / (GRID_H - 1)    # row  → world Y  (full range 2.5 units)
    cx_w    =  cx * x_scale - 2.0
    cy_w    = -(cy * y_scale - 1.25)  # image Y is flipped vs world Y

    # ── Build volumetric point pairs (front + back hemisphere) ───────────
    points: list[dict[str, Any]] = []
    for col, row, r, g, b in fg_pixels:
        # World position, centred on foreground centroid
        wx = (col * x_scale - 2.0) - cx_w
        wy = -(row * y_scale - 1.25) - cy_w

        # Normalised radial distance: 0 at centroid, 1 at silhouette
        dr = math.sqrt((col - cx) ** 2 + (row - cy) ** 2)
        rn = dr / max_r

        # Hemisphere Z profile
        geo_z = math.sqrt(max(0.0, 1.0 - rn * rn))

        # Luminance blend for subtle surface variation
        r_f, g_f, b_f = r / 255.0, g / 255.0, b / 255.0
        luma    = 0.299 * r_f + 0.587 * g_f + 0.114 * b_f
        color_z = 1.0 - luma   # darker pixels protrude slightly more

        z_front = Z_MAX * geo_z * (0.8 + 0.2 * color_z)
        z_back  = -Z_MAX * 0.65 * geo_z

        color_front = (r << 16) | (g << 8) | b
        color_back  = (max(0, r - 64) << 16) | (max(0, g - 64) << 8) | max(0, b - 64)

        points.append({"x": round(wx, 3), "y": round(wy, 3),
                       "z": round(z_front, 3), "color": color_front})
        points.append({"x": round(wx, 3), "y": round(wy, 3),
                       "z": round(z_back,  3), "color": color_back})

    return points


async def _process_image_to_scene(image_url: str, room_id: str = "global") -> None:
    """
    Background async task — geometric hemisphere projection pipeline.

    Calls _image_to_volumetric_points() (synchronous, runs in executor)
    up to MAX_ATTEMPTS times with RETRY_DELAY_S between each attempt.
    Pollinations lazy-renders; the image is typically ready 15-30 s
    after the URL is first requested.

    Broadcasts type="points"; falls back to the rainbow-helix
    confirmation cloud if all attempts fail.
    Never propagates exceptions.
    """
    MAX_ATTEMPTS  = 4
    RETRY_DELAY_S = 12.0

    try:
        loop = asyncio.get_event_loop()
        t0   = time.perf_counter()
        points: list[dict[str, Any]] | None = None

        for attempt in range(MAX_ATTEMPTS):
            if attempt > 0:
                await asyncio.sleep(RETRY_DELAY_S)
            try:
                points = await loop.run_in_executor(
                    None, _image_to_volumetric_points, image_url
                )
            except Exception:
                points = None
            if points is not None:
                break

        if points is None:
            points = _make_fallback_points()

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        await manager.broadcast_to_room(room_id, {
            "type":          "image_scene",
            "sceneData":     json.dumps({"type": "points", "points": points}),
            "pointCount":    len(points),
            "executionTime": elapsed_ms,
        })

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
