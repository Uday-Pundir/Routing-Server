const express = require('express');
const http = require('http');
const { WebSocketServer, WebSocket } = require('ws');
const { v4: uuidv4 } = require('uuid');

const app = express();

// CORS — allow browser clients from any origin
app.use((_req, res, next) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  next();
});
app.use(express.json());

const server = http.createServer(app);
const wss = new WebSocketServer({ server });

const startTime = Date.now();

// In-memory peer registry (stateless — no persistence)
// peerId -> { ws, lastSeen, meta }
const peers = new Map();

// ─────────────────────────────────────────────
// Utility helpers
// ─────────────────────────────────────────────
function send(ws, obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
  }
}

function broadcastExcept(senderPeerId, obj) {
  peers.forEach(({ ws }, id) => {
    if (id !== senderPeerId) send(ws, obj);
  });
}

function peerList() {
  const now = Date.now();
  return [...peers.entries()].map(([id, p]) => ({
    peerId: id,
    meta: p.meta,
    age: now - p.lastSeen,
  }));
}

function stalePeers(maxAgeMs = 30_000) {
  const now = Date.now();
  const stale = [];
  peers.forEach((p, id) => {
    if (now - p.lastSeen > maxAgeMs) stale.push(id);
  });
  return stale;
}

// ─────────────────────────────────────────────
// Stale-peer reaper (runs every 15 s)
// ─────────────────────────────────────────────
setInterval(() => {
  stalePeers(30_000).forEach((id) => {
    console.log(`[reaper] removing stale peer ${id}`);
    peers.delete(id);
  });
}, 15_000);

// ─────────────────────────────────────────────
// WebSocket — core signalling / relay
// ─────────────────────────────────────────────
wss.on('connection', (ws, req) => {
  const remoteIp =
    req.headers['x-forwarded-for']?.split(',')[0].trim() ||
    req.socket.remoteAddress;

  let myPeerId = null;

  ws.on('message', (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw);
    } catch {
      return send(ws, { type: 'error', error: 'invalid JSON' });
    }

    const { type } = msg;

    // ── REGISTER ──────────────────────────────
    if (type === 'register') {
      myPeerId = msg.peerId || uuidv4();
      peers.set(myPeerId, { ws, lastSeen: Date.now(), meta: msg.meta || {} });

      send(ws, {
        type: 'registered',
        peerId: myPeerId,
        ip: remoteIp,
        peers: peerList().filter((p) => p.peerId !== myPeerId),
      });

      // Notify others of the new peer
      broadcastExcept(myPeerId, {
        type: 'peer_joined',
        peerId: myPeerId,
        meta: msg.meta || {},
      });

      console.log(`[+] peer registered: ${myPeerId} (${remoteIp})`);
      return;
    }

    // All subsequent messages require a registered peer
    if (!myPeerId) {
      return send(ws, { type: 'error', error: 'not registered' });
    }

    // ── HEARTBEAT / PING ──────────────────────
    if (type === 'ping') {
      const p = peers.get(myPeerId);
      if (p) p.lastSeen = Date.now();
      return send(ws, { type: 'pong', ts: Date.now() });
    }

    // ── BLIND RELAY (no storage) ──────────────
    if (type === 'relay') {
      const { to, payload } = msg;

      // Relay to a specific peer
      if (to) {
        const target = peers.get(to);
        if (!target) {
          return send(ws, { type: 'error', error: `peer ${to} not found` });
        }
        send(target.ws, {
          type: 'relay',
          from: myPeerId,
          payload,
          ts: Date.now(),
        });
        // Message is NOT stored anywhere — blind mailbox
        return send(ws, { type: 'relay_ok', to });
      }

      // Broadcast relay to all other peers
      broadcastExcept(myPeerId, {
        type: 'relay',
        from: myPeerId,
        payload,
        ts: Date.now(),
      });
      return send(ws, { type: 'relay_ok', to: 'broadcast' });
    }

    // ── UDP HOLE-PUNCH SIGNALLING ─────────────
    // Clients exchange SDP / ICE candidates through this server
    if (type === 'signal') {
      const { to, signal } = msg;
      const target = peers.get(to);
      if (!target) {
        return send(ws, { type: 'error', error: `peer ${to} not found` });
      }
      send(target.ws, {
        type: 'signal',
        from: myPeerId,
        signal,
      });
      return; // Signal is relayed and immediately discarded
    }

    // ── PEER LIST ─────────────────────────────
    if (type === 'list') {
      return send(ws, { type: 'peers', peers: peerList() });
    }

    send(ws, { type: 'error', error: `unknown message type: ${type}` });
  });

  ws.on('close', () => {
    if (myPeerId) {
      peers.delete(myPeerId);
      broadcastExcept(myPeerId, { type: 'peer_left', peerId: myPeerId });
      console.log(`[-] peer disconnected: ${myPeerId}`);
    }
  });

  ws.on('error', (err) => {
    console.error(`[ws error] ${myPeerId}: ${err.message}`);
  });
});

// ─────────────────────────────────────────────
// HTTP endpoints
// ─────────────────────────────────────────────

// Health check (matches the reference implementation pattern)
app.get('/healthz', (_req, res) => {
  res.json({ status: 'ok', ts: Date.now(), peers: peers.size, uptime: process.uptime() });
});

// Ready check — stricter than healthz, confirms WebSocket server is bound
app.get('/ready', (_req, res) => {
  if (wss.clients !== undefined) {
    res.json({ ready: true, ts: Date.now(), peers: peers.size });
  } else {
    res.status(503).json({ ready: false });
  }
});

// Stats
app.get('/stats', (_req, res) => {
  res.json({
    peers: peers.size,
    peerList: peerList(),
    uptime: process.uptime(),
    startTime,
    memory: process.memoryUsage(),
  });
});

// Self-ping so Render/Railway free-tier doesn't spin down
app.get('/ping', (_req, res) => res.send('pong'));

// ─────────────────────────────────────────────
// Self-heartbeat (keep-alive for free-tier hosts)
// ─────────────────────────────────────────────
const SELF_URL = process.env.SELF_URL; // set this in Render/Railway env vars
if (SELF_URL) {
  const selfPing = async () => {
    try {
      const res = await fetch(`${SELF_URL}/ping`);
      console.log(`[heartbeat] self-ping → ${res.status}`);
    } catch (e) {
      console.error('[heartbeat] error:', e.message);
    }
  };
  // Ping every 5 minutes to prevent free-tier spin-down
  setInterval(selfPing, 5 * 60 * 1000);
  // Also ping immediately on start-up after a short delay
  setTimeout(selfPing, 10_000);
  console.log(`[heartbeat] self-ping enabled → ${SELF_URL}`);
}

// ─────────────────────────────────────────────
// Start
// ─────────────────────────────────────────────
const PORT = process.env.PORT || 3000;
server.listen(PORT, () => {
  console.log(`🚀  Routing server listening on port ${PORT}`);
});
