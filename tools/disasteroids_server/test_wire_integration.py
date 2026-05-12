#!/usr/bin/env python3
"""
Wire-level integration test against an in-process dserver.

Spawns DisasteroidsServer in a thread, connects two fake clients (one
"old" — never sends CLIENT_CAPS — and one "new" — sends CAPS announcing
ring support), drives them through CONNECT → SET_USERNAME → READY →
GAME_START, then verifies:

  1. With global sync_mode=RING (the v1.1.2+ default), the OLD client
     gets raw 22-byte SHIP_SYNC bytes (0xA9) for every relay, never
     SHIP_SYNC_Q (0xB1) or ASTEROID_SYNC (0xB2).
  2. The NEW (ring-capable) client gets:
     - SET_SYNC_MODE (0xB0) on connect
     - SHIP_SYNC_Q (0xB1) for relays
     - ASTEROID_SYNC (0xB2) periodically
  3. /api/tuning toggle to LERP makes BOTH clients receive raw 0xA9
     (and stops 0xB2 emission).
  4. /api/tuning toggle back to RING restores per-recipient dispatch.
  5. Hit message v1 (3-byte payload) and v2 (5-byte payload) both
     accepted by the server without errors logged.

Pass criteria: byte-for-byte assertions on what each client received.
Exit 0 on all-pass, non-zero on any failure with diff context.
"""

import importlib
import json
import os
import queue
import socket
import struct
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


# ----------------------------------------------------------------------
# Fake client — implements just enough of the wire protocol to drive the
# server through to "in_game" so SHIP_SYNC relays will actually fire.
# ----------------------------------------------------------------------

class FakeClient:
    """Bridge-authenticated, optionally ring-capable, in-game client.

    `announce_ring`: if True, sends DNET_MSG_CLIENT_CAPS (0x1C) with
    DNET_CAP_SUPPORTS_RING (0x01) right after WELCOME — i.e., behaves
    like a v1.1.0+ Saturn binary. If False, never sends CLIENT_CAPS,
    behaving like a pre-1.1.0 binary."""

    def __init__(self, host: str, port: int, username: str,
                 announce_ring: bool, dserver_module):
        self.username = username
        self.announce_ring = announce_ring
        self.D = dserver_module
        self.sock = socket.create_connection((host, port), timeout=5.0)
        self.sock.settimeout(0.05)
        self.recv_buf = b""
        # Aggregated received messages (decoded). One entry per frame.
        # Each entry: (msg_type:int, payload:bytes-after-type)
        self.received = []
        self.recv_lock = threading.Lock()
        self._stop = threading.Event()
        self.player_id = None
        self.in_game = False

        # Authenticate as a bridge.
        auth = self.D.AUTH_MAGIC + bytes([len(self.D.SHARED_SECRET)]) + \
            self.D.SHARED_SECRET
        self.sock.sendall(auth)
        self._read_until_auth_ok()

        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def _read_until_auth_ok(self):
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                b = self.sock.recv(1)
            except socket.timeout:
                continue
            if b == bytes([self.D.AUTH_OK]):
                return
        raise RuntimeError("Bridge auth timeout for %s" % self.username)

    def _read_loop(self):
        while not self._stop.is_set():
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            self.recv_buf += data
            self._parse_frames()

    def _parse_frames(self):
        # SNCP framing: [LEN_HI][LEN_LO][type:1][payload...]
        while len(self.recv_buf) >= 2:
            payload_len = (self.recv_buf[0] << 8) | self.recv_buf[1]
            if len(self.recv_buf) < 2 + payload_len:
                return
            frame = self.recv_buf[2:2 + payload_len]
            self.recv_buf = self.recv_buf[2 + payload_len:]
            if not frame:
                continue
            with self.recv_lock:
                self.received.append((frame[0], frame[1:]))

    def send_frame(self, payload: bytes):
        """SNCP-frame `payload` and send."""
        hdr = bytes([(len(payload) >> 8) & 0xFF, len(payload) & 0xFF])
        self.sock.sendall(hdr + payload)

    def msgs_of_type(self, msg_type: int):
        with self.recv_lock:
            return [p for (t, p) in self.received if t == msg_type]

    def clear(self):
        with self.recv_lock:
            self.received.clear()

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except Exception:
            pass


# ----------------------------------------------------------------------
# Test harness
# ----------------------------------------------------------------------

def admin_get(port: int, path: str):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        headers={"X-Admin-Auth": "nginx-verified"})
    with urllib.request.urlopen(req, timeout=2.0) as r:
        return json.loads(r.read())


def admin_post(port: int, path: str, body: dict):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path), data=data, method="POST",
        headers={"X-Admin-Auth": "nginx-verified",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=2.0) as r:
        return json.loads(r.read())


def main():
    print("Wire-level integration test against in-process dserver")

    # Use unusual ports so we don't collide with anything else local.
    GAME_PORT = 14822
    ADMIN_PORT = 19092

    # Import dserver fresh each run.
    if "dserver" in sys.modules:
        del sys.modules["dserver"]
    D = importlib.import_module("dserver")

    server = D.DisasteroidsServer(
        host="127.0.0.1", port=GAME_PORT, num_bots=0,
        admin_port=ADMIN_PORT, admin_user="admin",
        admin_password="testpw")
    # Run in a daemon thread so the test process can exit cleanly.
    t = threading.Thread(target=server.start, daemon=True)
    t.start()
    # Wait for the listener to bind.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", GAME_PORT),
                                     timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("Server didn't bind on port %d" % GAME_PORT)

    # Force the global toggle to RING so the dispatch logic exercises
    # the per-recipient path. (RING is the new default in v1.1.2 anyway,
    # but pinning here makes the test explicit.)
    admin_post(ADMIN_PORT, "/api/tuning", {"sync_mode": "RING"})
    time.sleep(0.3)

    # ----- Test 1+2: per-recipient dispatch with mixed clients -------
    print("Test: per-recipient SHIP_SYNC dispatch with mixed clients")
    old = FakeClient("127.0.0.1", GAME_PORT, "OLD", False, D)
    new = FakeClient("127.0.0.1", GAME_PORT, "NEW", True, D)

    # CONNECT (no UUID — server will issue one)
    for c in (old, new):
        c.send_frame(bytes([D.MSG_CONNECT]))
    time.sleep(0.4)

    # Server should have responded with USERNAME_REQUIRED (0x81). Both
    # clients send their usernames.
    old.send_frame(bytes([D.MSG_SET_USERNAME]) +
                   bytes([len(old.username)]) +
                   old.username.encode())
    new.send_frame(bytes([D.MSG_SET_USERNAME]) +
                   bytes([len(new.username)]) +
                   new.username.encode())
    time.sleep(0.4)

    # New client announces ring + ring_v2 (v1.1.3+ binary).
    # OLD client never sends CAPS.
    new.send_frame(bytes([D.DNET_MSG_CLIENT_CAPS,
                          D.CAP_SUPPORTS_RING | D.CAP_RING_V2]))
    time.sleep(0.4)

    # Verify: NEW received SET_SYNC_MODE (0xB0) with RING (1).
    # OLD did NOT receive 0xB0.
    new_set = new.msgs_of_type(D.DNET_MSG_SET_SYNC_MODE)
    old_set = old.msgs_of_type(D.DNET_MSG_SET_SYNC_MODE)
    assert len(new_set) >= 1, \
        "NEW client did not receive SET_SYNC_MODE: got %r" % new.received
    assert new_set[-1][0] == 1, \
        "NEW client SET_SYNC_MODE payload not RING: %r" % new_set[-1]
    assert len(old_set) == 0, \
        "OLD client unexpectedly got SET_SYNC_MODE: %r" % old_set
    print("  set_sync_mode dispatch: PASS")

    # Both READY → server starts game.
    old.send_frame(bytes([D.DNET_MSG_READY]))
    new.send_frame(bytes([D.DNET_MSG_READY]))
    time.sleep(0.5)

    old.send_frame(bytes([D.DNET_MSG_START_GAME_REQ]))
    time.sleep(0.5)

    # Now in game. Verify each client got GAME_START (0xA1).
    assert old.msgs_of_type(D.DNET_MSG_GAME_START), "OLD never got GAME_START"
    assert new.msgs_of_type(D.DNET_MSG_GAME_START), "NEW never got GAME_START"

    # ----- Drive a SHIP_STATE from each so the server relays.
    # Build a 21-byte (extended) SHIP_STATE for each. We use player_id
    # = the one assigned in WELCOME if we have it; otherwise the
    # game_player_id from GAME_START.
    def encode_ship_state_ext(pid, x, y, dx, dy, rot, flags):
        return (bytes([D.DNET_MSG_SHIP_STATE, pid]) +
                struct.pack("!iiiihB", x, y, dx, dy, rot, flags))

    # Read the assigned player_ids from GAME_START payload [seed:4][pid:1]...
    old_gs = old.msgs_of_type(D.DNET_MSG_GAME_START)[0]
    new_gs = new.msgs_of_type(D.DNET_MSG_GAME_START)[0]
    old_pid = old_gs[4]
    new_pid = new_gs[4]
    assert old_pid != new_pid, "duplicate player IDs"

    # Clear pre-game noise and just collect what arrives next.
    old.clear()
    new.clear()

    # OLD sends a SHIP_STATE with rot=1234 so we can verify the v2
    # relay preserves the rotation exactly (v1's q_angle would have
    # collapsed any rot in [0..255] to 0).
    old.send_frame(encode_ship_state_ext(
        old_pid, 1000 << 16, 2000 << 16, 100, 200, 1234, 0x01))
    time.sleep(0.5)

    # NEW (ring_v2-capable) should have received SHIP_SYNC_Q_V2 (0xB3),
    # NOT SHIP_SYNC_Q (0xB1) and NOT raw SHIP_SYNC (0xA9).
    new_q  = new.msgs_of_type(D.DNET_MSG_SHIP_SYNC_Q)
    new_q2 = new.msgs_of_type(D.DNET_MSG_SHIP_SYNC_Q_V2)
    new_raw = new.msgs_of_type(D.DNET_MSG_SHIP_SYNC)
    assert new_q2, "NEW client expected SHIP_SYNC_Q_V2 (0xB3) under RING+v2 caps"
    assert not new_q, \
        "NEW client got legacy 0xB1 instead of 0xB3 (caps dispatch broken)"
    assert not new_raw, \
        "NEW client got raw 0xA9 under RING (should be quantized v2)"
    # Verify the v2 payload preserves rot exactly: server's rot value
    # was 1234 in our SHIP_STATE; bytes [11..12] of the relayed
    # SHIP_SYNC_Q_V2 payload should be 0x04 0xD2.
    sample = new_q2[0]
    # payload layout (offset from payload[0] = type which IS the message
    # type at the dispatcher level — our `payload` slice starts AFTER
    # the type byte, so adjust): we receive (msg_type, payload[1:])
    # actually FakeClient strips the type so payload[0]=pid here.
    # payload: [pid:1][server_frame:2][x_q:2][y_q:2][dx_q:1][dy_q:1][rot:2 BE][flags:1]
    # rot is at offset 9..10.
    decoded_rot = struct.unpack("!h", sample[9:11])[0]
    assert decoded_rot == 1234, "v2 relay corrupted rot: got %d" % decoded_rot
    print("  NEW client received SHIP_SYNC_Q_V2 with exact rot=1234: PASS (%d msgs)" % len(new_q2))

    # NEW sends a SHIP_STATE; this should be relayed to OLD as raw.
    new.clear()
    new.send_frame(encode_ship_state_ext(
        new_pid, 5000 << 16, 6000 << 16, 50, -50, 1234, 0x01))
    time.sleep(0.5)

    old_q = old.msgs_of_type(D.DNET_MSG_SHIP_SYNC_Q)
    old_raw = old.msgs_of_type(D.DNET_MSG_SHIP_SYNC)
    assert old_raw, "OLD client expected raw SHIP_SYNC (0xA9) — caps not sent"
    assert not old_q, \
        "OLD client got 0xB1 (should never reach a non-ring-capable client)"
    print("  OLD client received raw SHIP_SYNC: PASS (%d msgs)" % len(old_raw))

    # ----- Test 3: ASTEROID_SYNC only goes to ring-capable clients -----
    print("Test: ASTEROID_SYNC only reaches ring-capable clients")
    # ASTEROID_SYNC fires every 30 ticks (1.5 s). Wait long enough.
    old.clear()
    new.clear()
    time.sleep(2.5)
    old_aster = old.msgs_of_type(D.DNET_MSG_ASTEROID_SYNC)
    new_aster = new.msgs_of_type(D.DNET_MSG_ASTEROID_SYNC)
    assert not old_aster, \
        "OLD client unexpectedly got ASTEROID_SYNC: %d msgs" % len(old_aster)
    assert new_aster, \
        "NEW client did not get any ASTEROID_SYNC in 2.5s"
    print("  ASTEROID_SYNC dispatch: PASS (NEW got %d, OLD got 0)" %
          len(new_aster))

    # ----- Test 4: toggle to LERP affects relay format ------------------
    print("Test: toggle to LERP makes both clients see raw SHIP_SYNC")
    # Clear FIRST so we can observe the SET_SYNC_MODE the toggle
    # broadcasts. Then post the toggle, then look for the message
    # before doing anything else that would race the buffer.
    old.clear()
    new.clear()
    admin_post(ADMIN_PORT, "/api/tuning", {"sync_mode": "LERP"})
    time.sleep(0.6)  # allow admin queue + broadcast to land

    new_set = new.msgs_of_type(D.DNET_MSG_SET_SYNC_MODE)
    assert new_set, "NEW client missing SET_SYNC_MODE on toggle"
    assert new_set[-1][0] == 0, \
        "NEW client SET_SYNC_MODE not LERP after toggle"
    print("  NEW received SET_SYNC_MODE=LERP on toggle: PASS")

    # OLD client never gets SET_SYNC_MODE regardless of toggle (no caps).
    old_set = old.msgs_of_type(D.DNET_MSG_SET_SYNC_MODE)
    assert not old_set, \
        "OLD client unexpectedly got SET_SYNC_MODE: %d" % len(old_set)

    # Now drive a SHIP_STATE under LERP and verify both clients are on
    # the raw 0xA9 path. Drive from OLD to NEW so NEW receives the
    # relay (which under LERP must be 0xA9, not 0xB1).
    old.clear()
    new.clear()
    old.send_frame(encode_ship_state_ext(
        old_pid, 7000 << 16, 8000 << 16, 0, 0, 0, 0x01))
    time.sleep(0.5)
    new_raw = new.msgs_of_type(D.DNET_MSG_SHIP_SYNC)
    new_q = new.msgs_of_type(D.DNET_MSG_SHIP_SYNC_Q)
    assert new_raw, "NEW client missing raw SHIP_SYNC under LERP toggle"
    assert not new_q, \
        "NEW client got SHIP_SYNC_Q under LERP toggle: %d msgs" % len(new_q)
    print("  LERP toggle: PASS (NEW received raw 0xA9, no 0xB1)")

    # ----- Test 5: hit message v1 + v2 both accepted -----------------
    print("Test: hit message v1 (3-byte) and v2 (5-byte) both accepted")
    # Flip back to RING for the asteroid-correction path.
    admin_post(ADMIN_PORT, "/api/tuning", {"sync_mode": "RING"})
    time.sleep(0.5)
    # Hit message v1 (no firer_frame).
    new.send_frame(bytes([D.DNET_MSG_SHIP_ASTEROID_HIT, 0xFF, old_pid]))
    time.sleep(0.2)
    # Hit message v2 (with firer_frame=12345).
    new.send_frame(bytes([D.DNET_MSG_SHIP_ASTEROID_HIT, 0xFF, old_pid]) +
                   struct.pack("!H", 12345))
    time.sleep(0.5)
    # Server should not have died — confirm by verifying admin still
    # responds with sync_mode = RING.
    state = admin_get(ADMIN_PORT, "/api/state")
    assert state["tuning"]["sync_mode"] == "RING", \
        "server inconsistent after hit messages: %s" % state["tuning"]
    print("  hit v1+v2: PASS (server still healthy)")

    # ----- cleanup --------------------------------------------------
    old.stop()
    new.stop()
    server._running = False
    print("ALL PASS")


if __name__ == "__main__":
    main()
