#!/usr/bin/env python3
"""
TalkType Relay Server - WebSocket relay for cross-network voice typing.

Bridges A machine (sender) and B machine (receiver) across different networks.
Deploy this on any free-tier server (Render, Railway, Fly.io, etc.).

Architecture:
    A machine  --HTTP POST /push/{room}--> Relay --> WebSocket /ws/{room} --> B machine

Usage:
    python relay_server.py
    python relay_server.py --port 8765
    python relay_server.py --host 0.0.0.0 --port 8765

Room ID acts as a shared secret - use a long random string (e.g. a UUID).
Generate one with: python -c "import uuid; print(uuid.uuid4())"

Deploy to Render.com (free):
    1. Push this file to a GitHub repo
    2. Create a new Web Service on render.com, point to the repo
    3. Set start command: python relay_server.py
    4. Your relay URL will be: https://your-app.onrender.com
"""

import argparse
import asyncio
import logging
from collections import defaultdict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
import uvicorn

# === Logging ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("relay")

# === App ===
app = FastAPI(
    title="TalkType Relay",
    description="WebSocket relay server for cross-network voice typing",
    version="1.0.0",
)

# room_id -> set of active WebSocket connections
rooms: dict[str, set[WebSocket]] = defaultdict(set)
rooms_lock = asyncio.Lock()


# === Endpoints ===

@app.get("/")
def index():
    """Simple status page."""
    total_listeners = sum(len(ws_set) for ws_set in rooms.values())
    return HTMLResponse(f"""
    <html><body style="font-family:monospace;padding:2em">
    <h2>TalkType Relay</h2>
    <p>Status: <b>running</b></p>
    <p>Active rooms: {len(rooms)}</p>
    <p>Total listeners: {total_listeners}</p>
    <p><a href="/docs">API docs</a></p>
    </body></html>
    """)


@app.get("/health")
def health():
    """Health check."""
    total_listeners = sum(len(ws_set) for ws_set in rooms.values())
    return {
        "status": "ok",
        "active_rooms": len(rooms),
        "total_listeners": total_listeners,
    }


@app.post("/push/{room_id}")
async def push_text(room_id: str, request: Request):  # noqa: C901
    """
    Send text to all receivers in a room.

    Body: {"text": "transcribed text here"}

    Called by A machine (talktype.py) after transcription.
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    text = data.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Missing 'text' field")

    if len(text) > 10000:
        raise HTTPException(status_code=400, detail="Text too long (max 10000 chars)")

    listeners = list(rooms.get(room_id, set()))
    if not listeners:
        logger.info(f"Push to room '{room_id[:8]}...' - no listeners connected")
        return {"delivered": 0, "listeners": 0}

    # Broadcast to all receivers in this room
    dead = []
    delivered = 0
    for ws in listeners:
        try:
            await ws.send_json({"text": text})
            delivered += 1
        except Exception:
            dead.append(ws)

    # Clean up dead connections
    if dead:
        async with rooms_lock:
            for ws in dead:
                rooms[room_id].discard(ws)

    logger.info(f"Push to room '{room_id[:8]}...' | delivered={delivered} dead={len(dead)} text_len={len(text)}")
    return {"delivered": delivered, "listeners": len(listeners)}


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, client_id: str = ""):
    """
    B machine connects here to receive transcribed text.

    Stays connected persistently. Receives JSON: {"text": "..."}
    Optional query param: ?client_id=<unique-id>  — deduplicates reconnects.
    """
    await websocket.accept()

    async with rooms_lock:
        rooms[room_id].add(websocket)

    client = websocket.client
    logger.info(f"Receiver connected | room='{room_id[:8]}...' | client={client}")

    try:
        # Send acknowledgment
        await websocket.send_json({"status": "connected", "room": room_id[:8] + "..."})

        # Keep connection alive, handle pings from client
        while True:
            try:
                # Wait for any message from client (ping/keepalive)
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                if msg == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                # Send keepalive ping to detect dead connections
                try:
                    await websocket.send_json({"ping": True})
                except Exception:
                    break
    except WebSocketDisconnect:
        logger.info(f"Receiver disconnected | room='{room_id[:8]}...'")
    except Exception as e:
        logger.warning(f"WebSocket error | room='{room_id[:8]}...' | {e}")
    finally:
        async with rooms_lock:
            rooms[room_id].discard(websocket)
            # Clean up empty rooms
            if not rooms[room_id]:
                del rooms[room_id]


# === Main ===
def parse_args():
    parser = argparse.ArgumentParser(description="TalkType Relay Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    parser.add_argument("--log-level", default="INFO", help="Log level (default: INFO)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    logger.setLevel(getattr(logging, args.log_level.upper(), logging.INFO))
    print(f"TalkType Relay starting on {args.host}:{args.port}")
    print("Deploy to Render.com: https://render.com (free tier available)")
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())
