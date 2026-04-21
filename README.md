# Distributed Routing Server

A **stateless** P2P routing server with blind mailbox relay and UDP hole-punch signalling.
Built for the Cloud Computing Track challenge.

---

## 📡 Live Endpoint

> **Replace with your actual deployed URL after deployment:**

| Endpoint | URL |
|---|---|
| Health check | `https://routing-server-8yxk.onrender.com/healthz` |
| Ready check | `https://routing-server-8yxk.onrender.com/ready` |
| Stats | `https://routing-server-8yxk.onrender.com/stats` |
| WebSocket | `wss://routing-server-8yxk.onrender.com` |

---

## 🏛️ Architecture

```
 ┌──────────┐                              ┌──────────┐
 │ Client A │──WSS──┐              ┌──WSS──│ Client B │
 └──────────┘       │              │       └──────────┘
                    ▼              ▼
               ┌────────────────────────┐
               │    Routing Server      │
               │                        │
               │  ┌──────────────────┐  │
               │  │  In-Memory Map   │  │
               │  │  (peer registry) │  │
               │  └──────────────────┘  │
               │                        │
               │  • No database         │
               │  • No disk storage     │
               │  • No message log      │
               │  • Purely stateless    │
               └────────────────────────┘
                    │              │
                    ▼              ▼
              After WebRTC signalling:
              A ◄─── P2P direct ───► B
              (UDP hole-punched connection)
```

### Key Design Decisions

- **Single Node.js process** — Express for HTTP health endpoints + `ws` library for WebSocket on the same port
- **Zero persistence** — the only state is a `Map<peerId, {ws, lastSeen, meta}>` in memory; process restart = clean slate
- **CORS enabled** — browser-based clients can connect without proxy issues
- **Protocol-agnostic payloads** — the `relay` and `signal` message types carry opaque JSON payloads, so clients decide their own data format

---

## 🔀 How Routing Works

The server acts as a **message relay** and **signalling rendezvous** — it never interprets or stores message content.

### Message Types

| Type | Direction | Purpose |
|---|---|---|
| `register` | Client → Server | Announce presence; server assigns/confirms `peerId` and returns peer list |
| `ping` | Client → Server | Heartbeat; server updates `lastSeen` and replies `pong` |
| `relay` (with `to`) | Client → Server → Target | Forward payload to a specific peer. **Message is never stored** (blind mailbox). |
| `relay` (broadcast) | Client → Server → All | Forward payload to all other connected peers. Never stored. |
| `signal` | Client → Server → Target | WebRTC SDP/ICE candidate exchange for UDP hole punching. Relayed then immediately discarded. |
| `list` | Client → Server | Returns the live list of connected peers |

### Blind Mailbox Guarantee

The "blind mailbox" pattern means:
1. Server receives a relay/signal message
2. Server looks up the target peer's WebSocket
3. Server forwards the payload **immediately**
4. Server **discards** the message — no logging, no queue, no database write
5. If the target peer is offline, the sender gets an error — the message is **lost forever**

### UDP Hole Punching Flow

```
Peer A                    Server                   Peer B
  │                          │                        │
  ├──signal(offer)──────────►│                        │
  │                          ├──signal(offer)────────►│
  │                          │                        │
  │                          │◄──signal(answer)───────┤
  │◄──signal(answer)─────────┤                        │
  │                          │                        │
  ├──signal(ice-candidate)──►│                        │
  │                          ├──signal(ice-candidate)►│
  │                          │                        │
  │◄═══════ Direct P2P (UDP/DTLS) ═══════════════════►│
  │         (server no longer needed)                 │
```

The server's role is purely **signalling rendezvous**. After clients exchange SDP offers/answers and ICE candidates through the server, they establish a direct peer-to-peer connection (typically via WebRTC DataChannels over UDP). The server is no longer in the data path.

---

## 💓 How Heartbeat is Handled

Three independent layers ensure the server stays alive and peer state stays clean:

### Layer 1: Client-Side Ping (WebSocket)

```json
Client sends:  { "type": "ping" }
Server replies: { "type": "pong", "ts": 1713684000000 }
```

- Clients should send pings every **10–20 seconds**
- Server updates the peer's `lastSeen` timestamp on each ping
- This keeps the WebSocket connection alive and tells the server the peer is still active

### Layer 2: Stale-Peer Reaper (Server-Side)

- Runs every **15 seconds** via `setInterval`
- Scans all registered peers for `lastSeen` older than **30 seconds**
- Removes stale peers from the registry (crashed clients, network drops)
- Broadcasts `peer_left` to remaining peers
- Prevents ghost entries from accumulating

### Layer 3: Self-Ping HTTP (Keep-Alive for Free-Tier Hosting)

```
Server → GET /ping → Server  (every 5 minutes)
```

- Render and Railway free tiers spin down idle instances after ~15 minutes
- The server pings its own `/ping` endpoint every **5 minutes** using `fetch()`
- Configured via the `SELF_URL` environment variable
- First self-ping fires 10 seconds after startup
- This keeps the dyno/container warm and prevents cold starts

---

## 🔧 API Reference

### HTTP Endpoints

| Method | Path | Response | Purpose |
|---|---|---|---|
| GET | `/healthz` | `{ status, ts, peers, uptime }` | Health check (UptimeRobot, Render health) |
| GET | `/ready` | `{ ready, ts, peers }` | Readiness probe (confirms WSS is bound) |
| GET | `/stats` | `{ peers, peerList, uptime, startTime, memory }` | Detailed server stats |
| GET | `/ping` | `"pong"` | Keep-alive probe |

### WebSocket Protocol

Connect to `wss://routing-server-8yxk.onrender.com` (or `ws://localhost:3000` locally).

#### Client → Server Messages

```jsonc
// Register (first message after connecting)
{ "type": "register", "peerId": "optional-custom-id", "meta": { "label": "my-device" } }

// Heartbeat ping
{ "type": "ping" }

// Relay to specific peer (blind mailbox — never stored)
{ "type": "relay", "to": "<peerId>", "payload": { /* anything */ } }

// Broadcast relay to all peers
{ "type": "relay", "payload": { /* anything */ } }

// WebRTC signal (SDP / ICE for hole-punching)
{ "type": "signal", "to": "<peerId>", "signal": { "type": "offer", "sdp": "..." } }

// Request peer list
{ "type": "list" }
```

#### Server → Client Messages

```jsonc
// Registration confirmed
{ "type": "registered", "peerId": "...", "ip": "...", "peers": [...] }

// Heartbeat reply
{ "type": "pong", "ts": 1713684000000 }

// Incoming relayed message
{ "type": "relay", "from": "<senderId>", "payload": { ... }, "ts": ... }

// Relay sent confirmation
{ "type": "relay_ok", "to": "<peerId>" }

// Incoming signal
{ "type": "signal", "from": "<senderId>", "signal": { ... } }

// Peer list
{ "type": "peers", "peers": [{ "peerId": "...", "meta": {...}, "age": 1234 }] }

// Peer joined / left
{ "type": "peer_joined", "peerId": "...", "meta": {...} }
{ "type": "peer_left", "peerId": "..." }

// Error
{ "type": "error", "error": "description" }
```

---

## 🚀 Deploy

### Render (Recommended)

1. Push this repo to GitHub
2. Go to [Render Dashboard](https://dashboard.render.com) → **New Web Service**
3. Connect your GitHub repo
4. Settings:
   - **Build Command:** `npm install`
   - **Start Command:** `npm start`
5. Add environment variable:
   - `SELF_URL` = `https://routing-server-8yxk.onrender.com`
6. Deploy — the `render.yaml` in this repo automates most of this

### Railway

1. Push to GitHub → connect at [railway.app](https://railway.app)
2. Or CLI: `railway login && railway init && railway up`
3. Add environment variable:
   - `SELF_URL` = `https://routing-server-8yxk.onrender.com`
4. The `railway.toml` handles build and health check config

---

## 🧪 Test

### Prerequisites

```bash
npm install
```

### Run Locally

```bash
# Terminal 1: Start the server
npm start

# Terminal 2: Run comprehensive tests
node test-client.js ws://localhost:3000
```

### Test Against Live Deployment

```bash
node test-client.js wss://routing-server-8yxk.onrender.com
```

### What the Test Client Validates

| # | Test | What it proves |
|---|---|---|
| 1 | Registration (2 peers) | Peer registry works, IDs assigned |
| 2 | Heartbeat ping/pong | Keep-alive mechanism works |
| 3 | Broadcast relay | Multi-peer message fan-out, blind mailbox |
| 4 | Targeted relay (A → B) | Point-to-point routing, no storage |
| 5 | Signal exchange | WebRTC/hole-punch signalling path works |
| 6 | Peer list | Discovery mechanism works |
| 7 | Disconnect notification | Cleanup + event broadcast on peer leave |

### Expected Output

```
🔌 Connecting to ws://localhost:3000

── TEST 1: Registration ──
  ✅ PASS: Peer A registered
  ✅ PASS: Peer B registered

── TEST 2: Heartbeat ping/pong ──
  ✅ PASS: Heartbeat ping/pong

── TEST 3: Broadcast relay ──
  ✅ PASS: Broadcast relay A → B

── TEST 4: Targeted relay (A → B) ──
  ✅ PASS: Targeted relay A → B

── TEST 5: Signal exchange (hole-punch) ──
  ✅ PASS: Signal offer A → B
  ✅ PASS: Signal answer B → A

── TEST 6: Peer list ──
  ✅ PASS: Peer list (2 peers)

── TEST 7: Disconnect notification ──
  ✅ PASS: Peer B disconnect notified to A

════════════════════════════════════
         TEST SUMMARY
════════════════════════════════════
  ✅ Peer A registered
  ✅ Peer B registered
  ✅ Heartbeat ping/pong
  ✅ Broadcast relay A → B
  ✅ Targeted relay A → B
  ✅ Signal offer A → B
  ✅ Signal answer B → A
  ✅ Peer list (2 peers)
  ✅ Peer B disconnect notified to A

  9 passed, 0 failed
```

---

## 📁 Project Structure

```
routing-server/
├── server.js          # Main server — Express + WebSocket
├── test-client.js     # Comprehensive test suite (9 tests)
├── package.json       # Dependencies: express, ws, uuid
├── render.yaml        # Render deployment blueprint
├── railway.toml       # Railway deployment config
├── .gitignore         # node_modules, .env, logs
└── README.md          # This file
```

---

## License

MIT
