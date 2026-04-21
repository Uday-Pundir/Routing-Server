"""
Distributed Routing Server (Python)

A stateless P2P routing server with blind mailbox relay and UDP hole-punch
signalling. Drop-in replacement for the Node.js server.js.

Usage:
    python server.py
    # or with custom port:
    PORT=8080 python server.py
"""

import asyncio
import json
import os
import time
import uuid
import traceback
import sys

import aiohttp
from aiohttp import web

# Force UTF-8 output on Windows consoles (emoji support)
if sys.platform == "win32":
    os.system("")  # Enable ANSI/VT100 escape sequences
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ─────────────────────────────────────────────
# Globals
# ─────────────────────────────────────────────
start_time = time.time()

# In-memory peer registry (stateless — no persistence)
# peer_id -> { "ws": WebSocketResponse, "last_seen": float, "meta": dict }
peers: dict[str, dict] = {}


# ─────────────────────────────────────────────
# Utility helpers
# ─────────────────────────────────────────────
async def ws_send(ws: web.WebSocketResponse, obj: dict):
    """Send a JSON message to a websocket if it's still open."""
    if not ws.closed:
        try:
            await ws.send_json(obj)
        except ConnectionResetError:
            pass


async def broadcast_except(sender_peer_id: str, obj: dict):
    """Send a message to all peers except the sender."""
    tasks = []
    for pid, p in peers.items():
        if pid != sender_peer_id:
            tasks.append(ws_send(p["ws"], obj))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def peer_list() -> list[dict]:
    """Return a list of all connected peers with metadata."""
    now = time.time() * 1000  # ms
    return [
        {
            "peerId": pid,
            "meta": p["meta"],
            "age": int(now - p["last_seen"]),
        }
        for pid, p in peers.items()
    ]


def stale_peers(max_age_ms: int = 30_000) -> list[str]:
    """Return peer IDs that haven't been seen within max_age_ms."""
    now = time.time() * 1000
    return [pid for pid, p in peers.items() if now - p["last_seen"] > max_age_ms]


# ─────────────────────────────────────────────
# Stale-peer reaper (runs every 15 s)
# ─────────────────────────────────────────────
async def reaper_loop():
    """Periodically remove stale peers."""
    while True:
        await asyncio.sleep(15)
        for pid in stale_peers(30_000):
            print(f"[reaper] removing stale peer {pid}")
            peers.pop(pid, None)


# ─────────────────────────────────────────────
# Self-heartbeat (keep-alive for free-tier hosts)
# ─────────────────────────────────────────────
async def self_ping_loop(self_url: str):
    """Ping our own /ping endpoint every 5 minutes to prevent spin-down."""
    # Initial delay
    await asyncio.sleep(10)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(f"{self_url}/ping") as resp:
                    print(f"[heartbeat] self-ping → {resp.status}")
            except Exception as e:
                print(f"[heartbeat] error: {e}")
            await asyncio.sleep(5 * 60)


# ─────────────────────────────────────────────
# CORS middleware
# ─────────────────────────────────────────────
@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ─────────────────────────────────────────────
# HTTP endpoints
# ─────────────────────────────────────────────
async def handle_root(request: web.Request):
    # If this is a WebSocket upgrade request, handle it as WS
    if (request.headers.get("Upgrade", "").lower() == "websocket"):
        return await handle_ws(request)

    # Otherwise serve the JSON info page
    return web.json_response({
        "name": "Distributed Routing Server",
        "status": "ok",
        "version": "1.0.0",
        "endpoints": {
            "healthz": "/healthz",
            "ready": "/ready",
            "stats": "/stats",
            "ping": "/ping",
            "websocket": "wss://routing-server-8yxk.onrender.com",
        },
        "peers": len(peers),
        "uptime": time.time() - start_time,
        "ts": int(time.time() * 1000),
    })


async def handle_healthz(request: web.Request):
    return web.json_response({
        "status": "ok",
        "ts": int(time.time() * 1000),
        "peers": len(peers),
        "uptime": time.time() - start_time,
    })


async def handle_ready(request: web.Request):
    return web.json_response({
        "ready": True,
        "ts": int(time.time() * 1000),
        "peers": len(peers),
    })


async def handle_stats(request: web.Request):
    return web.json_response({
        "peers": len(peers),
        "peerList": peer_list(),
        "uptime": time.time() - start_time,
        "startTime": int(start_time * 1000),
    })


async def handle_ping(request: web.Request):
    return web.Response(text="pong")



# ─────────────────────────────────────────────
# WebSocket — core signalling / relay
# ─────────────────────────────────────────────
async def handle_ws(request: web.Request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Get remote IP (respect proxy headers)
    remote_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote
        or "unknown"
    )

    my_peer_id: str | None = None

    try:
        async for raw_msg in ws:
            if raw_msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    msg = json.loads(raw_msg.data)
                except json.JSONDecodeError:
                    await ws_send(ws, {"type": "error", "error": "invalid JSON"})
                    continue

                msg_type = msg.get("type")

                # ── REGISTER ──────────────────────────────
                if msg_type == "register":
                    my_peer_id = msg.get("peerId") or str(uuid.uuid4())
                    peers[my_peer_id] = {
                        "ws": ws,
                        "last_seen": time.time() * 1000,
                        "meta": msg.get("meta", {}),
                    }

                    await ws_send(ws, {
                        "type": "registered",
                        "peerId": my_peer_id,
                        "ip": remote_ip,
                        "peers": [p for p in peer_list() if p["peerId"] != my_peer_id],
                    })

                    # Notify others of the new peer
                    await broadcast_except(my_peer_id, {
                        "type": "peer_joined",
                        "peerId": my_peer_id,
                        "meta": msg.get("meta", {}),
                    })

                    print(f"[+] peer registered: {my_peer_id} ({remote_ip})")
                    continue

                # All subsequent messages require a registered peer
                if not my_peer_id:
                    await ws_send(ws, {"type": "error", "error": "not registered"})
                    continue

                # ── HEARTBEAT / PING ──────────────────────
                if msg_type == "ping":
                    p = peers.get(my_peer_id)
                    if p:
                        p["last_seen"] = time.time() * 1000
                    await ws_send(ws, {"type": "pong", "ts": int(time.time() * 1000)})
                    continue

                # ── BLIND RELAY (no storage) ──────────────
                if msg_type == "relay":
                    to = msg.get("to")
                    payload = msg.get("payload")

                    # Relay to a specific peer
                    if to:
                        target = peers.get(to)
                        if not target:
                            await ws_send(ws, {"type": "error", "error": f"peer {to} not found"})
                            continue
                        await ws_send(target["ws"], {
                            "type": "relay",
                            "from": my_peer_id,
                            "payload": payload,
                            "ts": int(time.time() * 1000),
                        })
                        # Message is NOT stored anywhere — blind mailbox
                        await ws_send(ws, {"type": "relay_ok", "to": to})
                        continue

                    # Broadcast relay to all other peers
                    await broadcast_except(my_peer_id, {
                        "type": "relay",
                        "from": my_peer_id,
                        "payload": payload,
                        "ts": int(time.time() * 1000),
                    })
                    await ws_send(ws, {"type": "relay_ok", "to": "broadcast"})
                    continue

                # ── UDP HOLE-PUNCH SIGNALLING ─────────────
                # Clients exchange SDP / ICE candidates through this server
                if msg_type == "signal":
                    to = msg.get("to")
                    signal = msg.get("signal")
                    target = peers.get(to)
                    if not target:
                        await ws_send(ws, {"type": "error", "error": f"peer {to} not found"})
                        continue
                    await ws_send(target["ws"], {
                        "type": "signal",
                        "from": my_peer_id,
                        "signal": signal,
                    })
                    continue  # Signal is relayed and immediately discarded

                # ── PEER LIST ─────────────────────────────
                if msg_type == "list":
                    await ws_send(ws, {"type": "peers", "peers": peer_list()})
                    continue

                await ws_send(ws, {"type": "error", "error": f"unknown message type: {msg_type}"})

            elif raw_msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

    except Exception as e:
        print(f"[ws error] {my_peer_id}: {e}")
    finally:
        if my_peer_id:
            peers.pop(my_peer_id, None)
            await broadcast_except(my_peer_id, {"type": "peer_left", "peerId": my_peer_id})
            print(f"[-] peer disconnected: {my_peer_id}")

    return ws


# ─────────────────────────────────────────────
# App factory & startup
# ─────────────────────────────────────────────
def create_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware])

    # HTTP routes
    app.router.add_get("/", handle_root)
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_get("/ready", handle_ready)
    app.router.add_get("/stats", handle_stats)
    app.router.add_get("/ping", handle_ping)


    # Background tasks
    async def start_background(app_: web.Application):
        app_["reaper"] = asyncio.create_task(reaper_loop())

        self_url = os.environ.get("SELF_URL")
        if self_url:
            app_["self_ping"] = asyncio.create_task(self_ping_loop(self_url))
            print(f"[heartbeat] self-ping enabled → {self_url}")

    async def stop_background(app_: web.Application):
        app_["reaper"].cancel()
        if "self_ping" in app_:
            app_["self_ping"].cancel()

    app.on_startup.append(start_background)
    app.on_cleanup.append(stop_background)

    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    print(f"🚀  Routing server listening on port {port}")
    web.run_app(create_app(), port=port, print=None)
