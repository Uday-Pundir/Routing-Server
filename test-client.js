#!/usr/bin/env node
/**
 * Comprehensive test client for the routing server.
 *
 * Usage:
 *   node test-client.js [server-ws-url]
 *
 * Examples:
 *   node test-client.js                          # defaults to ws://localhost:3000
 *   node test-client.js wss://your-app.onrender.com
 *
 * This script spawns TWO peers (A and B) and runs:
 *   1. Registration
 *   2. Heartbeat ping/pong
 *   3. Broadcast relay
 *   4. Targeted relay (A → B)
 *   5. Signal exchange (simulated WebRTC offer/answer)
 *   6. Peer list
 *   7. Disconnect notification
 */

const WebSocket = require('ws');

const SERVER = process.argv[2] || 'ws://localhost:3000';
const results = [];

function log(tag, ...args) {
  console.log(`[${tag}]`, ...args);
}

function pass(name) {
  results.push({ name, ok: true });
  console.log(`  ✅ PASS: ${name}`);
}

function fail(name, reason) {
  results.push({ name, ok: false, reason });
  console.error(`  ❌ FAIL: ${name} — ${reason}`);
}

function createPeer(id) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(SERVER);
    const peer = { id, ws, inbox: [], registered: false, peerId: null };

    ws.on('open', () => {
      ws.send(JSON.stringify({ type: 'register', peerId: id, meta: { label: id } }));
    });

    ws.on('message', (raw) => {
      const msg = JSON.parse(raw);
      peer.inbox.push(msg);
      if (msg.type === 'registered') {
        peer.registered = true;
        peer.peerId = msg.peerId;
        resolve(peer);
      }
    });

    ws.on('error', (e) => reject(e));

    // Timeout registration
    setTimeout(() => {
      if (!peer.registered) reject(new Error(`Peer ${id} registration timed out`));
    }, 10_000);
  });
}

function sendAndWait(peer, msg, filterType, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const handler = (raw) => {
      const m = JSON.parse(raw);
      if (m.type === filterType) {
        peer.ws.off('message', handler);
        resolve(m);
      }
    };
    peer.ws.on('message', handler);
    peer.ws.send(JSON.stringify(msg));
    setTimeout(() => {
      peer.ws.off('message', handler);
      reject(new Error(`Timed out waiting for "${filterType}"`));
    }, timeoutMs);
  });
}

function waitForMessage(peer, filterType, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const handler = (raw) => {
      const m = JSON.parse(raw);
      if (m.type === filterType) {
        peer.ws.off('message', handler);
        resolve(m);
      }
    };
    peer.ws.on('message', handler);
    setTimeout(() => {
      peer.ws.off('message', handler);
      reject(new Error(`Timed out waiting for "${filterType}"`));
    }, timeoutMs);
  });
}

async function run() {
  console.log(`\n🔌 Connecting to ${SERVER}\n`);

  // ── 1. Registration ─────────────────────────────────
  console.log('── TEST 1: Registration ──');
  let peerA, peerB;
  try {
    peerA = await createPeer('test-peer-A');
    pass('Peer A registered');
  } catch (e) {
    fail('Peer A registration', e.message);
    process.exit(1);
  }

  try {
    peerB = await createPeer('test-peer-B');
    pass('Peer B registered');
  } catch (e) {
    fail('Peer B registration', e.message);
    peerA.ws.close();
    process.exit(1);
  }

  // ── 2. Heartbeat ────────────────────────────────────
  console.log('\n── TEST 2: Heartbeat ping/pong ──');
  try {
    const pong = await sendAndWait(peerA, { type: 'ping' }, 'pong');
    if (pong.ts && typeof pong.ts === 'number') {
      pass('Heartbeat ping/pong');
    } else {
      fail('Heartbeat ping/pong', 'Missing or invalid ts in pong');
    }
  } catch (e) {
    fail('Heartbeat ping/pong', e.message);
  }

  // ── 3. Broadcast relay ──────────────────────────────
  console.log('\n── TEST 3: Broadcast relay ──');
  try {
    const relayPromise = waitForMessage(peerB, 'relay');
    peerA.ws.send(JSON.stringify({
      type: 'relay',
      payload: { text: 'hello broadcast from A' },
    }));
    const relayed = await relayPromise;
    if (relayed.from === 'test-peer-A' && relayed.payload?.text === 'hello broadcast from A') {
      pass('Broadcast relay A → B');
    } else {
      fail('Broadcast relay A → B', 'Unexpected relay content');
    }
  } catch (e) {
    fail('Broadcast relay', e.message);
  }

  // ── 4. Targeted relay ───────────────────────────────
  console.log('\n── TEST 4: Targeted relay (A → B) ──');
  try {
    const relayPromise = waitForMessage(peerB, 'relay');
    peerA.ws.send(JSON.stringify({
      type: 'relay',
      to: 'test-peer-B',
      payload: { secret: 42 },
    }));
    const relayed = await relayPromise;
    if (relayed.from === 'test-peer-A' && relayed.payload?.secret === 42) {
      pass('Targeted relay A → B');
    } else {
      fail('Targeted relay A → B', 'Unexpected payload');
    }
  } catch (e) {
    fail('Targeted relay A → B', e.message);
  }

  // ── 5. Signal exchange (UDP hole-punch simulation) ──
  console.log('\n── TEST 5: Signal exchange (hole-punch) ──');
  try {
    const sigPromise = waitForMessage(peerB, 'signal');
    peerA.ws.send(JSON.stringify({
      type: 'signal',
      to: 'test-peer-B',
      signal: { type: 'offer', sdp: 'fake-sdp-offer' },
    }));
    const sig = await sigPromise;
    if (sig.from === 'test-peer-A' && sig.signal?.type === 'offer') {
      pass('Signal offer A → B');
    } else {
      fail('Signal offer A → B', 'Unexpected signal content');
    }

    // Answer back
    const ansPromise = waitForMessage(peerA, 'signal');
    peerB.ws.send(JSON.stringify({
      type: 'signal',
      to: 'test-peer-A',
      signal: { type: 'answer', sdp: 'fake-sdp-answer' },
    }));
    const ans = await ansPromise;
    if (ans.from === 'test-peer-B' && ans.signal?.type === 'answer') {
      pass('Signal answer B → A');
    } else {
      fail('Signal answer B → A', 'Unexpected signal content');
    }
  } catch (e) {
    fail('Signal exchange', e.message);
  }

  // ── 6. Peer list ────────────────────────────────────
  console.log('\n── TEST 6: Peer list ──');
  try {
    const list = await sendAndWait(peerA, { type: 'list' }, 'peers');
    if (Array.isArray(list.peers) && list.peers.length >= 2) {
      pass(`Peer list (${list.peers.length} peers)`);
    } else {
      fail('Peer list', `Expected >= 2 peers, got ${list.peers?.length}`);
    }
  } catch (e) {
    fail('Peer list', e.message);
  }

  // ── 7. Disconnect notification ──────────────────────
  console.log('\n── TEST 7: Disconnect notification ──');
  try {
    const leftPromise = waitForMessage(peerA, 'peer_left');
    peerB.ws.close();
    const left = await leftPromise;
    if (left.peerId === 'test-peer-B') {
      pass('Peer B disconnect notified to A');
    } else {
      fail('Disconnect notification', 'Wrong peerId in peer_left');
    }
  } catch (e) {
    fail('Disconnect notification', e.message);
  }

  // ── Summary ─────────────────────────────────────────
  peerA.ws.close();
  console.log('\n════════════════════════════════════');
  console.log('         TEST SUMMARY');
  console.log('════════════════════════════════════');
  const passed = results.filter((r) => r.ok).length;
  const failed = results.filter((r) => !r.ok).length;
  results.forEach((r) => {
    console.log(`  ${r.ok ? '✅' : '❌'} ${r.name}${r.reason ? ` (${r.reason})` : ''}`);
  });
  console.log(`\n  ${passed} passed, ${failed} failed\n`);
  process.exit(failed > 0 ? 1 : 0);
}

run().catch((e) => {
  console.error('Fatal error:', e);
  process.exit(1);
});
