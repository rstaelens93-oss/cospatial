"""
Collaborative AI Dashboard - Python FastAPI Backend
Handles WebSocket real-time chat and Python code execution
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="AI Dashboard Python Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────
# WebSocket Connection Manager
# ─────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict[str, Any]):
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)
        for conn in disconnected:
            self.disconnect(conn)

    async def send_personal_message(self, websocket: WebSocket, message: dict[str, Any]):
        await websocket.send_json(message)


manager = ConnectionManager()

# In-memory message store (for demo purposes)
chat_history: list[dict[str, Any]] = [
    {
        "id": 1,
        "content": "Welcome to the Collaborative AI Dashboard! Use the prompt panel to generate concepts.",
        "author": "System",
        "createdAt": "2026-07-25T00:00:00Z",
        "type": "system"
    }
]
next_message_id = 2


# ─────────────────────────────────────────────
# WebSocket endpoint
# ─────────────────────────────────────────────

@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await manager.connect(websocket)
    # Send history on connect
    await manager.send_personal_message(websocket, {
        "type": "history",
        "messages": chat_history
    })
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "message":
                global next_message_id
                message = {
                    "id": next_message_id,
                    "content": data.get("content", ""),
                    "author": data.get("author", "Anonymous"),
                    "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "type": "user"
                }
                next_message_id += 1
                chat_history.append(message)
                # Keep only last 100 messages
                if len(chat_history) > 100:
                    chat_history.pop(0)
                await manager.broadcast({"type": "message", "message": message})

            elif msg_type == "ping":
                await manager.send_personal_message(websocket, {"type": "pong"})

    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ─────────────────────────────────────────────
# Code Execution endpoint
# ─────────────────────────────────────────────

@app.post("/python-api/execute")
async def execute_code(body: dict[str, Any]):
    """Execute Python code in a subprocess sandbox and return stdout/stderr."""
    code = body.get("code", "")
    if not code.strip():
        return {"success": False, "stdout": "", "stderr": "No code provided", "executionTime": 0, "sceneData": None}

    # Broadcast execution start to all WebSocket clients
    await manager.broadcast({"type": "code_executing", "code": code})

    start = time.perf_counter()
    scene_data = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            # Inject helper so user code can emit 3D scene data
            header = """
import json as _json
import sys as _sys

def emit_scene(data):
    \"\"\"Emit JSON scene data for the 3D viewport.\"\"\"
    print(f"__SCENE_DATA__:{_json.dumps(data)}:__SCENE_DATA__", file=_sys.stderr)

"""
            f.write(header + code)
            tmp_path = f.name

        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=10
        )
        elapsed = (time.perf_counter() - start) * 1000

        stdout = result.stdout
        stderr_raw = result.stderr

        # Extract scene data if emitted
        scene_marker = "__SCENE_DATA__:"
        if scene_marker in stderr_raw:
            parts = stderr_raw.split(scene_marker)
            if len(parts) >= 3:
                try:
                    scene_data = parts[1]
                    stderr_raw = parts[0] + parts[2] if len(parts) > 2 else parts[0]
                except Exception:
                    scene_data = None

        success = result.returncode == 0
        payload = {
            "success": success,
            "stdout": stdout,
            "stderr": stderr_raw,
            "executionTime": round(elapsed, 2),
            "sceneData": scene_data
        }

        # Broadcast result to WebSocket clients
        await manager.broadcast({"type": "code_result", "result": payload})
        return payload

    except subprocess.TimeoutExpired:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "success": False,
            "stdout": "",
            "stderr": "Execution timed out after 10 seconds.",
            "executionTime": round(elapsed, 2),
            "sceneData": None
        }
    except Exception as e:
        return {
            "success": False,
            "stdout": "",
            "stderr": str(e),
            "executionTime": 0,
            "sceneData": None
        }
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


@app.get("/python-api/healthz")
async def healthz():
    return {"status": "ok", "service": "python-fastapi"}


@app.get("/python-api/ws-stats")
async def ws_stats():
    return {
        "connections": len(manager.active_connections),
        "messageCount": len(chat_history)
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PYTHON_PORT", "8001"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
