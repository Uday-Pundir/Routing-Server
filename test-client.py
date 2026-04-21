#!/usr/bin/env python3
"""
Comprehensive test client for the routing server.

Usage:
    python test-client.py [server-ws-url]

Examples:
    python test-client.py                              # defaults to ws://localhost:3000
    python test-client.py wss://your-app.onrender.com

This script spawns TWO peers (A and B) and runs:
    1. Registration
    2. Heartbeat ping/pong
    3. Broadcast relay
    4. Targeted relay (A → B)
    5. Signal exchange (simulated WebRTC offer/answer)
    6. Peer list
    7. Disconnect notification
"""

import asyncio
import json
import sys
import os

# Force UTF-8 output on Windows consoles (emoji support)
if sys.platform == "win32":
    os.system("")  # Enable ANSI/VT100 escape sequences
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import websockets
except ImportError:
    print("Missing dependency: websockets")
    print("Install it with:  pip install websockets")
    sys.exit(1)

SERVER = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:3000"
results: list[dict] = []


def log(tag: str, *args):
    print(f"[{tag}]", *args)


def pass_test(name: str):
    results.append({"name": name, "ok": True})
    print(f"  ✅ PASS: {name}")


def fail_test(name: str, reason: str):
    results.append({"name": name, "ok": False, "reason": reason})
    print(f"  ❌ FAIL: {name} — {reason}")


class Peer:
    """Wraps a WebSocket connection and provides message helpers."""

    def __init__(self, peer_id: str):
        self.id = peer_id
        self.ws = None
        self.inbox: list[dict] = []
        self.registered = False
        self.peer_id: str | None = None
        self._listeners: list[asyncio.Future] = []
        self._bg_task: asyncio.Task | None = None

    async def connect(self, timeout: float = 10.0):
        """Connect, register, and wait for the 'registered' response."""
        self.ws = await websockets.connect(SERVER)
        # Start background reader
        self._bg_task = asyncio.create_task(self._reader())
        # Send registration
        await self.ws.send(json.dumps({
            "type": "register",
            "peerId": self.id,
            "meta": {"label": self.id},
        }))
        # Wait for 'registered' message
        msg = await self.wait_for("registered", timeout=timeout)
        self.registered = True
        self.peer_id = msg["peerId"]
        return self

    async def _reader(self):
        """Background task that reads all incoming messages."""
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                self.inbox.append(msg)
                # Notify any waiters
                for fut in list(self._listeners):
                    if not fut.done():
                        fut.set_result(msg)
        except websockets.exceptions.ConnectionClosed:
            pass

    async def send(self, msg: dict):
        await self.ws.send(json.dumps(msg))

    async def send_and_wait(self, msg: dict, filter_type: str, timeout: float = 5.0) -> dict:
        """Send a message and wait for a specific response type."""
        wait_task = asyncio.ensure_future(self.wait_for(filter_type, timeout))
        await self.send(msg)
        return await wait_task

    async def wait_for(self, filter_type: str, timeout: float = 5.0) -> dict:
        """Wait for a message of a given type."""
        # Check inbox first (messages that already arrived)
        for msg in self.inbox:
            if msg["type"] == filter_type:
                self.inbox.remove(msg)
                return msg

        # Otherwise wait for future messages
        loop = asyncio.get_event_loop()
        while True:
            fut = loop.create_future()
            self._listeners.append(fut)
            try:
                msg = await asyncio.wait_for(fut, timeout=timeout)
                if msg["type"] == filter_type:
                    return msg
                # Not the type we want — keep waiting (reduce remaining timeout slightly)
            except asyncio.TimeoutError:
                raise TimeoutError(f'Timed out waiting for "{filter_type}"')
            finally:
                if fut in self._listeners:
                    self._listeners.remove(fut)

    async def close(self):
        if self._bg_task:
            self._bg_task.cancel()
        if self.ws:
            await self.ws.close()


async def run():
    print(f"\n🔌 Connecting to {SERVER}\n")

    # ── 1. Registration ─────────────────────────────────
    print("── TEST 1: Registration ──")
    peer_a = Peer("test-peer-A")
    peer_b = Peer("test-peer-B")

    try:
        await peer_a.connect()
        pass_test("Peer A registered")
    except Exception as e:
        fail_test("Peer A registration", str(e))
        sys.exit(1)

    try:
        await peer_b.connect()
        pass_test("Peer B registered")
    except Exception as e:
        fail_test("Peer B registration", str(e))
        await peer_a.close()
        sys.exit(1)

    # ── 2. Heartbeat ────────────────────────────────────
    print("\n── TEST 2: Heartbeat ping/pong ──")
    try:
        pong = await peer_a.send_and_wait({"type": "ping"}, "pong")
        if pong.get("ts") and isinstance(pong["ts"], (int, float)):
            pass_test("Heartbeat ping/pong")
        else:
            fail_test("Heartbeat ping/pong", "Missing or invalid ts in pong")
    except Exception as e:
        fail_test("Heartbeat ping/pong", str(e))

    # ── 3. Broadcast relay ──────────────────────────────
    print("\n── TEST 3: Broadcast relay ──")
    try:
        relay_task = asyncio.ensure_future(peer_b.wait_for("relay"))
        await peer_a.send({
            "type": "relay",
            "payload": {"text": "hello broadcast from A"},
        })
        relayed = await asyncio.wait_for(relay_task, timeout=5.0)
        if (relayed.get("from") == "test-peer-A"
                and relayed.get("payload", {}).get("text") == "hello broadcast from A"):
            pass_test("Broadcast relay A → B")
        else:
            fail_test("Broadcast relay A → B", "Unexpected relay content")
    except Exception as e:
        fail_test("Broadcast relay", str(e))

    # ── 4. Targeted relay ───────────────────────────────
    print("\n── TEST 4: Targeted relay (A → B) ──")
    try:
        # Drain any stale messages from previous tests
        await asyncio.sleep(0.5)
        peer_b.inbox = [m for m in peer_b.inbox if m["type"] != "relay"]
        relay_task = asyncio.ensure_future(peer_b.wait_for("relay"))
        await peer_a.send({
            "type": "relay",
            "to": "test-peer-B",
            "payload": {"secret": 42},
        })
        relayed = await asyncio.wait_for(relay_task, timeout=5.0)
        if (relayed.get("from") == "test-peer-A"
                and relayed.get("payload", {}).get("secret") == 42):
            pass_test("Targeted relay A → B")
        else:
            fail_test("Targeted relay A → B", "Unexpected payload")
    except Exception as e:
        fail_test("Targeted relay A → B", str(e))

    # ── 5. Signal exchange (UDP hole-punch simulation) ──
    print("\n── TEST 5: Signal exchange (hole-punch) ──")
    try:
        sig_task = asyncio.ensure_future(peer_b.wait_for("signal"))
        await peer_a.send({
            "type": "signal",
            "to": "test-peer-B",
            "signal": {"type": "offer", "sdp": "fake-sdp-offer"},
        })
        sig = await asyncio.wait_for(sig_task, timeout=5.0)
        if (sig.get("from") == "test-peer-A"
                and sig.get("signal", {}).get("type") == "offer"):
            pass_test("Signal offer A → B")
        else:
            fail_test("Signal offer A → B", "Unexpected signal content")

        # Answer back
        ans_task = asyncio.ensure_future(peer_a.wait_for("signal"))
        await peer_b.send({
            "type": "signal",
            "to": "test-peer-A",
            "signal": {"type": "answer", "sdp": "fake-sdp-answer"},
        })
        ans = await asyncio.wait_for(ans_task, timeout=5.0)
        if (ans.get("from") == "test-peer-B"
                and ans.get("signal", {}).get("type") == "answer"):
            pass_test("Signal answer B → A")
        else:
            fail_test("Signal answer B → A", "Unexpected signal content")
    except Exception as e:
        fail_test("Signal exchange", str(e))

    # ── 6. Peer list ────────────────────────────────────
    print("\n── TEST 6: Peer list ──")
    try:
        peer_list = await peer_a.send_and_wait({"type": "list"}, "peers")
        peers = peer_list.get("peers", [])
        if isinstance(peers, list) and len(peers) >= 2:
            pass_test(f"Peer list ({len(peers)} peers)")
        else:
            fail_test("Peer list", f"Expected >= 2 peers, got {len(peers)}")
    except Exception as e:
        fail_test("Peer list", str(e))

    # ── 7. Disconnect notification ──────────────────────
    print("\n── TEST 7: Disconnect notification ──")
    try:
        left_task = asyncio.ensure_future(peer_a.wait_for("peer_left"))
        await peer_b.close()
        left = await asyncio.wait_for(left_task, timeout=5.0)
        if left.get("peerId") == "test-peer-B":
            pass_test("Peer B disconnect notified to A")
        else:
            fail_test("Disconnect notification", "Wrong peerId in peer_left")
    except Exception as e:
        fail_test("Disconnect notification", str(e))

    # Wait 30 seconds before closing connections
    print("\n⏳ Waiting 30 seconds... check your stats page now!")
    await asyncio.sleep(30)

    # ── Summary ─────────────────────────────────────────
    await peer_a.close()
    print("\n════════════════════════════════════")
    print("         TEST SUMMARY")
    print("════════════════════════════════════")
    passed = sum(1 for r in results if r["ok"])
    failed = sum(1 for r in results if not r["ok"])
    for r in results:
        reason = f' ({r["reason"]})' if r.get("reason") else ""
        icon = "✅" if r["ok"] else "❌"
        print(f"  {icon} {r['name']}{reason}")
    print(f"\n  {passed} passed, {failed} failed\n")
    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
