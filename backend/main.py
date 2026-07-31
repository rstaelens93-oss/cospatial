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
import re
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


# ─────────────────────────────────────────────────────────────────────────────
# LLM code-generation engine  (Groq free tier — zero cost)
# ─────────────────────────────────────────────────────────────────────────────

_GROQ_API_KEY: str = os.environ.get("GROQ_API_KEY", "")

_LLM_SYSTEM_PROMPT = """\
You are a Python code generator for 3D point-cloud visualizations.
Output ONLY raw Python code. No markdown fences. No prose. No explanations.
One optional comment at the very top (# shape title) is allowed.

══════════════════ ABSOLUTE RULES ══════════════════
1. DO NOT write any import statement. `math` is already available.
2. DO NOT hardcode coordinate lists. NEVER write points = [{...},{...},...].
   Every point MUST be computed inside a `for` loop using math formulas.
3. DO NOT define functions or classes. Top-level sequential code only.
4. Keep total points between 800 and 1200 — enough for visual density,
   small enough to never run out of tokens before finishing.
5. The VERY LAST line of code must be exactly:
     emit_scene({"type": "points", "points": points, "color": "#rrggbb"})
   Nothing may follow it.
════════════════ REQUIRED STRUCTURE ════════════════
points = []
for i in range(<N>):
    <parametric math with math.sin / math.cos / math.sqrt / math.pi>
    x = ...
    y = ...
    z = ...
    color = "#{:02x}{:02x}{:02x}".format(<r>, <g>, <b>)
    points.append({"x": round(x, 3), "y": round(y, 3), "z": round(z, 3), "color": color})
emit_scene({"type": "points", "points": points, "color": "#rrggbb"})
════════════════ GEOMETRY GUIDANCE ═════════════════
Model the 3D shape described by the user with custom math:
  • Drone / quadcopter → 4 rotor discs (polar loops at offset positions) + slim box fuselage
  • Ship / boat        → hull parabolic cross-section + waterline ring + mast vertical line
  • Cathedral          → stacked arch rings that narrow toward the top + two tower columns
  • Robot              → torso box + spherical head + four limb cylinders
  • Network graph      → nodes on a sphere + edges as straight interpolated line segments
Assign gradient colors by height (y), radial distance, or structural zone.
Avoid single-color fills — color should reveal the shape's geometry.\
"""


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

        # ── Layer quantization ─────────────────────────────────────────────
        # Snap Z to discrete topographic slices so the geometry reads as
        # clean stacked contours in CAD/point-cloud tools.
        # Front: 10 layers spanning [0, Z_MAX]; step ≈ 0.20 units.
        # Back:   6 layers spanning [0, -Z_MAX×0.65]; step ≈ 0.22 units.
        N_FRONT, N_BACK = 10, 6
        z_step_f = Z_MAX / N_FRONT
        z_step_b = (Z_MAX * 0.65) / N_BACK
        # Clamp to at least one layer so centroid points are never at z=0.
        z_front = round(max(round(z_front / z_step_f), 1) * z_step_f, 3)
        z_back  = round(-max(round(abs(z_back) / z_step_b), 1) * z_step_b, 3)

        color_front = (r << 16) | (g << 8) | b
        color_back  = (max(0, r - 64) << 16) | (max(0, g - 64) << 8) | max(0, b - 64)

        points.append({"x": round(wx, 3), "y": round(wy, 3),
                       "z": round(z_front, 3), "color": color_front})
        points.append({"x": round(wx, 3), "y": round(wy, 3),
                       "z": round(z_back,  3), "color": color_back})

    return points


def _build_editor_script(prompt: str) -> str:
    """
    Strict dictionary keyword → script mapper.

    Extracts whole words from the prompt (punctuation stripped), then checks
    each entry in KEYWORD_MAP in order.  First entry whose keyword set has a
    non-empty intersection with the prompt words wins.

    Using whole-word set intersection (``words & keys``) instead of substring
    search (``k in prompt``) prevents false positives such as "block" matching
    "blockchain" or "cube" matching "cubic" in unrelated concepts.
    """
    # ── Whole-word extraction — strips punctuation so "station." → "station" ─
    words: set[str] = set(
        "".join(c if c.isalnum() or c == " " else " " for c in prompt.lower()).split()
    )

    # ── Script bodies ────────────────────────────────────────────────────────

    RING = (
        "# Point cloud: flat circular ring\n"
        "import math\n"
        "points = []\n"
        "for i in range(360):\n"
        "    theta = math.radians(i)\n"
        "    for radius in [1.2, 1.5, 1.8, 2.1]:\n"
        "        x = math.cos(theta) * radius\n"
        "        z = math.sin(theta) * radius\n"
        "        h = i / 360\n"
        '        color = "#{:02x}{:02x}{:02x}".format(\n'
        "            int(h * 0xff), int(0x88 + h * 0x44), int(0xff - h * 0x88))\n"
        '        points.append({"x": round(x, 3), "y": 0.0, "z": round(z, 3), "color": color})\n'
        'emit_scene({"type": "points", "points": points, "color": "#ffaa00"})\n'
    )

    CUBE = (
        "# Point cloud: cube (6 faces)\n"
        "import math\n"
        "points = []\n"
        "SIDE = 14\n"
        "for face in range(6):\n"
        "    for i in range(SIDE):\n"
        "        for j in range(SIDE):\n"
        "            u = (i / (SIDE - 1)) * 2 - 1\n"
        "            v = (j / (SIDE - 1)) * 2 - 1\n"
        "            coords = [(u,v,1),(u,v,-1),(1,u,v),(-1,u,v),(u,1,v),(u,-1,v)]\n"
        "            x, y, z = coords[face]\n"
        "            h = face / 5\n"
        '            color = "#{:02x}{:02x}{:02x}".format(\n'
        "                int(0x44 + h * 0xaa), int(0x88 + h * 0x44), int(0xcc - h * 0x44))\n"
        '            points.append({"x": round(x*2,3), "y": round(y*2,3), "z": round(z*2,3), "color": color})\n'
        'emit_scene({"type": "points", "points": points, "color": "#4488ff"})\n'
    )

    STATION = (
        "# Point cloud: space station (hub + 4 arms + outer ring)\n"
        "import math\n"
        "points = []\n"
        "# Central hub — Fibonacci sphere\n"
        "N = 400\n"
        "phi_gold = (1 + math.sqrt(5)) / 2\n"
        "for i in range(N):\n"
        "    y = 1 - (i / (N - 1)) * 2\n"
        "    r = math.sqrt(max(0.0, 1 - y * y))\n"
        "    theta = 2 * math.pi * i / phi_gold\n"
        "    points.append({\"x\": round(math.cos(theta)*r*0.5, 3),\n"
        "                   \"y\": round(y * 0.5, 3),\n"
        "                   \"z\": round(math.sin(theta)*r*0.5, 3),\n"
        "                   \"color\": \"#44aacc\"})\n"
        "# Radial arms\n"
        "for arm in range(4):\n"
        "    angle = math.pi / 2 * arm\n"
        "    for i in range(60):\n"
        "        t = i / 59\n"
        "        x = math.cos(angle) * t * 2.0\n"
        "        z = math.sin(angle) * t * 2.0\n"
        "        c = int(0x44 + t * 0xaa)\n"
        "        points.append({\"x\": round(x, 3), \"y\": round((i % 3) * 0.05 - 0.05, 3),\n"
        "                       \"z\": round(z, 3), \"color\": \"#{:02x}aa{:02x}\".format(c, c)})\n"
        "# Outer ring\n"
        "for i in range(300):\n"
        "    theta = 2 * math.pi * i / 300\n"
        "    x = math.cos(theta) * 2.2\n"
        "    z = math.sin(theta) * 2.2\n"
        "    wave = round(math.sin(theta * 8) * 0.08, 3)\n"
        "    points.append({\"x\": round(x, 3), \"y\": wave, \"z\": round(z, 3), \"color\": \"#00d4ff\"})\n"
        'emit_scene({"type": "points", "points": points, "color": "#44aacc"})\n'
    )

    SPHERE = (
        "# Point cloud: sphere (Fibonacci lattice)\n"
        "import math\n"
        "points = []\n"
        "N = 2000\n"
        "phi_gold = (1 + math.sqrt(5)) / 2\n"
        "for i in range(N):\n"
        "    y = 1 - (i / (N - 1)) * 2\n"
        "    r = math.sqrt(max(0.0, 1 - y * y))\n"
        "    theta = 2 * math.pi * i / phi_gold\n"
        "    x = math.cos(theta) * r\n"
        "    z = math.sin(theta) * r\n"
        "    h = i / N\n"
        '    color = "#{:02x}{:02x}{:02x}".format(\n'
        "        int(0x22 + h * 0xcc), int(0xaa + h * 0x44), int(0xff - h * 0x66))\n"
        '    points.append({"x": round(x * 2, 3), "y": round(y * 2, 3), "z": round(z * 2, 3), "color": color})\n'
        'emit_scene({"type": "points", "points": points, "color": "#00ffcc"})\n'
    )

    TORUS = (
        "# Point cloud: torus\n"
        "import math\n"
        "points = []\n"
        "R, r = 1.5, 0.55\n"
        "for i in range(80):\n"
        "    theta = 2 * math.pi * i / 80\n"
        "    for j in range(40):\n"
        "        phi = 2 * math.pi * j / 40\n"
        "        x = (R + r * math.cos(phi)) * math.cos(theta)\n"
        "        y = r * math.sin(phi)\n"
        "        z = (R + r * math.cos(phi)) * math.sin(theta)\n"
        "        h = i / 80\n"
        '        color = "#{:02x}{:02x}{:02x}".format(\n'
        "            int(0xff * h), int(0x44 + 0xaa * h), int(0xff * (1 - h)))\n"
        '        points.append({"x": round(x,3), "y": round(y,3), "z": round(z,3), "color": color})\n'
        'emit_scene({"type": "points", "points": points, "color": "#ff44ff"})\n'
    )

    SPIRAL = (
        "# Point cloud: double helix spiral\n"
        "import math\n"
        "points = []\n"
        "for strand in range(2):\n"
        "    offset = math.pi * strand\n"
        "    for i in range(900):\n"
        "        t = i / 900 * 6 * math.pi\n"
        "        radius = 0.4 + t * 0.06\n"
        "        x = math.cos(t + offset) * radius\n"
        "        z = math.sin(t + offset) * radius\n"
        "        y = t * 0.13 - 2.5\n"
        "        h = i / 900\n"
        "        r_c = int(0x22 + h * 0xdd) if strand == 0 else int(0xff - h * 0xdd)\n"
        '        color = "#{:02x}{:02x}{:02x}".format(r_c, int(0xff - h * 0xaa), int(0x88 + h * 0x77))\n'
        '        points.append({"x": round(x,3), "y": round(y,3), "z": round(z,3), "color": color})\n'
        'emit_scene({"type": "points", "points": points, "color": "#00ffcc"})\n'
    )

    PYRAMID = (
        "# Point cloud: layered cone / pyramid\n"
        "import math\n"
        "points = []\n"
        "LAYERS = 14\n"
        "for layer in range(LAYERS):\n"
        "    y = layer / LAYERS * 4 - 2\n"
        "    radius = (1 - layer / LAYERS) * 2.2\n"
        "    n = max(4, int(48 * (1 - layer / LAYERS)))\n"
        "    for i in range(n):\n"
        "        a = 2 * math.pi * i / n\n"
        "        x = math.cos(a) * radius\n"
        "        z = math.sin(a) * radius\n"
        "        h = layer / LAYERS\n"
        '        color = "#{:02x}{:02x}{:02x}".format(\n'
        "            int(0xff * (1 - h)), int(0x44 + 0x88 * h), int(0x22 + 0x88 * h))\n"
        '        points.append({"x": round(x,3), "y": round(y,3), "z": round(z,3), "color": color})\n'
        'emit_scene({"type": "points", "points": points, "color": "#ffaa00"})\n'
    )

    CYLINDER = (
        "# Point cloud: cylinder with caps\n"
        "import math\n"
        "points = []\n"
        "RINGS, PER_RING = 20, 60\n"
        "for ring in range(RINGS):\n"
        "    y = ring / (RINGS - 1) * 4 - 2\n"
        "    for i in range(PER_RING):\n"
        "        a = 2 * math.pi * i / PER_RING\n"
        "        x = math.cos(a) * 1.6\n"
        "        z = math.sin(a) * 1.6\n"
        "        h = ring / RINGS\n"
        '        color = "#{:02x}{:02x}{:02x}".format(\n'
        "            int(0x22 + h * 0xcc), int(0x88 + h * 0x44), int(0xff - h * 0x88))\n"
        '        points.append({"x": round(x,3), "y": round(y,3), "z": round(z,3), "color": color})\n'
        "for i in range(240):\n"
        "    a = 2 * math.pi * i / 240\n"
        "    r = (i % 20) / 20 * 1.6\n"
        "    for cy in [-2.0, 2.0]:\n"
        '        points.append({"x": round(math.cos(a)*r,3), "y": cy, "z": round(math.sin(a)*r,3), "color": "#44aaff"})\n'
        'emit_scene({"type": "points", "points": points, "color": "#44aaff"})\n'
    )

    TREE = (
        "# Point cloud: recursive branching tree\n"
        "import math\n"
        "points = []\n"
        "\n"
        "def add_branch(x, y, z, axz, tilt, length, depth):\n"
        "    if depth == 0 or length < 0.08:\n"
        "        return\n"
        "    steps = max(1, int(length * 16))\n"
        "    for i in range(steps):\n"
        "        t = i / steps\n"
        "        bx = x + math.sin(axz) * math.sin(tilt) * t * length\n"
        "        by = y + math.cos(tilt) * t * length\n"
        "        bz = z + math.cos(axz) * math.sin(tilt) * t * length\n"
        "        g = int(0x55 + depth * 0x1a)\n"
        '        points.append({"x": round(bx,3), "y": round(by,3), "z": round(bz,3), "color": "#1a{:02x}1a".format(g)})\n'
        "    nx = x + math.sin(axz) * math.sin(tilt) * length\n"
        "    ny = y + math.cos(tilt) * length\n"
        "    nz = z + math.cos(axz) * math.sin(tilt) * length\n"
        "    for da, dt in [(-0.55, 0.28), (0.55, 0.28), (0.0, 0.08)]:\n"
        "        add_branch(nx, ny, nz, axz + da, tilt + dt, length * 0.63, depth - 1)\n"
        "\n"
        "add_branch(0, -2.2, 0, 0, 0.08, 1.6, 5)\n"
        'emit_scene({"type": "points", "points": points[:2500], "color": "#22aa22"})\n'
    )

    # ── Strict keyword → script mapping (checked in order; first match wins) ─
    # Each entry: (frozenset of exact trigger words, script string).
    # Words are matched as whole tokens — "ring" will NOT match "earring" or
    # "ringmaster" because we split on word boundaries before comparing.
    KEYWORD_MAP: list[tuple[frozenset[str], str]] = [
        # Most-specific shapes first to prevent partial overlap with defaults.
        (frozenset({"station", "outpost", "habitat", "orbital"}),      STATION),
        (frozenset({"ring", "hoop", "circle", "loop"}),                RING),
        (frozenset({"torus", "donut", "doughnut"}),                    TORUS),
        (frozenset({"cube", "box"}),                                   CUBE),
        (frozenset({"sphere", "ball", "planet", "earth", "moon",
                    "globe", "orb"}),                                  SPHERE),
        (frozenset({"spiral", "helix", "galaxy", "tornado",
                    "vortex", "swirl", "coil"}),                       SPIRAL),
        (frozenset({"pyramid", "cone", "mountain", "volcano",
                    "peak"}),                                          PYRAMID),
        (frozenset({"cylinder", "tower", "pillar", "column",
                    "tube", "pipe", "barrel"}),                        CYLINDER),
        (frozenset({"tree", "plant", "flower", "forest",
                    "branch", "leaf", "fern", "nature"}),              TREE),
        # Broader architectural terms last — only reached if none of the
        # more specific shape words appear in the prompt.
        (frozenset({"building", "architecture", "city", "castle",
                    "block", "structure"}),                            CUBE),
    ]

    for trigger_words, script in KEYWORD_MAP:
        if words & trigger_words:          # non-empty intersection → match
            return script

    # ── Default: (2,3) torus knot — visually interesting for any subject ─────
    safe = "".join(c if c.isalnum() or c in " -" else "" for c in prompt)[:60].strip()
    return (
        f"# Auto-generated point cloud for: {safe}\n"
        "import math\n"
        "points = []\n"
        "for i in range(2000):\n"
        "    t = i / 2000 * 2 * math.pi\n"
        "    theta, phi = t * 2, t * 3\n"
        "    R, r = 1.6, 0.6\n"
        "    x = (R + r * math.cos(phi)) * math.cos(theta)\n"
        "    y = (R + r * math.cos(phi)) * math.sin(theta)\n"
        "    z = r * math.sin(phi)\n"
        "    h = i / 2000\n"
        '    color = "#{:02x}{:02x}{:02x}".format(\n'
        "        int(h * 0xff), int(0xd4 - h * 0x50), int(0xff - h * 0x66))\n"
        '    points.append({"x": round(x,3), "y": round(y,3), "z": round(z,3), "color": color})\n'
        'emit_scene({"type": "points", "points": points, "color": "#00d4ff"})\n'
    )


def _groq_post_process(raw: str) -> str | None:
    """
    Shared cleanup applied to any raw Groq response (streaming or non-streaming).
    Returns the cleaned script on success or None if the safety-net check fails.
    """
    # Strip markdown code fences if the model disobeys the system prompt.
    raw = re.sub(r"^```(?:python)?\s*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\n?```\s*$",            "", raw, flags=re.MULTILINE)
    raw = raw.strip()

    # Strip any stray import lines — the sandbox has math pre-imported and
    # numpy / other packages are not available; remove them defensively.
    raw = re.sub(r"^import\s+\S+.*$",       "", raw, flags=re.MULTILINE)
    raw = re.sub(r"^from\s+\S+\s+import.*$", "", raw, flags=re.MULTILINE)
    # Collapse runs of blank lines left by removed imports.
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()

    # Safety-net: generated code must contain both sentinel tokens.
    if "emit_scene" in raw and "points" in raw:
        return raw
    return None


def _stream_groq_to_queue(
    prompt: str,
    loop: asyncio.AbstractEventLoop,
    queue: "asyncio.Queue[tuple[str, bool] | None]",
) -> None:
    """
    Blocking SSE reader — runs inside a thread-pool executor.

    Reads server-sent events from the Groq streaming endpoint and deposits
    ``(delta_text, is_done)`` tuples into *queue* via ``run_coroutine_threadsafe``.
    Deposits ``None`` on any error so the consumer can bail out.
    """
    if not _GROQ_API_KEY:
        asyncio.run_coroutine_threadsafe(queue.put(None), loop)
        return

    payload = json.dumps({
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM_PROMPT},
            {"role": "user",   "content": f"Generate a 3D point cloud for: {prompt}"},
        ],
        "max_tokens": 1500,
        "temperature": 0.15,
        "stream": True,
    }).encode()

    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {_GROQ_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "groq-python/1.0",
        },
        method="POST",
    )

    def _put(item: "tuple[str, bool] | None") -> None:
        asyncio.run_coroutine_threadsafe(queue.put(item), loop)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").rstrip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    _put(("", True))
                    return
                try:
                    chunk = json.loads(data_str)
                    delta = chunk["choices"][0]["delta"].get("content", "")
                    if delta:
                        _put((delta, False))
                except Exception:
                    continue
        # Stream ended cleanly without an explicit [DONE] line.
        _put(("", True))
    except Exception as exc:
        print(f"[groq] stream thread failed: {exc}", flush=True)
        _put(None)


async def _stream_groq_script(prompt: str, room_id: str) -> str | None:
    """
    Drive Groq streaming for *prompt*, broadcasting incremental
    ``update_editor_text`` frames (``partial: true``) to *room_id* as text
    accumulates.

    Broadcasts a partial frame roughly every ``BROADCAST_EVERY`` characters so
    the editor shows a smooth typewriter effect without flooding the WebSocket.

    Returns the full cleaned script string on success, or ``None`` if the
    stream fails before finishing.
    """
    BROADCAST_EVERY = 60  # characters between partial broadcasts

    loop = asyncio.get_event_loop()
    queue: "asyncio.Queue[tuple[str, bool] | None]" = asyncio.Queue()

    # Launch the blocking SSE reader in a thread so the event loop stays free.
    reader_future = loop.run_in_executor(
        None, _stream_groq_to_queue, prompt, loop, queue
    )

    accumulated = ""
    last_broadcast_len = 0

    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                print("[groq] stream queue timeout", flush=True)
                return None

            if item is None:
                # Thread reported an error.
                return None

            delta, is_done = item
            accumulated += delta

            if is_done:
                break

            # Broadcast a partial frame when enough new text has accumulated.
            if len(accumulated) - last_broadcast_len >= BROADCAST_EVERY:
                await manager.broadcast_to_room(room_id, {
                    "type":    "update_editor_text",
                    "code":    accumulated,
                    "partial": True,
                })
                last_broadcast_len = len(accumulated)

    finally:
        # Always wait for the reader thread to finish so we don't leak executors.
        try:
            await asyncio.wait_for(reader_future, timeout=5.0)
        except Exception:
            pass

    return _groq_post_process(accumulated)


def _call_groq_sync(prompt: str) -> str | None:
    """
    Non-streaming Groq REST call — kept as a reference/fallback path.

    Intended to run inside ``asyncio.to_thread()`` so it never blocks the
    event loop.  In normal operation the streaming path is preferred; this is
    only used if the streaming path is explicitly bypassed.
    """
    if not _GROQ_API_KEY:
        return None

    payload = json.dumps({
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM_PROMPT},
            {"role": "user",   "content": f"Generate a 3D point cloud for: {prompt}"},
        ],
        "max_tokens": 1500,
        "temperature": 0.15,
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {_GROQ_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "groq-python/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        raw: str = data["choices"][0]["message"]["content"].strip()
        return _groq_post_process(raw)
    except Exception as exc:
        print(f"[groq] call failed: {exc}", flush=True)
        return None


async def _generate_editor_script(prompt: str, room_id: str) -> None:
    """
    Pre-Fill hook — called concurrently after the image_scene broadcast.

    Pipeline:
      1. Stream Groq LLM tokens to the room, broadcasting incremental
         ``update_editor_text`` frames with ``partial: true`` so users see
         the script being written in real time.
      2. After the stream completes, validate the full text with a sandbox
         dry-run.  If it passes, broadcast the final frame (``partial: false``).
         If validation fails, fall back to the keyword dictionary and broadcast
         the fallback as the final frame (overwriting the partial stream).
      3. If GROQ_API_KEY is absent or the stream errors before finishing,
         fall straight to the keyword-dictionary fallback.

    Non-fatal — exceptions are swallowed so this never disrupts the event loop.
    """
    try:
        script: str | None = None

        # ── 1. Streaming LLM path ───────────────────────────────────────────
        if _GROQ_API_KEY:
            candidate = await _stream_groq_script(prompt, room_id)
            if candidate:
                print(
                    f"[groq] stream complete ({len(candidate)} chars) for: {prompt[:60]!r}",
                    flush=True,
                )

                # ── 1a. Sandbox dry-run on the complete text ───────────────
                # Prepend a no-op emit_scene so the sandbox can execute the
                # script without access to the real WebSocket emitter.
                _DRY_RUN_HEADER = "def emit_scene(data): pass\n"
                dry_run_result: dict = await asyncio.get_event_loop().run_in_executor(
                    None,
                    execute_user_spatial_math,
                    _DRY_RUN_HEADER + candidate,
                    2.0,   # 2-second hard timeout
                )
                if dry_run_result["success"]:
                    script = candidate
                    print(f"[groq] dry-run passed for: {prompt[:60]!r}", flush=True)
                else:
                    print(
                        f"[groq] dry-run FAILED for {prompt[:60]!r} — falling back. "
                        f"Error: {dry_run_result.get('error', '')!r}",
                        flush=True,
                    )

        # ── 2. Fallback: instant local keyword dictionary ───────────────────
        if not script:
            script = _build_editor_script(prompt)
            print(f"[groq] using keyword fallback for: {prompt[:60]!r}", flush=True)

        # ── 3. Final frame signals completion to the frontend ───────────────
        # ``partial: false`` tells the client that generation is done.  The
        # editor should settle on this exact text regardless of any
        # intermediate partial frames that may have arrived.
        if script:
            await manager.broadcast_to_room(room_id, {
                "type":    "update_editor_text",
                "code":    script,
                "partial": False,
            })

    except Exception as exc:
        print(f"[groq] _generate_editor_script error: {exc}", flush=True)
        # best-effort — never crash the event loop


async def _process_image_to_scene(image_url: str, prompt: str = "", room_id: str = "global") -> None:
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

        # Fire the editor pre-fill concurrently — does not block the image pipeline.
        if prompt:
            asyncio.create_task(_generate_editor_script(prompt, room_id))

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
    asyncio.create_task(_process_image_to_scene(image_url, raw_prompt, room_id))

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
    fg = _flood_fill_fg_mask(px, GRID_W, GRID_H, threshold=40)

    # Fallback: if fewer than 10 % of pixels are foreground, the corners
    # probably landed on the subject — disable masking for this image.
    fg_count = sum(fg[r][c] for r in range(GRID_H) for c in range(GRID_W))
    if fg_count < GRID_W * GRID_H * 0.10:
        fg = [[True] * GRID_W for _ in range(GRID_H)]
    else:
        # Morphological opening (erode → dilate) removes single-pixel noise
        # and ragged protrusions at the silhouette boundary without shrinking
        # the core subject shape.
        fg = _open_mask(fg, GRID_W, GRID_H)

        # Re-check after opening — opening can only reduce fg_count.
        fg_count = sum(fg[r][c] for r in range(GRID_H) for c in range(GRID_W))
        if fg_count < GRID_W * GRID_H * 0.10:
            fg = [[True] * GRID_W for _ in range(GRID_H)]

    # Per-pixel feather weights: depth is graded toward 0 within 3 cells of
    # the boundary so silhouette edges blend smoothly instead of hard-cutting.
    feather = _edge_feather_weights(fg, GRID_W, GRID_H, feather_cells=3)

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
            # Feather: grade depth toward zero near the silhouette edge so
            # the boundary blends smoothly instead of stepping hard to bg.
            depth *= feather[row][col]
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
    #   • X and Y axes scaled INDEPENDENTLY so each fills [-2.0, +2.0].
    #     This compensates for the landscape grid (64×40) distorting portrait
    #     images: a tall subject's foreground spans more rows than columns in
    #     pixel space, but the raw x coords span 4.8 units vs 3.0 for y —
    #     uniform "widest axis" scaling would still let x dominate for many
    #     portrait subjects.  Per-axis scaling ensures portrait subjects fill
    #     the vertical viewport extent and landscape subjects fill horizontal.
    #   • Z axis scaled independently so depth ≤ 12 % of the smaller XY
    #     half-extent, preventing the depth-displacement from over-stretching.
    # XY re-centred using the foreground-vertex centroid (mean x/y) so the
    # subject always lands at the world origin even when the image composition
    # is off-centre (e.g. subject occupies only the left third of the frame).
    # Z is still re-centred on the bounding-box midpoint (unchanged behaviour).
    TARGET   = 2.0          # half-extent target on each axis
    Z_PCT    = 0.12         # max Z depth as fraction of normalised XY span

    xs = verts[0::3]
    ys = verts[1::3]
    zs = verts[2::3]

    n_verts  = len(xs)
    x_ctr    = sum(xs) / n_verts          # foreground centroid, not bbox midpoint
    y_ctr    = sum(ys) / n_verts          # foreground centroid, not bbox midpoint
    z_ctr    = (max(zs) + min(zs)) / 2.0  # bbox midpoint (depth range is symmetric)

    x_half    = max((max(xs) - min(xs)) / 2.0, 1e-6)
    y_half    = max((max(ys) - min(ys)) / 2.0, 1e-6)
    x_scale   = TARGET / x_half               # maps x foreground extent → ±2.0
    y_scale   = TARGET / y_half               # maps y foreground extent → ±2.0

    z_raw_half   = max((max(zs) - min(zs)) / 2.0, 1e-6)
    z_max_half   = TARGET * 2.0 * Z_PCT        # 12 % of 4.0 = 0.48 → ±0.24
    # Cap z by the smaller of the two XY scales so depth stays proportional
    # to whichever axis is more constrained.
    z_scale      = min(z_max_half / z_raw_half, min(x_scale, y_scale))

    for i in range(len(verts) // 3):
        verts[i * 3]     = round((verts[i * 3]     - x_ctr) * x_scale, 3)
        verts[i * 3 + 1] = round((verts[i * 3 + 1] - y_ctr) * y_scale, 3)
        verts[i * 3 + 2] = round((verts[i * 3 + 2] - z_ctr) * z_scale,  3)

    return {"type": "mesh", "vertices": verts, "faces": faces, "colors": colors}
