#!/usr/bin/env python3
"""
Disasteroids NetLink Game Server

Manages online multiplayer for Disasteroids. Architecture follows the
Coup server pattern: bridge-authenticated connections, SNCP binary framing,
lobby management, and server-authoritative state sync.

Networking model: SERVER-AUTHORITATIVE STATE SYNC
  Server owns all randomness and authoritative game events.
  Each Saturn sends local player inputs and ship state.
  Server generates asteroid data, detects ship-asteroid collisions,
  manages waves and game over conditions.
  Saturns run deterministic asteroid physics from server-provided data.

Usage:
    python3 tools/disasteroids_server/dserver.py
    python3 tools/disasteroids_server/dserver.py --port 4822 --bots 2
"""

import argparse
import base64
import collections
import json
import logging
import math
import os
import queue
import random
import select
import socket
import struct
import sys
import threading
import time
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("disasteroids_server")

# ==========================================================================
# Constants
# ==========================================================================

HEARTBEAT_TIMEOUT = 60.0
MAX_RECV_BUFFER = 8192
USERNAME_MAX_LEN = 16
UUID_LEN = 36

# Bridge authentication
SHARED_SECRET = b"SaturnDisasteroids2026!NetLink#Key"
AUTH_MAGIC = b"AUTH"
AUTH_OK = 0x01
AUTH_TIMEOUT = 5.0

MAX_BRIDGES = 10
MAX_PLAYERS = 12  # Disasteroids online: up to 12 players

# SNCP Auth Messages
MSG_CONNECT = 0x01
MSG_SET_USERNAME = 0x02
MSG_HEARTBEAT = 0x04
MSG_DISCONNECT = 0x05

MSG_USERNAME_REQUIRED = 0x81
MSG_WELCOME = 0x82
MSG_WELCOME_BACK = 0x83
MSG_USERNAME_TAKEN = 0x84

# Disasteroids Messages — Client -> Server
DNET_MSG_READY = 0x10
DNET_MSG_INPUT_STATE = 0x11
DNET_MSG_START_GAME_REQ = 0x12
DNET_MSG_PAUSE_REQ = 0x13
DNET_MSG_SHIP_STATE = 0x14
DNET_MSG_ASTEROID_HIT = 0x15
DNET_MSG_ADD_LOCAL_PLAYER = 0x16
DNET_MSG_ADD_BOT = 0x17
DNET_MSG_REMOVE_BOT = 0x18
DNET_MSG_REMOVE_LOCAL_PLAYER = 0x19
DNET_MSG_SHIP_ASTEROID_HIT = 0x1A
DNET_MSG_LEADERBOARD_REQ = 0x1B
DNET_MSG_CLIENT_CAPS = 0x1C  # [caps:1] bit0=supports_ring

# Disasteroids Messages — Server -> Client
DNET_MSG_LOBBY_STATE = 0xA0
DNET_MSG_GAME_START = 0xA1
DNET_MSG_INPUT_RELAY = 0xA2
DNET_MSG_PLAYER_JOIN = 0xA3
DNET_MSG_PLAYER_LEAVE = 0xA4
DNET_MSG_GAME_OVER = 0xA5
DNET_MSG_LOG = 0xA6
DNET_MSG_PAUSE_ACK = 0xA7
DNET_MSG_SETTINGS_UPDATE = 0xA8
DNET_MSG_SHIP_SYNC = 0xA9
DNET_MSG_ASTEROID_SPAWN = 0xAA
DNET_MSG_ASTEROID_DESTROY = 0xAB
DNET_MSG_WAVE_EVENT = 0xAC
DNET_MSG_PLAYER_KILL = 0xAD
DNET_MSG_PLAYER_SPAWN = 0xAE
DNET_MSG_LOCAL_PLAYER_ACK = 0x86
DNET_MSG_LEADERBOARD_DATA = 0xAF
DNET_MSG_SET_SYNC_MODE = 0xB0  # [mode:1] (0=LERP, 1=RING)
DNET_MSG_SHIP_SYNC_Q = 0xB1    # quantized SHIP_SYNC v1 (12-byte payload,
                               # 1-byte q_angle — broken for plain-degree rot)
DNET_MSG_ASTEROID_SYNC = 0xB2  # periodic asteroid position correction
DNET_MSG_SHIP_SYNC_Q_V2 = 0xB3 # quantized SHIP_SYNC v2 (13-byte payload,
                               # int16 raw rot — fixes v1 quantization bug)

# Sync-mode wire constants. MUST match Saturn-side DNET_SYNC_MODE_* in
# disasteroids_protocol.h. Stored as strings in self.tuning for human-friendly
# admin API; converted to byte for the wire by _sync_mode_to_byte() below.
SYNC_MODE_BYTE = {"LERP": 0, "RING": 1}

# Capability flag bits sent in CLIENT_CAPS payload.
CAP_SUPPORTS_RING = 0x01
CAP_RING_V2       = 0x02   # v1.1.3 — client wants the 13-byte 0xB3 form

# Quantization shift amounts. MUST match Saturn-side DNET_QPOS_SHIFT etc.
QPOS_SHIFT = 9       # 1/128 unit precision, ±256 unit range
QVEL_SHIFT = 10      # 1/64 unit/frame precision, ±2 unit/frame range
QANGLE_SHIFT = 8     # 256 levels = 1.4° precision

# Snapshot-ring depth on the server side (Phase 4 lag-comp). 30 entries
# at the per-player SHIP_STATE rate (~7.5 Hz) gives ~4 sec of history —
# more than enough to rewind any in-flight projectile.
SERVER_SNAP_RING_DEPTH = 30

# Game types (matching Disasteroids GAME_TYPE enum)
GAME_TYPE_COOP = 0
GAME_TYPE_VERSUS = 1

# Bot difficulty levels
BOT_DIFFICULTY_EASY = 0
BOT_DIFFICULTY_MEDIUM = 1
BOT_DIFFICULTY_HARD = 2
BOT_DIFFICULTY_DEFAULT = BOT_DIFFICULTY_MEDIUM

# Bot names (cycled through when adding bots)
BOT_NAMES = [
    "DANTE", "RANDAL", "JAY", "ELIAS",
    "BECKY", "BRODIE", "RENE", "BANKY",
    "AZRAEL", "LOKI", "BART", "RUFUS",
]

# Game constants (matching gameplay_constants.h)
MAX_DISASTEROIDS = 50
MAX_WAVE = 99
DISASTEROID_SPAWN_TIMER = 150  # INVULNERABILITY_TIMER + 30
INVULNERABILITY_TIMER = 120    # 2 * 60fps
RESPAWN_TIMER = 120            # 2 * 60fps
FIXED_SCALE = 65536            # jo_fixed 16.16 scale factor

# Disasteroid sizes
DISASTEROID_SIZE_SMALL = 0
DISASTEROID_SIZE_MEDIUM = 1
DISASTEROID_SIZE_LARGE = 2
DISASTEROID_SIZE_MAX = 3
NUM_DISASTEROID_VARIATIONS = 16

# Collision radii (in integer screen pixels)
PLAYER_SHIP_RADIUS = 5
DISASTEROID_RADIUS_SMALL = 4
DISASTEROID_RADIUS_MEDIUM = 7
DISASTEROID_RADIUS_LARGE = 10

# Screen bounds (integer, matching gameplay_constants.h)
SCREEN_MIN_X = -160
SCREEN_MAX_X = 160
SCREEN_MIN_Y = -120
SCREEN_MAX_Y = 120

# ==========================================================================
# SNCP Framing
# ==========================================================================


def encode_frame(payload: bytes) -> bytes:
    """Wrap payload in SNCP length-prefixed frame."""
    return struct.pack("!H", len(payload)) + payload


def encode_lp_string(s: str) -> bytes:
    """Encode a length-prefixed string."""
    raw = s.encode("utf-8")[:255]
    return struct.pack("B", len(raw)) + raw


def encode_uuid(uuid_str: str) -> bytes:
    """Encode a fixed-length UUID (36 bytes ASCII)."""
    raw = uuid_str.encode("ascii")[:UUID_LEN]
    return raw.ljust(UUID_LEN, b'\x00')


# ==========================================================================
# Message Builders
# ==========================================================================


def build_username_required() -> bytes:
    return encode_frame(bytes([MSG_USERNAME_REQUIRED]))


def build_welcome(user_id: int, uuid_str: str, username: str) -> bytes:
    payload = (bytes([MSG_WELCOME])
               + struct.pack("B", user_id & 0xFF)
               + encode_uuid(uuid_str)
               + encode_lp_string(username))
    return encode_frame(payload)


def build_welcome_back(user_id: int, uuid_str: str, username: str) -> bytes:
    payload = (bytes([MSG_WELCOME_BACK])
               + struct.pack("B", user_id & 0xFF)
               + encode_uuid(uuid_str)
               + encode_lp_string(username))
    return encode_frame(payload)


def build_username_taken() -> bytes:
    return encode_frame(bytes([MSG_USERNAME_TAKEN]))


def build_lobby_state(players: list) -> bytes:
    count = min(len(players), MAX_PLAYERS)
    payload = bytes([DNET_MSG_LOBBY_STATE, count])
    for p in players[:count]:
        payload += struct.pack("B", p["id"])
        payload += encode_lp_string(p["name"])
        payload += struct.pack("B", 1 if p["ready"] else 0)
    return encode_frame(payload)


def build_game_start(seed: int, player_id: int, opponent_count: int,
                     game_type: int, num_lives: int) -> bytes:
    payload = bytes([DNET_MSG_GAME_START])
    payload += struct.pack("!I", seed & 0xFFFFFFFF)
    payload += bytes([player_id, opponent_count, game_type, num_lives])
    return encode_frame(payload)


def build_input_relay(player_id: int, frame_num: int,
                      input_bits: int) -> bytes:
    payload = bytes([DNET_MSG_INPUT_RELAY])
    payload += bytes([player_id])
    payload += struct.pack("!HH", frame_num & 0xFFFF, input_bits & 0xFFFF)
    return encode_frame(payload)


def build_player_join(player_id: int, name: str) -> bytes:
    payload = bytes([DNET_MSG_PLAYER_JOIN, player_id])
    payload += encode_lp_string(name)
    return encode_frame(payload)


def build_player_leave(player_id: int) -> bytes:
    return encode_frame(bytes([DNET_MSG_PLAYER_LEAVE, player_id]))


def build_game_over(winner_id: int) -> bytes:
    return encode_frame(bytes([DNET_MSG_GAME_OVER, winner_id]))


def build_leaderboard_data(entries: list) -> bytes:
    """Build LEADERBOARD_DATA message. entries = list of dicts with name,wins,best_score,games_played."""
    count = min(len(entries), 10)
    payload = bytes([DNET_MSG_LEADERBOARD_DATA, count])
    for e in entries[:count]:
        name_bytes = e["name"].encode("utf-8")[:16]
        payload += struct.pack("B", len(name_bytes)) + name_bytes
        payload += struct.pack("!HHH",
                               min(e.get("wins", 0), 65535),
                               min(e.get("best_score", 0), 65535),
                               min(e.get("games_played", 0), 65535))
    return encode_frame(payload)


def build_log(text: str) -> bytes:
    raw = text.encode("utf-8")[:255]
    payload = bytes([DNET_MSG_LOG, len(raw)]) + raw
    return encode_frame(payload)


def build_pause_ack(paused: bool) -> bytes:
    return encode_frame(bytes([DNET_MSG_PAUSE_ACK, 1 if paused else 0]))


def build_ship_sync(player_id: int, x: int, y: int, dx: int, dy: int,
                    rot: int, flags: int) -> bytes:
    payload = bytes([DNET_MSG_SHIP_SYNC, player_id & 0xFF])
    payload += struct.pack("!iiiihB", x, y, dx, dy, rot, flags)
    return encode_frame(payload)


def build_ship_sync_raw(player_id: int, raw_payload: bytes) -> bytes:
    """Relay raw ship state bytes from client, just prepend SHIP_SYNC + player_id."""
    payload = bytes([DNET_MSG_SHIP_SYNC, player_id & 0xFF]) + raw_payload
    return encode_frame(payload)


# === Phase 3: quantized SHIP_SYNC + sync-mode helpers ====================
# Shift-only quantizers (no divide). Saturn-side decoders are pure left
# shifts of these results — see DNET_QPOS_SHIFT/DNET_QVEL_SHIFT/DNET_QANGLE_SHIFT
# in disasteroids_protocol.h. Round-trip parity is required; tested in the
# `_q_parity_selftest()` function called from main().


def _q_clamp(v: int, lo: int, hi: int) -> int:
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def q_pos(fxp: int) -> int:
    """Quantize fxp position to int16 (>>9 → 1/128 unit precision)."""
    # Arithmetic shift right (Python ints are unbounded so `>>` does what
    # we want on signed values). Clamp to the int16 wire range to defend
    # against rare out-of-bounds positions before they corrupt the packet.
    return _q_clamp(fxp >> QPOS_SHIFT, -32768, 32767)


def q_vel(fxp: int) -> int:
    """Quantize fxp velocity to int8 (>>10 → 1/64 unit/frame precision)."""
    return _q_clamp(fxp >> QVEL_SHIFT, -128, 127)


def q_angle(rot: int) -> int:
    """Quantize SGL int16 angle to uint8 (>>8 → 1.4° precision).
    The angle wraps modulo 65536, so we mask first to a uint16 range,
    then shift. Result is in [0, 255]."""
    return (rot & 0xFFFF) >> QANGLE_SHIFT


def build_ship_sync_quant(player_id: int, server_frame: int,
                          x: int, y: int, dx: int, dy: int,
                          rot: int, flags: int) -> bytes:
    """Quantized SHIP_SYNC v1 for RING-capable clients (legacy, v1.1.0-v1.1.2).
    12-byte payload — but q_angle is BROKEN for plain-degree rot. Kept for
    backward compat with clients that don't announce CAP_RING_V2.

    Wire: [type:1][pid:1][server_frame:2 BE][x_q:i16 BE][y_q:i16 BE]
          [dx_q:i8][dy_q:i8][angle_q:u8][flags:1]
    """
    payload = bytes([DNET_MSG_SHIP_SYNC_Q, player_id & 0xFF])
    payload += struct.pack("!H", server_frame & 0xFFFF)
    payload += struct.pack("!hh", q_pos(x), q_pos(y))
    payload += struct.pack("!bb", q_vel(dx), q_vel(dy))
    payload += struct.pack("!BB", q_angle(rot), flags & 0xFF)
    return encode_frame(payload)


def build_ship_sync_quant_v2(player_id: int, server_frame: int,
                              x: int, y: int, dx: int, dy: int,
                              rot: int, flags: int) -> bytes:
    """Quantized SHIP_SYNC v2 for clients announcing CAP_RING_V2.
    13-byte payload — sends raw int16 rot instead of the broken q_angle
    (rot in Disasteroids is plain integer degrees 0..359, so q_angle's
    `(rot & 0xFFFF) >> 8` collapsed 71% of the circle to 0°).

    Wire: [type:1][pid:1][server_frame:2 BE][x_q:i16 BE][y_q:i16 BE]
          [dx_q:i8][dy_q:i8][rot:i16 BE][flags:1]
    """
    payload = bytes([DNET_MSG_SHIP_SYNC_Q_V2, player_id & 0xFF])
    payload += struct.pack("!H", server_frame & 0xFFFF)
    payload += struct.pack("!hh", q_pos(x), q_pos(y))
    payload += struct.pack("!bb", q_vel(dx), q_vel(dy))
    # Clamp rot to int16 range (it should already be 0..359 from Saturn,
    # but defend against any out-of-range values reaching the encoder).
    rot_i16 = max(-32768, min(32767, int(rot)))
    payload += struct.pack("!hB", rot_i16, flags & 0xFF)
    return encode_frame(payload)


def build_set_sync_mode(mode_str: str) -> bytes:
    """Tell a client which sync engine to run. mode_str is 'LERP' or 'RING'."""
    payload = bytes([DNET_MSG_SET_SYNC_MODE, SYNC_MODE_BYTE.get(mode_str, 0)])
    return encode_frame(payload)


def build_asteroid_sync(entries) -> bytes:
    """Periodic asteroid position correction (RING + asteroid_sync_correct).

    `entries` is an iterable of (slot, x, y, dx, dy) tuples. Wire format:
    [type:1=0xB2][count:1]{slot:1,x:4 BE,y:4 BE,dx:4 BE,dy:4 BE}xN

    Bandwidth bounded: at MAX_DISASTEROIDS=12 active asteroids,
    payload = 1 + 1 + 12*17 = 206 bytes; sent every 30 ticks (1.5 s)
    = ~137 B/s — small fraction of the 1440 B/s modem ceiling.
    """
    parts = [bytes([DNET_MSG_ASTEROID_SYNC])]
    n = 0
    body = b""
    for slot, x, y, dx, dy in entries:
        body += bytes([slot & 0xFF])
        body += struct.pack("!iiii", x, y, dx, dy)
        n += 1
        if n >= 255:
            break
    parts.append(bytes([n & 0xFF]))
    parts.append(body)
    return encode_frame(b"".join(parts))


def _q_parity_selftest():
    """Round-trip a representative set of values through quantize→dequantize
    and verify error is within the documented precision. Run at startup so
    a botched edit to the q_* helpers fails loudly instead of silently
    corrupting wire data."""
    # Mirror the Saturn-side decoders.
    def d_pos(q: int) -> int:
        # Saturn does ((int32_t)q) << 9. For Python signed ints that is
        # the same as q * 512 since q is already signed.
        return q * (1 << QPOS_SHIFT)

    def d_vel(q: int) -> int:
        return q * (1 << QVEL_SHIFT)

    def d_angle(q: int) -> int:
        # Saturn does ((uint16_t)q) << 8 then casts to int16.
        v = (q & 0xFF) << QANGLE_SHIFT
        if v >= 0x8000:
            v -= 0x10000
        return v

    # Position: ±256 world units in fxp 16.16. q_pos has 1/128 unit precision.
    pos_max_err = 0
    for fxp in (0, 65536, -65536, 32768, -32768,  # 1, -1, 0.5, -0.5 units
                256 * 65536 - 1, -256 * 65536):
        err = abs(fxp - d_pos(q_pos(fxp)))
        pos_max_err = max(pos_max_err, err)
    assert pos_max_err < 512, "q_pos parity failed: max err %d > 512" % pos_max_err

    # Velocity: ±2 units/frame. q_vel has 1/64 unit/frame precision.
    vel_max_err = 0
    for fxp in (0, 1024, -1024, 32768, -32768,  # tiny, half-unit, full-unit
                127 * 1024, -128 * 1024):
        err = abs(fxp - d_vel(q_vel(fxp)))
        vel_max_err = max(vel_max_err, err)
    assert vel_max_err < 1024, "q_vel parity failed: max err %d > 1024" % vel_max_err

    # Angle: 256 levels around the 65536-unit circle. ~256-unit max error.
    ang_max_err = 0
    for rot in (0, 16384, -16384, 32767, -32768, 65535, 1):
        decoded = d_angle(q_angle(rot))
        # Both decoded and rot represent angles modulo 65536; compute the
        # circular distance as the smaller of |a-b| and 65536-|a-b|.
        diff = abs(((rot - decoded) & 0xFFFF))
        if diff > 32768:
            diff = 65536 - diff
        ang_max_err = max(ang_max_err, diff)
    assert ang_max_err < 256, "q_angle parity failed: max err %d >= 256" % ang_max_err


def _sync_mode_byte(mode_str: str) -> int:
    return SYNC_MODE_BYTE.get(mode_str, 0)


def _asteroid_sync_selftest():
    """v1.1.1 — sanity-check the asteroid drift correction subsystem.

    Three parts:
      1. Saturn-matching edge-snap wrap places overshoots at the opposite
         edge exactly (matches Saturn `boundGameplayObject`).
      2. build_asteroid_sync round-trip — emit then manually decode and
         verify field-by-field, so a future struct.pack mistake fails
         loudly at startup instead of silently corrupting wire data.
      3. Tick catch-up loop advances exactly N ticks for a wall-clock
         delta of N*tick_interval (no off-by-one, no missed ticks under
         jitter). We model the loop arithmetic directly here.
    """
    # 1. Wrap parity. SCREEN bounds in fixed-point (FIXED_SCALE = 65536).
    lo = SCREEN_MIN_X
    hi = SCREEN_MAX_X
    lo_f = lo * FIXED_SCALE
    hi_f = hi * FIXED_SCALE
    # Overshoot past hi → snap to lo (same as Saturn).
    assert _wrap_coord_saturn(hi_f + 12345, lo, hi) == lo_f, "wrap_saturn hi"
    assert _wrap_coord_saturn(lo_f - 12345, lo, hi) == hi_f, "wrap_saturn lo"
    # In-bounds → unchanged.
    assert _wrap_coord_saturn(0, lo, hi) == 0, "wrap_saturn in-bounds"

    # 2. Round-trip build_asteroid_sync. Manually unpack what we built.
    sample = [(0, 100, 200, 300, 400),
              (5, -1, -2, -3, -4),
              (49, FIXED_SCALE * 100, FIXED_SCALE * -50, 1000, -2000)]
    frame = build_asteroid_sync(sample)
    # Frame layout: [LEN_HI][LEN_LO][TYPE=0xB2][count]{17 bytes}xN
    payload_len = (frame[0] << 8) | frame[1]
    assert payload_len == 2 + 17 * len(sample), \
        "build_asteroid_sync length wrong: %d" % payload_len
    assert frame[2] == DNET_MSG_ASTEROID_SYNC, "wrong msg type"
    assert frame[3] == len(sample), "wrong count"
    off = 4
    for i, (slot, x, y, dx, dy) in enumerate(sample):
        s_slot = frame[off]
        s_x, s_y, s_dx, s_dy = struct.unpack("!iiii", frame[off + 1:off + 17])
        assert s_slot == (slot & 0xFF), "entry %d slot" % i
        assert s_x == x and s_y == y and s_dx == dx and s_dy == dy, \
            "entry %d field mismatch" % i
        off += 17

    # 2b. build_ship_sync_quant_v2 round-trip (v1.1.3): the new 13-byte
    # form replaces broken q_angle with raw int16 rot. Verify exact
    # preservation of rot values that the v1 form silently destroyed
    # (rot ∈ [0..255] all collapsed to 0° under v1's q_angle = rot>>8).
    test_rots = [0, 1, 7, 14, 90, 91, 180, 255, 256, 359, -180, 32767]
    for r in test_rots:
        frame = build_ship_sync_quant_v2(5, 0x1234, 0, 0, 0, 0, r, 0)
        # Frame layout: [LEN_HI][LEN_LO][TYPE=0xB3][pid][server_frame:2]
        # [x_q:2][y_q:2][dx_q:1][dy_q:1][rot:2 BE][flags:1] = 13 payload
        payload_len = (frame[0] << 8) | frame[1]
        assert payload_len == 13, "v2 length wrong: %d" % payload_len
        assert frame[2] == DNET_MSG_SHIP_SYNC_Q_V2, "v2 type wrong"
        # frame[0..1]=SNCP, frame[2]=type, frame[3]=pid, frame[4..5]=server_frame,
        # frame[6..7]=x_q, frame[8..9]=y_q, frame[10]=dx_q, frame[11]=dy_q,
        # frame[12..13]=rot BE, frame[14]=flags
        decoded_rot = struct.unpack("!h", frame[12:14])[0]
        expected = max(-32768, min(32767, r))
        assert decoded_rot == expected, \
            "v2 rot round-trip failed: in=%d out=%d" % (r, decoded_rot)

    # 3. Tick catch-up arithmetic. Given a wall-clock interval and a tick
    # interval, the loop should run floor(elapsed / tick_interval) ticks.
    interval = 0.05  # 20 Hz
    cap = 10
    for elapsed_ticks in [1, 3, 9, 10, 11, 25, 100]:
        elapsed = interval * elapsed_ticks
        last_tick = 0.0
        now = elapsed
        ran = 0
        while now - last_tick >= interval and ran < cap:
            last_tick += interval
            ran += 1
        # Below the cap → ran exactly elapsed_ticks; at/above cap → ran=cap.
        expected = min(elapsed_ticks, cap)
        assert ran == expected, \
            "catchup %s: ran=%d expected=%d" % (elapsed_ticks, ran, expected)
# =========================================================================


def build_asteroid_destroy(slot: int, scorer_id: int,
                           children: list) -> bytes:
    """children: list of (child_slot, dx, dy, size, type) tuples."""
    payload = bytes([DNET_MSG_ASTEROID_DESTROY, slot & 0xFF,
                     scorer_id & 0xFF, len(children) & 0xFF])
    for child_slot, cdx, cdy, csize, ctype in children:
        payload += struct.pack("B", child_slot & 0xFF)
        payload += struct.pack("!ii", cdx, cdy)
        payload += bytes([csize & 0xFF, ctype & 0xFF])
    return encode_frame(payload)


def build_wave_event(wave: int, asteroids: list,
                     spawn_timer: int) -> bytes:
    """asteroids: list of (x, y, dx, dy, size, type) tuples."""
    count = min(len(asteroids), MAX_DISASTEROIDS)
    payload = bytes([DNET_MSG_WAVE_EVENT, wave & 0xFF, count & 0xFF])
    payload += struct.pack("!H", spawn_timer & 0xFFFF)
    for x, y, dx, dy, size, atype in asteroids[:count]:
        payload += struct.pack("!ii", x, y)
        payload += struct.pack("!ii", dx, dy)
        payload += bytes([size & 0xFF, atype & 0xFF])
    return encode_frame(payload)


def build_player_kill(player_id: int, lives: int, angle: int,
                      invuln: int, respawn: int) -> bytes:
    payload = bytes([DNET_MSG_PLAYER_KILL, player_id & 0xFF, lives & 0xFF])
    payload += struct.pack("!hHH", angle, invuln & 0xFFFF, respawn & 0xFFFF)
    return encode_frame(payload)


def build_player_spawn(player_id: int, angle: int,
                       invuln: int) -> bytes:
    payload = bytes([DNET_MSG_PLAYER_SPAWN, player_id & 0xFF])
    payload += struct.pack("!hH", angle, invuln & 0xFFFF)
    return encode_frame(payload)


def build_local_player_ack(player_id: int) -> bytes:
    return encode_frame(bytes([DNET_MSG_LOCAL_PLAYER_ACK, player_id & 0xFF]))


# ==========================================================================
# Game Simulation (Server-Authoritative)
# ==========================================================================


def _disasteroid_radius(size: int) -> int:
    """Return collision radius for a disasteroid size."""
    if size == DISASTEROID_SIZE_SMALL:
        return DISASTEROID_RADIUS_SMALL
    elif size == DISASTEROID_SIZE_MEDIUM:
        return DISASTEROID_RADIUS_MEDIUM
    else:
        return DISASTEROID_RADIUS_LARGE


def _to_fixed(val: float) -> int:
    """Convert float to jo_fixed (16.16)."""
    return int(val * FIXED_SCALE)


def _from_fixed(val: int) -> float:
    """Convert jo_fixed to float."""
    return val / FIXED_SCALE


def _circle_collision(x1: int, y1: int, r1: int,
                      x2: int, y2: int, r2: int) -> bool:
    """Check circle collision using integer screen coordinates."""
    dx = x2 - x1
    dy = y2 - y1
    dr = r1 + r2
    return (dx * dx + dy * dy) < (dr * dr)


# Input bit layout (DNET_INPUT_*) — used by bot AI.
INPUT_UP, INPUT_DOWN, INPUT_LEFT, INPUT_RIGHT = 1, 2, 4, 8
INPUT_A, INPUT_B, INPUT_C = 16, 32, 64

# NOTE: Server-side input prediction was attempted for v1.1.3 but DEFERRED.
# After tracing the actual code (rot is plain integer degrees, not SGL
# 16-bit modular), the planned prediction needs more rigorous physics
# parity testing against a Saturn-instrumented build before it can ship
# safely. See project memory.


def _wrap_coord(val: int, lo: int, hi: int) -> int:
    """Modulo-style wrap for jo_fixed coordinates within screen bounds.

    Preserves overshoot when crossing the edge. Used by the LERP-mode tick
    path to keep behavior byte-for-byte identical to v1.0.0/v1.1.0 LERP.
    """
    lo_f = lo * FIXED_SCALE
    hi_f = hi * FIXED_SCALE
    span = hi_f - lo_f
    if val > hi_f:
        val -= span
    elif val < lo_f:
        val += span
    return val


def _wrap_coord_saturn(val: int, lo: int, hi: int) -> int:
    """Edge-snap wrap matching the Saturn client's `boundGameplayObject`
    in objects/objects.c. When a coord crosses an edge, it's clamped to
    the OPPOSITE edge — overshoot is discarded.

    This matches what the Saturn renders, so server-side asteroid /
    player positions stay aligned with the client's view across many
    wrap events. Used by the RING-mode tick path.
    """
    lo_f = lo * FIXED_SCALE
    hi_f = hi * FIXED_SCALE
    if val < lo_f:
        return hi_f
    if val > hi_f:
        return lo_f
    return val


class GameSimulation:
    """Server-side asteroid/collision management."""

    TICK_RATE = 20  # Server ticks per second (vs 60fps on Saturn)
    TICK_RATIO = 3  # 60/20 — Saturn frames per server tick

    def __init__(self, game_type: int, num_lives: int, num_players: int):
        self.asteroids = [None] * MAX_DISASTEROIDS  # None or dict
        self.players = {}    # player_id -> {x,y,alive,invuln_frames,lives,...}
        self.wave = 0
        self.spawn_countdown = 0
        self.game_type = game_type
        self.num_lives = num_lives
        self.num_players = num_players
        self.game_over = False
        self.scores = {}  # player_id -> int (asteroid kill count)

    def init_player(self, player_id: int):
        """Register a player for collision tracking."""
        self.players[player_id] = {
            "x": 0, "y": 0,
            "dx": 0, "dy": 0,
            "alive": True,
            "invuln_frames": INVULNERABILITY_TIMER,
            "lives": self.num_lives,
            "respawn_frames": 0,
            # Phase 4 lag-comp: ring of recent (frame, x, y, dx, dy) so
            # PvP hit reports carrying a firer_frame can be compared
            # against the victim's authoritative pose at that moment.
            "history": collections.deque(maxlen=SERVER_SNAP_RING_DEPTH),
        }

    def update_player_pos(self, player_id: int, x: int, y: int,
                          dx: int, dy: int, flags: int,
                          server_frame: int = 0):
        """Update player position and velocity from SHIP_STATE.
        `server_frame` is appended to the history ring; default 0 keeps
        legacy callers working without behavior change."""
        if player_id not in self.players:
            return
        p = self.players[player_id]
        p["x"] = x
        p["y"] = y
        p["dx"] = dx
        p["dy"] = dy
        p["alive"] = bool(flags & 0x01)
        if flags & 0x02:
            p["invuln_frames"] = max(p["invuln_frames"], 1)
        # Push to history (Phase 4). Used by lookup_player_at() during
        # PvP hit lag-compensation.
        p["history"].append((server_frame & 0xFFFF, x, y, dx, dy))

    def lookup_player_at(self, player_id: int, frame: int):
        """Return the player's pose closest to `frame` from history, or
        None if the ring is empty. Searches for an exact frame match
        first (typical case — frames arrive in order); falls back to
        the closest neighbor by signed delta. Returns (x, y, dx, dy)."""
        p = self.players.get(player_id)
        if not p or not p["history"]:
            return None
        target = frame & 0xFFFF
        best = None
        best_delta = 0xFFFF
        for entry in p["history"]:
            ef, ex, ey, edx, edy = entry
            # Signed 16-bit distance handles wrap.
            d = (target - ef) & 0xFFFF
            if d > 0x8000:
                d = 0x10000 - d
            if best is None or d < best_delta:
                best = (ex, ey, edx, edy)
                best_delta = d
                if d == 0:
                    break
        return best

    def start_wave(self, speed_scale: float = 1.0) -> tuple:
        """Generate a new wave of asteroids.
        Returns (wave_num, asteroid_data_list, player_spawn_list)."""
        self.wave += 1
        if self.wave > MAX_WAVE:
            self.wave = MAX_WAVE

        # Clear all asteroids
        self.asteroids = [None] * MAX_DISASTEROIDS

        # Calculate number of asteroids for this wave
        if self.game_type == GAME_TYPE_COOP:
            count = 3 + self.wave // 2
        else:
            count = 2 + self.wave // 2
        count = min(count, MAX_DISASTEROIDS)

        speed_increment = 0x100 * self.wave  # matches getSpeedIncrementOfDisasteroids

        asteroid_data = []
        for i in range(count):
            a = self._randomize_asteroid(speed_increment, speed_scale)
            self.asteroids[i] = a
            asteroid_data.append((a["x"], a["y"], a["dx"], a["dy"],
                                  a["size"], a["type"]))

        self.spawn_countdown = DISASTEROID_SPAWN_TIMER

        # Generate player spawn data
        alive_ids = [pid for pid, p in self.players.items() if p["alive"]]
        num_alive = len(alive_ids)
        player_spawns = []
        if num_alive > 0:
            delta = 360 // num_alive
            start = random.randint(0, 11) * 30
            for idx, pid in enumerate(alive_ids):
                angle = (delta * idx + start)
                invuln = INVULNERABILITY_TIMER + random.randint(1, 16)
                player_spawns.append((pid, angle, invuln))
                # Reset player invuln
                self.players[pid]["invuln_frames"] = invuln
                self.players[pid]["respawn_frames"] = 0

        return (self.wave, asteroid_data, player_spawns)

    def _randomize_asteroid(self, speed_increment: int,
                             speed_scale: float = 1.0) -> dict:
        """Generate random asteroid data matching randomizeDisasteroid()."""
        angle = random.randint(1, 360)

        if self.game_type == GAME_TYPE_COOP:
            v_radius = _to_fixed(random.randint(1, 20) + 90)
            h_radius = _to_fixed(random.randint(1, 30) + 120)
            angle += 7
        else:
            v_radius = _to_fixed(40)
            h_radius = _to_fixed(40)

        # Position in fixed-point (h_radius/v_radius are already fixed-point)
        rad = math.radians(angle)
        x = int(h_radius * math.cos(rad))
        y = int(v_radius * math.sin(rad))

        # Velocities: jo_random(4) returns 1..4, minus 2 -> range -1..2
        dx = _to_fixed(random.randint(1, 4) - 2)
        dy = _to_fixed(random.randint(1, 4) - 2)

        # Apply speed increment
        if dx > 0:
            dx += speed_increment
        elif dx < 0:
            dx -= speed_increment

        if dy > 0:
            dy += speed_increment
        elif dy < 0:
            dy -= speed_increment

        # Minimum speed enforcement
        quarter = _to_fixed(0.25)
        if dx >= 0 and dx < quarter:
            dx += quarter
        elif dx < 0 and dx > -quarter:
            dx -= quarter

        if dy >= 0 and dy < quarter:
            dy += quarter
        elif dy < 0 and dy > -quarter:
            dy -= quarter

        # v1.1.3 — apply admin-tunable speed scale AFTER the minimum-speed
        # floor and direction-wise increment, so a scale of 0.5 still
        # gives every asteroid some motion (we don't end up with stuck
        # asteroids from rounding to zero). Clamped to a sane range by
        # the validator (0.25..2.0).
        if speed_scale != 1.0:
            dx = int(dx * speed_scale)
            dy = int(dy * speed_scale)

        size = random.randint(0, DISASTEROID_SIZE_MAX - 1)
        atype = random.randint(0, NUM_DISASTEROID_VARIATIONS - 1)

        return {
            "x": x, "y": y, "dx": dx, "dy": dy,
            "size": size, "type": atype, "alive": True,
        }

    def tick(self, wrap_saturn: bool = False,
              speed_scale: float = 1.0,
              ship_radius_bonus: int = 0) -> list:
        """Run one server tick (~50ms). Returns list of events to broadcast.

        Runs TICK_RATIO sub-steps per tick (one per Saturn frame) so that
        collision detection effectively operates at 60Hz, preventing fast
        objects from tunneling through each other.

        `wrap_saturn`: when True (RING + asteroid_sync_correct), use
        `_wrap_coord_saturn` (edge-snap, matches Saturn) for both player
        and asteroid wrap. When False (LERP path), use legacy `_wrap_coord`
        (modulo-style, byte-for-byte v1.0.0/v1.1.0 LERP behavior).
        """
        events = []
        wrap_fn = _wrap_coord_saturn if wrap_saturn else _wrap_coord

        if self.game_over:
            return events

        # Decrement spawn countdown (adjust for tick ratio)
        if self.spawn_countdown > 0:
            self.spawn_countdown -= self.TICK_RATIO
            if self.spawn_countdown < 0:
                self.spawn_countdown = 0
            return events  # Don't move asteroids during countdown

        # Decrement player invulnerability/respawn (adjust for tick ratio)
        for pid, p in self.players.items():
            if p["invuln_frames"] > 0:
                p["invuln_frames"] -= self.TICK_RATIO
                if p["invuln_frames"] < 0:
                    p["invuln_frames"] = 0
            if p["respawn_frames"] > 0:
                p["respawn_frames"] -= self.TICK_RATIO
                if p["respawn_frames"] < 0:
                    p["respawn_frames"] = 0

        # Sub-step loop: advance by 1 frame and check collisions each step
        killed_this_tick = set()  # track players already killed
        for _step in range(self.TICK_RATIO):
            # Advance player positions by 1 frame
            for pid, p in self.players.items():
                if not p["alive"] or p["respawn_frames"] > 0:
                    continue
                p["x"] += p["dx"]
                p["y"] += p["dy"]
                p["x"] = wrap_fn(p["x"], SCREEN_MIN_X, SCREEN_MAX_X)
                p["y"] = wrap_fn(p["y"], SCREEN_MIN_Y, SCREEN_MAX_Y)

            # Advance asteroid positions by 1 frame
            for i, a in enumerate(self.asteroids):
                if a is None or not a["alive"]:
                    continue
                a["x"] += a["dx"]
                a["y"] += a["dy"]
                a["x"] = wrap_fn(a["x"], SCREEN_MIN_X, SCREEN_MAX_X)
                a["y"] = wrap_fn(a["y"], SCREEN_MIN_Y, SCREEN_MAX_Y)

            # Check ship-asteroid collisions at this sub-step
            for pid, p in self.players.items():
                if pid in killed_this_tick:
                    continue
                if not p["alive"] or p["invuln_frames"] > 0 or p["respawn_frames"] > 0:
                    continue
                px = int(_from_fixed(p["x"]))
                py = int(_from_fixed(p["y"]))
                for i, a in enumerate(self.asteroids):
                    if a is None or not a["alive"]:
                        continue
                    ax = int(_from_fixed(a["x"]))
                    ay = int(_from_fixed(a["y"]))
                    ar = _disasteroid_radius(a["size"]) + 2
                    # v1.1.3: ship-vs-asteroid radius is admin-tunable.
                    # Negative bonus shrinks the kill zone, reducing
                    # ghost-kills from stale server-side player positions.
                    ship_r = PLAYER_SHIP_RADIUS + ship_radius_bonus
                    if ship_r < 1:
                        ship_r = 1
                    if _circle_collision(ax, ay, ar, px, py, ship_r):
                        kill_evt = self._kill_player(pid, i)
                        if kill_evt:
                            events.append(kill_evt)
                        destroy_evt = self._destroy_asteroid(i, 0xFF,
                                                              speed_scale)
                        if destroy_evt:
                            events.append(destroy_evt)
                        killed_this_tick.add(pid)
                        break  # One collision per player per sub-step

        # Check wave over
        wave_over = not any(a is not None and a["alive"] for a in self.asteroids)
        if wave_over:
            events.append(("wave_over",))

        # Check game over — but NOT on the same tick as wave_over,
        # because _start_new_wave will respawn players
        if not wave_over:
            go_evt = self._check_game_over()
            if go_evt:
                events.append(go_evt)

        return events

    def handle_asteroid_hit(self, slot: int, scorer_id: int,
                              speed_scale: float = 1.0):
        """Handle ASTEROID_HIT from a client. Returns destroy event or None."""
        if slot < 0 or slot >= MAX_DISASTEROIDS:
            return None
        a = self.asteroids[slot]
        if a is None or not a["alive"]:
            return None
        result = self._destroy_asteroid(slot, scorer_id, speed_scale)
        if result is not None and scorer_id != 0xFF and scorer_id < MAX_PLAYERS:
            self.scores[scorer_id] = self.scores.get(scorer_id, 0) + 1
        return result

    def _destroy_asteroid(self, slot: int, scorer_id: int,
                           speed_scale: float = 1.0):
        """Destroy asteroid, generate split data. Returns event tuple."""
        a = self.asteroids[slot]
        if a is None or not a["alive"]:
            return None

        children = []
        if a["size"] > DISASTEROID_SIZE_SMALL:
            # Split into 2 children
            for _ in range(2):
                child_slot = self._find_free_slot()
                if child_slot < 0:
                    break
                child = self._randomize_asteroid(0x100 * self.wave,
                                                  speed_scale)
                child["x"] = a["x"]
                child["y"] = a["y"]
                child["size"] = a["size"] - 1
                self.asteroids[child_slot] = child
                children.append((child_slot, child["dx"], child["dy"],
                                 child["size"], child["type"]))

        a["alive"] = False
        return ("asteroid_destroy", slot, scorer_id, children)

    def _kill_player(self, player_id: int, asteroid_slot: int):
        """Handle player death from ship-asteroid collision."""
        p = self.players.get(player_id)
        if not p or not p["alive"]:
            return None

        p["lives"] -= 1
        if p["lives"] <= 0:
            p["alive"] = False
            p["dx"] = 0
            p["dy"] = 0
            return ("player_kill", player_id, 0, 0,
                    0, 0)

        # Respawn with server-generated data
        angle = random.randint(0, 11) * 30
        invuln = INVULNERABILITY_TIMER + random.randint(1, 16)
        respawn = RESPAWN_TIMER + random.randint(1, 16)
        p["invuln_frames"] = invuln
        p["respawn_frames"] = respawn

        # Update server-side position to match respawn point
        rad = math.radians(angle)
        if self.game_type == GAME_TYPE_VERSUS:
            h_radius = 120
            v_radius = 80
        else:
            h_radius = 40
            v_radius = 40
        p["x"] = int(h_radius * math.cos(rad) * FIXED_SCALE)
        p["y"] = int(v_radius * math.sin(rad) * FIXED_SCALE)
        p["dx"] = 0
        p["dy"] = 0

        return ("player_kill", player_id, p["lives"], angle,
                invuln, respawn)

    def _check_game_over(self):
        """Check if game should end."""
        if self.game_type == GAME_TYPE_COOP:
            # All players dead
            if not any(p["alive"] for p in self.players.values()):
                self.game_over = True
                return ("game_over", 0xFF)
        else:
            # Versus: one or zero players left
            alive = [pid for pid, p in self.players.items() if p["alive"]]
            if len(alive) <= 1:
                self.game_over = True
                winner = alive[0] if alive else 0xFF
                return ("game_over", winner)
        return None

    def _find_free_slot(self) -> int:
        """Find first free asteroid slot."""
        for i, a in enumerate(self.asteroids):
            if a is None or not a["alive"]:
                return i
        return -1


# ==========================================================================
# Input Bitmask (matches disasteroids_protocol.h)
# ==========================================================================

INPUT_UP    = 1 << 0
INPUT_DOWN  = 1 << 1
INPUT_LEFT  = 1 << 2
INPUT_RIGHT = 1 << 3
INPUT_A     = 1 << 4  # shoot
INPUT_B     = 1 << 5  # thrust
INPUT_C     = 1 << 6  # shoot
INPUT_X     = 1 << 7  # change color


# ==========================================================================
# Bot AI (reused from test_client.py)
# ==========================================================================


class BotAI:
    """Bot AI with difficulty-adjusted behavior.
    Easy: mostly cruises, shoots rarely.
    Medium: balanced attack/cruise/evade.
    Hard: aggressive attack, rapid fire, tight weaving.
    """

    def __init__(self, difficulty=BOT_DIFFICULTY_MEDIUM):
        self.frame = 0
        self.difficulty = difficulty
        self.state = "cruise"
        self.state_timer = 0
        self._pick_new_state()

    def _pick_new_state(self):
        if self.difficulty == BOT_DIFFICULTY_EASY:
            self.state = random.choice([
                "cruise", "cruise", "cruise",
                "hunt_left", "hunt_right", "evade",
            ])
            self.state_timer = random.randint(90, 240)
        elif self.difficulty == BOT_DIFFICULTY_HARD:
            self.state = random.choice([
                "attack", "attack", "attack", "attack",
                "hunt_left", "hunt_right",
                "strafe_attack", "strafe_attack",
            ])
            self.state_timer = random.randint(40, 120)
        else:  # Medium
            self.state = random.choice([
                "attack", "attack", "attack",
                "hunt_left", "hunt_right", "cruise",
                "strafe_attack", "evade",
            ])
            self.state_timer = random.randint(60, 180)

    def tick(self):
        """Returns input bits for this frame."""
        self.frame += 1
        self.state_timer -= 1
        if self.state_timer <= 0:
            self._pick_new_state()

        bits = 0

        # Difficulty-scaled fire rate divisor (lower = faster shooting)
        fire_div = {
            BOT_DIFFICULTY_EASY: 12,
            BOT_DIFFICULTY_MEDIUM: 6,
            BOT_DIFFICULTY_HARD: 4,
        }.get(self.difficulty, 6)

        if self.state == "attack":
            bits |= INPUT_UP
            if self.frame % fire_div < (fire_div // 2):
                bits |= INPUT_A
            if self.frame % 40 < 20:
                bits |= INPUT_LEFT
            else:
                bits |= INPUT_RIGHT
        elif self.state == "hunt_left":
            bits |= INPUT_LEFT | INPUT_UP
            if self.frame % fire_div < (fire_div // 2):
                bits |= INPUT_A
        elif self.state == "hunt_right":
            bits |= INPUT_RIGHT | INPUT_UP
            if self.frame % fire_div < (fire_div // 2):
                bits |= INPUT_C
        elif self.state == "cruise":
            bits |= INPUT_UP
            if self.frame % (fire_div * 2) < fire_div:
                bits |= INPUT_A
            if self.frame % 80 < 15:
                bits |= INPUT_LEFT
        elif self.state == "strafe_attack":
            cycle = self.frame % 40
            if cycle < 15:
                bits |= INPUT_LEFT | INPUT_A
            elif cycle < 30:
                bits |= INPUT_UP | INPUT_A
            else:
                bits |= INPUT_RIGHT | INPUT_C
        elif self.state == "evade":
            bits |= INPUT_UP
            cycle = self.frame % 20
            if cycle < 7:
                bits |= INPUT_LEFT
            elif cycle < 14:
                bits |= INPUT_RIGHT
            if self.frame % (fire_div + 2) < (fire_div // 2):
                bits |= INPUT_C
        return bits


# ==========================================================================
# Bot Player
# ==========================================================================


class BotPlayer:
    """Virtual bot player — no socket, runs inside server."""

    def __init__(self, name: str, bot_id: int,
                 difficulty: int = BOT_DIFFICULTY_DEFAULT):
        self.name = name
        self.bot_id = bot_id
        self.difficulty = difficulty
        self.ready = True  # Auto-ready
        self.in_game = False
        self.alive = True
        self.game_player_id = 0
        # Bot physics (jo_fixed format)
        self.x = 0
        self.y = 0
        self.dx = 0
        self.dy = 0
        self.rot = 0
        self.invuln = 0
        self.respawn = 0
        # AI
        self.ai = BotAI(difficulty)
        self.last_sent_bits = -1
        self.force_send_counter = 0
        # Tuning: SHIP_SYNC rate throttle
        self.sync_tick_counter = 0

    def update_physics(self, bits: int):
        """Simple physics matching Saturn's ship model."""
        # Respawning — just count down, don't move
        if self.respawn > 0:
            self.respawn -= 1
            if self.invuln > 0:
                self.invuln -= 1
            return

        if bits & (INPUT_UP | INPUT_B):
            rad = math.radians(self.rot)
            self.dx += int(math.sin(rad) * FIXED_SCALE)
            self.dy -= int(math.cos(rad) * FIXED_SCALE)
        if bits & INPUT_LEFT:
            self.rot -= 7
        if bits & INPUT_RIGHT:
            self.rot += 7
        # Clamp speed
        max_spd = 2 * FIXED_SCALE
        self.dx = max(-max_spd, min(max_spd, self.dx))
        self.dy = max(-max_spd, min(max_spd, self.dy))
        # Move
        self.x += self.dx
        self.y += self.dy
        # Wrap
        self.x = _wrap_coord(self.x, SCREEN_MIN_X, SCREEN_MAX_X)
        self.y = _wrap_coord(self.y, SCREEN_MIN_Y, SCREEN_MAX_Y)
        # Decrement invuln
        if self.invuln > 0:
            self.invuln -= 1

    def reset_for_game(self):
        """Reset bot state for a new game."""
        self.x = 0
        self.y = 0
        self.dx = 0
        self.dy = 0
        self.rot = 0
        self.invuln = INVULNERABILITY_TIMER
        self.respawn = 0
        self.alive = True
        self.in_game = True
        self.ai = BotAI(self.difficulty)
        self.last_sent_bits = -1
        self.force_send_counter = 0
        self.sync_tick_counter = 0


# ==========================================================================
# Client Info
# ==========================================================================


class ClientInfo:
    def __init__(self, sock: socket.socket, address: tuple):
        self.socket = sock
        self.address = address
        self.uuid = ""
        self.username = ""
        self.user_id = 0
        self.authenticated = False
        self.recv_buffer = b""
        self.last_activity = time.time()
        # Game state
        self.ready = False
        self.in_game = False
        self.alive = True
        self.game_player_id = 0  # Assigned during GAME_START
        self.local_player_ids = []  # Additional local player IDs (dual controller)
        self.local_player_names = []  # Names for additional local players
        # Tuning bookkeeping (SHIP_SYNC relay throttle)
        self.ship_sync_relay_counter = 0
        self.last_ship_sync_relay_tick = 0
        # Phase 3: capability handshake. Old clients (pre-1.1.0) never
        # send CLIENT_CAPS, so we default to "no advanced caps" — they
        # always receive raw 22-byte SHIP_SYNC and ignore SET_SYNC_MODE.
        self.supports_ring = False
        # v1.1.3: client supports the fixed 13-byte SHIP_SYNC_Q_V2 (0xB3)
        # with raw int16 rot. Clients announcing only CAP_SUPPORTS_RING
        # (pre-1.1.3) get the legacy 12-byte 0xB1 form.
        self.supports_ring_v2 = False
        # Last sync mode this client was told about. None = never told.
        # Lets us avoid spamming SET_SYNC_MODE on every tick.
        self.last_sent_sync_mode = None
        # Egress telemetry (rolling window)
        self._egress_bytes = 0
        self._egress_window_start = time.time()
        self._egress_rate = 0  # bytes/sec, updated once per window

    def send_raw(self, data: bytes) -> bool:
        try:
            self.socket.sendall(data)
        except OSError:
            return False
        # Count egress and roll the window
        self._egress_bytes += len(data)
        now = time.time()
        elapsed = now - self._egress_window_start
        if elapsed >= 1.0:
            self._egress_rate = int(self._egress_bytes / elapsed)
            self._egress_bytes = 0
            self._egress_window_start = now
        return True


# ==========================================================================
# Disasteroids Server
# ==========================================================================


class DisasteroidsServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 4822,
                 num_bots: int = 0,
                 admin_port: int = 0,
                 admin_user: str = "admin",
                 admin_password: str = "disast2026"):
        self.host = host
        self.port = port
        self.clients: dict = {}  # {socket: ClientInfo}
        self.uuid_map: dict = {}  # {uuid: username}
        self.server_socket = None
        self._running = False

        # Bridge auth
        self.pending_auth: dict = {}  # {socket: {"deadline": float, "buf": bytes}}
        self.authenticated_bridges: set = set()

        # Game state
        self.game_active = False
        self.game_seed = 0
        self.game_paused = False
        self.game_type = GAME_TYPE_VERSUS
        self.num_lives = 3
        self.sim = None  # GameSimulation instance

        # Bots (can be added via CLI or by players in lobby)
        self.bots: list = []  # List of BotPlayer instances
        for i in range(num_bots):
            name = BOT_NAMES[i % len(BOT_NAMES)]
            self.bots.append(BotPlayer(name, i, BOT_DIFFICULTY_DEFAULT))

        # Leaderboard persistence
        self.leaderboard = {}  # {name: {"wins": N, "best_score": N, "games_played": N}}
        self._leaderboard_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leaderboard.json")
        self._load_leaderboard()

        # Delta compression: track last relayed input per player_id
        self.last_relayed_input = {}   # {player_id: input_bits}
        self.relay_cooldown = {}       # {player_id: frames_since_relay}

        # Tick timer for game simulation
        self._last_tick = 0.0
        self._tick_interval = 1.0 / GameSimulation.TICK_RATE
        self._tick_counter = 0  # Monotonic server tick, used for keepalive bookkeeping

        # --- Runtime-tunable bandwidth knobs (see admin /api/tuning) ---
        # All defaults match pre-tuning behavior exactly (PASSTHROUGH).
        # `sync_mode` is orthogonal to the bandwidth presets: LERP keeps
        # current single-snapshot lerp+extrap behavior, RING activates the
        # snapshot-ring + interp/extrap engine on clients that support it
        # (Phase 3 client opt-in via SET_SYNC_MODE message). Defaulting
        # to LERP preserves byte-for-byte backward compatibility.
        self.tuning = {
            "preset": "PASSTHROUGH",
            "ship_sync_decimate": 1,           # relay every Nth SHIP_STATE per source
            "ship_sync_skip_stationary": False,
            "stationary_vel_threshold": 2,     # |dx|+|dy| below this = stationary
            "ship_sync_keepalive_ticks": 60,   # force relay every N server ticks
            "bot_sync_every_n_ticks": 1,       # 1=20Hz, 2=10Hz, 3≈7Hz, 4=5Hz
            # Default flipped to RING in v1.1.2 after the Saturn-side
            # jitter fix landed. Old binaries (pre-1.1.0) still get LERP
            # automatically because they don't announce CLIENT_CAPS — see
            # _send_sync_mode_to() which forces LERP for non-ring-capable
            # clients. New v1.1.0+ binaries activate RING on connect.
            "sync_mode": "RING",

            # v1.1.1 — asteroid drift correction. Only takes effect when
            # sync_mode==RING. Server enables: (a) wall-clock catch-up loop
            # in the tick scheduler, (b) Saturn-matching edge-snap wrap, and
            # (c) periodic ASTEROID_SYNC broadcasts. When False, server runs
            # the v1.1.0 LERP-style asteroid path even if sync_mode==RING.
            "asteroid_sync_correct": True,
            # v1.1.3 — server-side ship-vs-asteroid leniency. Server adds
            # this to PLAYER_SHIP_RADIUS (5) when checking collisions in
            # tick(). Negative = stricter (server requires more overlap
            # before killing), reducing "ghost kills" caused by the
            # server's stale 4-frame-old player position. Positive =
            # more aggressive (kills on near-misses). Range -3..+3
            # enforced by validator. Default 0 = pre-1.1.3 behavior.
            "ship_collision_radius_bonus": 0,
            # v1.1.3 — game-feel: scale all new asteroid spawn velocities
            # by this factor. 1.0 = original speed; 0.5 = half-speed asteroids
            # (gentler, more time to react); 1.5 = harder. Only affects
            # asteroids spawned AFTER the toggle change — in-flight asteroids
            # keep their original velocity. Range 0.25..2.0 enforced by
            # validator.
            "asteroid_speed_scale": 1.0,
        }
        self._tuning_presets = {
            "PASSTHROUGH": {
                "ship_sync_decimate": 1,
                "ship_sync_skip_stationary": False,
                "stationary_vel_threshold": 2,
                "ship_sync_keepalive_ticks": 60,
                "bot_sync_every_n_ticks": 1,
            },
            "LIGHT": {
                "ship_sync_decimate": 1,
                "ship_sync_skip_stationary": True,
                "stationary_vel_threshold": 2,
                "ship_sync_keepalive_ticks": 60,
                "bot_sync_every_n_ticks": 2,
            },
            "MODERATE": {
                "ship_sync_decimate": 2,
                "ship_sync_skip_stationary": True,
                "stationary_vel_threshold": 2,
                "ship_sync_keepalive_ticks": 45,
                "bot_sync_every_n_ticks": 2,
            },
            "AGGRESSIVE": {
                "ship_sync_decimate": 2,
                "ship_sync_skip_stationary": True,
                "stationary_vel_threshold": 3,
                "ship_sync_keepalive_ticks": 30,
                "bot_sync_every_n_ticks": 3,
            },
        }
        # sync_mode is independent of the bandwidth presets — switching
        # bandwidth preset does NOT touch sync_mode. Keep it as its own
        # axis so admins can mix-and-match (e.g., AGGRESSIVE + RING).
        # AUTO mode tracking
        self._auto_mode = False          # when True, preset is re-selected on count change
        self._auto_last_downgrade = 0.0  # timestamp of last candidate-downgrade (for hysteresis)
        self._auto_current = "PASSTHROUGH"
        self._AUTO_DOWNGRADE_HOLD = 10.0  # seconds of stable lower count before easing preset

        # Per-client egress telemetry (rolling 1-second window)
        self._egress_window = 1.0

        # Admin HTTP portal
        self._admin_port = admin_port
        self._admin_user = admin_user
        self._admin_password = admin_password
        self._admin_command_queue = queue.Queue()
        self._admin_httpd = None
        self._admin_thread = None
        self._start_time = time.time()
        self._join_history = []
        self._join_history_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "join_history.json")
        self._load_join_history()

    def _load_leaderboard(self):
        """Load leaderboard from disk."""
        try:
            if os.path.exists(self._leaderboard_path):
                with open(self._leaderboard_path, "r") as f:
                    data = json.load(f)
                self.leaderboard = data.get("players", {})
                log.info("Loaded leaderboard: %d players", len(self.leaderboard))
        except Exception as e:
            log.warning("Failed to load leaderboard: %s", e)
            self.leaderboard = {}

    def _save_leaderboard(self):
        """Save leaderboard to disk."""
        try:
            with open(self._leaderboard_path, "w") as f:
                json.dump({"players": self.leaderboard}, f, indent=2)
        except Exception as e:
            log.warning("Failed to save leaderboard: %s", e)

    def _update_leaderboard(self, winner_id):
        """Update leaderboard after a game ends."""
        if not self.sim:
            return

        # Build name -> score mapping from game roster
        game_players = {}  # name -> score
        for c in self.clients.values():
            if c.in_game and c.game_player_id is not None:
                score = self.sim.scores.get(c.game_player_id, 0)
                game_players[c.username] = score
                # Also count local players
                for i, lp_id in enumerate(c.local_player_ids):
                    ln = c.local_player_names[i] if i < len(c.local_player_names) else "P2"
                    game_players[ln] = self.sim.scores.get(lp_id, 0)
        for bot in self.bots:
            if bot.in_game and bot.game_player_id is not None:
                game_players[bot.name] = self.sim.scores.get(bot.game_player_id, 0)

        # Find winner name
        winner_name = None
        if winner_id != 0xFF:
            for c in self.clients.values():
                if c.game_player_id == winner_id:
                    winner_name = c.username
                    break
                if winner_id in c.local_player_ids:
                    idx = c.local_player_ids.index(winner_id)
                    if idx < len(c.local_player_names):
                        winner_name = c.local_player_names[idx]
                    break
            if not winner_name:
                for bot in self.bots:
                    if bot.game_player_id == winner_id:
                        winner_name = bot.name
                        break

        # Update each participant
        for name, score in game_players.items():
            if name not in self.leaderboard:
                self.leaderboard[name] = {"wins": 0, "best_score": 0, "games_played": 0}
            entry = self.leaderboard[name]
            entry["games_played"] += 1
            if score > entry["best_score"]:
                entry["best_score"] = score
            if winner_name and name == winner_name:
                entry["wins"] += 1

        self._save_leaderboard()
        log.info("Leaderboard updated: %d total players", len(self.leaderboard))

    def _get_leaderboard_top10(self) -> list:
        """Get top 10 leaderboard entries sorted by wins (tiebreak: best_score)."""
        entries = []
        for name, data in self.leaderboard.items():
            entries.append({
                "name": name,
                "wins": data["wins"],
                "best_score": data["best_score"],
                "games_played": data["games_played"],
            })
        entries.sort(key=lambda e: (e["wins"], e["best_score"]), reverse=True)
        return entries[:10]

    def _send_leaderboard_to_client(self, client):
        """Send current leaderboard to a specific client."""
        entries = self._get_leaderboard_top10()
        msg = build_leaderboard_data(entries)
        client.send_raw(msg)

    def _broadcast_leaderboard(self):
        """Send leaderboard to all authenticated clients."""
        entries = self._get_leaderboard_top10()
        msg = build_leaderboard_data(entries)
        for c in self.clients.values():
            if c.authenticated:
                c.send_raw(msg)

    def _next_user_id(self) -> int:
        """Find lowest available user_id (1-based), recycling disconnected IDs."""
        used = {c.user_id for c in self.clients.values() if c.user_id > 0}
        uid = 1
        while uid in used:
            uid += 1
        return uid

    def start(self):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(MAX_BRIDGES + 5)
        self.server_socket.setblocking(False)

        log.info("Disasteroids Server listening on %s:%d", self.host, self.port)
        self._running = True
        self._start_admin_server()
        self._run()

    def _run(self):
        while self._running:
            # Build socket list for select
            read_sockets = [self.server_socket]
            read_sockets.extend(self.pending_auth.keys())
            read_sockets.extend(self.clients.keys())

            # Use short timeout when game is active for tick processing
            timeout = self._tick_interval if self.game_active else 1.0

            try:
                readable, _, _ = select.select(read_sockets, [], [], timeout)
            except (ValueError, OSError):
                # Socket was closed, clean up
                self._cleanup_dead_sockets()
                continue

            now = time.time()

            for sock in readable:
                if sock is self.server_socket:
                    self._accept_connection()
                elif sock in self.pending_auth:
                    self._handle_bridge_auth(sock, now)
                elif sock in self.clients:
                    self._handle_client_data(sock)

            # Game simulation tick.
            #
            # Two scheduler paths gated by the v1.1.1 asteroid-correction
            # toggle:
            #
            #   LERP / asteroid_sync_correct OFF — single-shot scheduler
            #     (byte-for-byte identical to v1.0.0 / v1.1.0 LERP). If the
            #     server was busy and missed a tick window, that tick is
            #     silently dropped — server's asteroid simulation falls
            #     behind the Saturn's deterministic local sim, which is
            #     the documented v1.1.0 LERP behavior.
            #
            #   RING + asteroid_sync_correct ON — bounded catch-up loop:
            #     run every missed tick (up to 10) so the server's
            #     wall-clock asteroid simulation stays aligned with the
            #     Saturn's per-frame advance. Cap prevents a multi-second
            #     pause (debugger / GC / swap) from triggering a huge
            #     burst when execution resumes.
            if self.game_active and self.sim and not self.game_paused:
                use_catchup = (
                    self.tuning.get("sync_mode", "LERP") == "RING" and
                    self.tuning.get("asteroid_sync_correct", True))
                if use_catchup:
                    catchup_cap = 10
                    ran = 0
                    while (now - self._last_tick >= self._tick_interval
                           and ran < catchup_cap):
                        self._game_tick()
                        self._last_tick += self._tick_interval
                        ran += 1
                    # If we hit the cap, fast-forward last_tick so we don't
                    # keep bursting on every iteration of the main loop.
                    if (ran >= catchup_cap and
                            now - self._last_tick >= self._tick_interval):
                        self._last_tick = now
                else:
                    if now - self._last_tick >= self._tick_interval:
                        self._last_tick = now
                        self._game_tick()

            # Periodic tasks
            self._check_timeouts(now)
            self._process_admin_commands()

    def _accept_connection(self):
        try:
            client_sock, addr = self.server_socket.accept()
            client_sock.setblocking(False)
        except OSError:
            return

        if len(self.authenticated_bridges) >= MAX_BRIDGES:
            log.warning("Max bridges reached, rejecting %s:%d", addr[0], addr[1])
            client_sock.close()
            return

        log.info("New connection from %s:%d", addr[0], addr[1])
        self.pending_auth[client_sock] = {
            "deadline": time.time() + AUTH_TIMEOUT,
            "buf": b"",
            "address": addr,
        }

    def _handle_bridge_auth(self, sock, now: float):
        info = self.pending_auth[sock]

        if now > info["deadline"]:
            log.warning("Auth timeout from %s", info["address"])
            sock.close()
            del self.pending_auth[sock]
            return

        try:
            data = sock.recv(256)
        except (BlockingIOError, OSError):
            return

        if not data:
            sock.close()
            del self.pending_auth[sock]
            return

        info["buf"] += data

        # Check for AUTH_MAGIC + len + secret
        buf = info["buf"]
        magic_len = len(AUTH_MAGIC)
        if len(buf) < magic_len:
            return

        if buf[:magic_len] != AUTH_MAGIC:
            log.warning("Invalid auth magic from %s", info["address"])
            sock.close()
            del self.pending_auth[sock]
            return

        if len(buf) < magic_len + 1:
            return

        secret_len = buf[magic_len]
        total_needed = magic_len + 1 + secret_len
        if len(buf) < total_needed:
            return

        received_secret = buf[magic_len + 1:total_needed]
        if received_secret != SHARED_SECRET:
            log.warning("Wrong shared secret from %s", info["address"])
            sock.close()
            del self.pending_auth[sock]
            return

        # Auth success
        try:
            sock.sendall(bytes([AUTH_OK]))
        except OSError:
            sock.close()
            del self.pending_auth[sock]
            return

        log.info("Bridge authenticated from %s:%d",
                 info["address"][0], info["address"][1])
        self.authenticated_bridges.add(sock)
        del self.pending_auth[sock]

        # Create client
        client = ClientInfo(sock, info["address"])
        self.clients[sock] = client

    def _handle_client_data(self, sock):
        client = self.clients.get(sock)
        if not client:
            return

        try:
            data = sock.recv(MAX_RECV_BUFFER)
        except (BlockingIOError, OSError):
            return

        if not data:
            self._remove_client(sock, "connection closed")
            return

        client.last_activity = time.time()
        client.recv_buffer += data

        # Process complete SNCP frames
        while len(client.recv_buffer) >= 2:
            payload_len = (client.recv_buffer[0] << 8) | client.recv_buffer[1]
            total = 2 + payload_len
            if payload_len == 0 or payload_len > MAX_RECV_BUFFER:
                log.warning("Invalid frame from %s, disconnecting", client.address)
                self._remove_client(sock, "invalid frame")
                return
            if len(client.recv_buffer) < total:
                break  # Wait for more data

            payload = client.recv_buffer[2:total]
            client.recv_buffer = client.recv_buffer[total:]
            self._process_message(sock, client, payload)

    def _process_message(self, sock, client: ClientInfo, payload: bytes):
        if not payload:
            return

        msg_type = payload[0]

        if msg_type == MSG_CONNECT:
            self._handle_connect(sock, client, payload)
        elif msg_type == MSG_SET_USERNAME:
            self._handle_set_username(sock, client, payload)
        elif msg_type == MSG_HEARTBEAT:
            pass  # Just updates last_activity
        elif msg_type == MSG_DISCONNECT:
            self._remove_client(sock, "disconnect requested")
        elif msg_type == DNET_MSG_READY:
            self._handle_ready(sock, client)
        elif msg_type == DNET_MSG_START_GAME_REQ:
            self._handle_start_game(sock, client)
        elif msg_type == DNET_MSG_INPUT_STATE:
            self._handle_input_state(sock, client, payload)
        elif msg_type == DNET_MSG_PAUSE_REQ:
            self._handle_pause(sock, client)
        elif msg_type == DNET_MSG_SHIP_STATE:
            self._handle_ship_state(sock, client, payload)
        elif msg_type == DNET_MSG_ASTEROID_HIT:
            self._handle_asteroid_hit(sock, client, payload)
        elif msg_type == DNET_MSG_SHIP_ASTEROID_HIT:
            self._handle_ship_asteroid_hit(sock, client, payload)
        elif msg_type == DNET_MSG_ADD_LOCAL_PLAYER:
            self._handle_add_local_player(sock, client, payload)
        elif msg_type == DNET_MSG_ADD_BOT:
            self._handle_add_bot(sock, client, payload)
        elif msg_type == DNET_MSG_REMOVE_BOT:
            self._handle_remove_bot(sock, client, payload)
        elif msg_type == DNET_MSG_REMOVE_LOCAL_PLAYER:
            self._handle_remove_local_player(sock, client)
        elif msg_type == DNET_MSG_LEADERBOARD_REQ:
            self._send_leaderboard_to_client(client)
        elif msg_type == DNET_MSG_CLIENT_CAPS:
            self._handle_client_caps(sock, client, payload)
        else:
            log.debug("Unknown message type 0x%02X from %s",
                      msg_type, client.address)

    # ------------------------------------------------------------------
    # Auth handlers
    # ------------------------------------------------------------------

    def _handle_connect(self, sock, client: ClientInfo, payload: bytes):
        client_uuid = ""
        if len(payload) > 1 + UUID_LEN - 1:
            client_uuid = payload[1:1 + UUID_LEN].decode("ascii", errors="replace").rstrip("\x00")

        if client_uuid and client_uuid in self.uuid_map:
            # Reconnecting player
            client.uuid = client_uuid
            client.username = self.uuid_map[client_uuid]
            client.user_id = self._next_user_id()
            client.authenticated = True
            log.info("Player reconnected: %s (uuid=%s..)",
                     client.username, client_uuid[:8])
            client.send_raw(build_welcome_back(
                client.user_id, client.uuid, client.username))
            self._log_join(client.username,
                           "%s:%d" % client.address, "reconnect")
            self._broadcast_lobby_state()
            self._send_leaderboard_to_client(client)
        else:
            # New player — reuse UUID if this socket already got one
            # (handles duplicate CONNECT on same connection)
            if not client.uuid:
                client.uuid = str(uuid.uuid4())
                client.user_id = self._next_user_id()
                self.uuid_map[client.uuid] = ""
            log.info("New player connected (uuid=%s..)", client.uuid[:8])
            client.send_raw(build_username_required())

    def _handle_set_username(self, sock, client: ClientInfo, payload: bytes):
        if len(payload) < 2:
            return
        name_len = payload[1]
        if len(payload) < 2 + name_len:
            return
        username = payload[2:2 + name_len].decode("utf-8", errors="replace")
        username = username[:USERNAME_MAX_LEN].strip()

        if not username:
            client.send_raw(build_username_taken())
            return

        # Check for duplicate names
        for s, c in self.clients.items():
            if s != sock and c.authenticated and c.username.lower() == username.lower():
                log.info("Username '%s' taken, rejected for user_id %d",
                         username, client.user_id)
                client.send_raw(build_username_taken())
                return

        # Check if lobby is full (count all authenticated + their locals + bots)
        lobby_slots = 0
        for c in self.clients.values():
            if c.authenticated:
                lobby_slots += 1 + len(c.local_player_names)
        lobby_slots += len(self.bots)
        if lobby_slots >= MAX_PLAYERS:
            client.send_raw(build_log("Server full (%d/%d)" % (lobby_slots, MAX_PLAYERS)))
            return

        client.username = username
        client.authenticated = True
        self.uuid_map[client.uuid] = username

        log.info("Player %d set username: %s", client.user_id, username)
        client.send_raw(build_welcome(client.user_id, client.uuid, username))
        self._log_join(username, "%s:%d" % client.address, "join")

        # Let new player know if a game is in progress
        if self.game_active:
            client.send_raw(build_log("Game in progress - wait for next round"))

        self._broadcast_lobby_state()
        self._send_leaderboard_to_client(client)

        # Announce join
        for s, c in self.clients.items():
            if s != sock and c.authenticated:
                c.send_raw(build_player_join(client.user_id, username))
                c.send_raw(build_log("%s joined!" % username))

    def _handle_add_local_player(self, sock, client: ClientInfo,
                                    payload: bytes):
        """Handle ADD_LOCAL_PLAYER: register a second local player on this connection."""
        if not client.authenticated:
            return
        if len(payload) < 2:
            return
        name_len = payload[1]
        if len(payload) < 2 + name_len:
            return
        name = payload[2:2 + name_len].decode("utf-8", errors="replace")
        name = name[:USERNAME_MAX_LEN].strip()
        if not name:
            return

        # Check duplicate names
        all_names = set()
        for c in self.clients.values():
            if c.authenticated:
                all_names.add(c.username.lower())
                for ln in c.local_player_names:
                    all_names.add(ln.lower())
        for bot in self.bots:
            all_names.add(bot.name.lower())

        if name.lower() in all_names:
            # Try appending digits
            for suffix in range(2, 10):
                candidate = name + str(suffix)
                if candidate.lower() not in all_names:
                    name = candidate
                    break

        client.local_player_names.append(name)
        # Player ID will be assigned during game start; send a provisional ACK
        # with id=0xFF (will be reassigned on GAME_START)
        ack_id = 0xFF
        client.send_raw(build_local_player_ack(ack_id))
        log.info("Player %s registered local player 2: %s",
                 client.username, name)
        self._broadcast_lobby_state()

    def _handle_remove_local_player(self, sock, client: ClientInfo):
        """Handle REMOVE_LOCAL_PLAYER: remove the second local player."""
        if not client.authenticated:
            return

        if not client.local_player_names:
            return

        removed_name = client.local_player_names.pop()
        log.info("Player %s removed local player: %s", client.username, removed_name)

        if self.game_active and self.sim and client.local_player_ids:
            # Mid-game: mark the local player as dead
            pid = client.local_player_ids.pop()
            if pid in self.sim.players:
                self.sim.players[pid]["alive"] = False
                self.sim.players[pid]["lives"] = 0
            # Notify all clients this player is dead
            kill_msg = build_player_kill(pid, 0, 0, 0, 0)
            log_msg = build_log("%s left" % removed_name)
            self._broadcast_to_game(kill_msg)
            self._broadcast_to_game(log_msg)
        elif client.local_player_ids:
            client.local_player_ids.pop()

        self._broadcast_lobby_state()

    def _handle_add_bot(self, sock, client: ClientInfo, payload: bytes):
        """Handle ADD_BOT: player requests a bot be added to the lobby."""
        if self.game_active:
            client.send_raw(build_log("Can't add bot mid-game"))
            return
        if not client.authenticated:
            return

        difficulty = payload[1] if len(payload) >= 2 else BOT_DIFFICULTY_DEFAULT
        if difficulty > 2:
            difficulty = BOT_DIFFICULTY_DEFAULT

        # Check max bots
        if len(self.bots) >= MAX_PLAYERS - 1:
            client.send_raw(build_log("Too many bots"))
            return

        # Pick next bot name
        name_idx = len(self.bots) % len(BOT_NAMES)
        bot_name = BOT_NAMES[name_idx]
        bot_id = len(self.bots)
        bot = BotPlayer(bot_name, bot_id, difficulty)
        self.bots.append(bot)

        diff_names = {0: "easy", 1: "medium", 2: "hard"}
        log.info("Bot added: %s (%s) by %s",
                 bot_name, diff_names.get(difficulty, "?"), client.username)
        self._broadcast_lobby_state()

    def _handle_remove_bot(self, sock, client: ClientInfo, payload: bytes):
        """Handle REMOVE_BOT: player requests the last bot be removed."""
        if self.game_active:
            client.send_raw(build_log("Can't remove bot mid-game"))
            return
        if not client.authenticated:
            return
        if not self.bots:
            return

        removed = self.bots.pop()
        log.info("Bot removed: %s by %s", removed.name, client.username)
        self._broadcast_lobby_state()

    # ------------------------------------------------------------------
    # Lobby handlers
    # ------------------------------------------------------------------

    def _handle_ready(self, sock, client: ClientInfo):
        if not client.authenticated:
            return
        client.ready = not client.ready
        log.info("Player %s ready=%s", client.username, client.ready)
        self._broadcast_lobby_state()

    def _handle_start_game(self, sock, client: ClientInfo):
        if self.game_active:
            return
        if not client.authenticated:
            return

        # Only ready players join the game
        ready_players = [c for c in self.clients.values()
                         if c.authenticated and c.ready]

        # Count ready bots
        ready_bots = [b for b in self.bots if b.ready]

        # Total player count includes real + local extras + bots
        total_player_slots = len(ready_players)
        for c in ready_players:
            total_player_slots += len(c.local_player_names)
        total_player_slots += len(ready_bots)

        if total_player_slots < 2:
            client.send_raw(build_log("Need 2+ ready players"))
            return

        if total_player_slots > MAX_PLAYERS:
            client.send_raw(build_log("Too many players (max %d)" % MAX_PLAYERS))
            return

        # Start game
        self.game_seed = random.randint(0, 0xFFFFFFFF)
        self.game_active = True
        self.game_paused = False
        self._last_tick = time.time()
        self._tick_counter = 0

        # Reset delta compression state for new game
        self.last_relayed_input.clear()
        self.relay_cooldown.clear()

        log.info("Game starting! Seed=%08X, %d player slots",
                 self.game_seed, total_player_slots)

        # Initialize game simulation
        self.sim = GameSimulation(self.game_type, self.num_lives,
                                 total_player_slots)

        # Assign player IDs (0-indexed)
        pid = 0

        # Real clients: primary player
        for c in ready_players:
            c.in_game = True
            c.alive = True
            c.game_player_id = pid
            c.local_player_ids = []
            self.sim.init_player(pid)
            pid += 1

        # Real clients: additional local players
        for c in ready_players:
            for ln in c.local_player_names:
                c.local_player_ids.append(pid)
                self.sim.init_player(pid)
                pid += 1

        # Bots
        for bot in ready_bots:
            bot.game_player_id = pid
            bot.reset_for_game()
            self.sim.init_player(pid)
            pid += 1

        # Send GAME_START to all real clients
        for c in ready_players:
            opponent_count = total_player_slots - 1
            c.send_raw(build_game_start(
                self.game_seed, c.game_player_id, opponent_count,
                self.game_type, self.num_lives))
            # Send LOCAL_PLAYER_ACK for additional local players
            for lp_id in c.local_player_ids:
                c.send_raw(build_local_player_ack(lp_id))

        # Send PLAYER_JOIN roster so clients know game_player_id -> name
        roster = []
        for c in ready_players:
            roster.append((c.game_player_id, c.username))
        for c in ready_players:
            for i, ln in enumerate(c.local_player_names):
                roster.append((c.local_player_ids[i], ln))
        for bot in ready_bots:
            roster.append((bot.game_player_id, bot.name))
        for c in ready_players:
            for pid, name in roster:
                c.send_raw(build_player_join(pid, name))

        # Re-evaluate AUTO tuning preset for the live player count
        self._evaluate_auto_preset()

        # Start first wave after a short delay for clients to init
        self._start_new_wave()

    # ------------------------------------------------------------------
    # In-game handlers
    # ------------------------------------------------------------------

    def _handle_input_state(self, sock, client: ClientInfo, payload: bytes):
        if not self.game_active or not client.in_game:
            return
        if len(payload) < 5:
            return

        # Extended format: [type:1][player_id:1][frame:2 BE][input:2 BE] (6 bytes)
        # Original format: [type:1][frame:2 BE][input:2 BE] (5 bytes)
        if len(payload) >= 6:
            # Extended format with explicit player_id
            player_id = payload[1]
            frame_num = (payload[2] << 8) | payload[3]
            input_bits = (payload[4] << 8) | payload[5]
            # Validate: must be either primary or a local player of this client
            valid_ids = [client.game_player_id] + client.local_player_ids
            if player_id not in valid_ids:
                return
        else:
            # Original format — infer player_id
            frame_num = (payload[1] << 8) | payload[2]
            input_bits = (payload[3] << 8) | payload[4]
            player_id = client.game_player_id

        # Delta compression: only relay when input changed or every 15 frames
        last = self.last_relayed_input.get(player_id, -1)
        cooldown = self.relay_cooldown.get(player_id, 15)

        if input_bits != last or cooldown >= 15:
            relay_msg = build_input_relay(player_id, frame_num, input_bits)
            for s, c in self.clients.items():
                if c.in_game and s != sock:
                    c.send_raw(relay_msg)
            self.last_relayed_input[player_id] = input_bits
            self.relay_cooldown[player_id] = 0
        else:
            self.relay_cooldown[player_id] = cooldown + 1

    def _handle_pause(self, sock, client: ClientInfo):
        if not self.game_active or not client.in_game:
            return
        self.game_paused = not self.game_paused
        pause_msg = build_pause_ack(self.game_paused)
        for s, c in self.clients.items():
            if c.authenticated and c.in_game:
                c.send_raw(pause_msg)

    def _handle_client_caps(self, sock, client: ClientInfo, payload: bytes):
        """Client announced its capabilities. Updates per-client state and
        immediately tells the client the current global sync mode so its
        sync engine and ours agree before any SHIP_SYNC traffic flows."""
        if len(payload) < 2:
            return
        caps = payload[1]
        client.supports_ring = bool(caps & CAP_SUPPORTS_RING)
        client.supports_ring_v2 = bool(caps & CAP_RING_V2)
        log.info("Client %s caps: 0x%02X (ring=%s ring_v2=%s)",
                 client.username or "?", caps,
                 client.supports_ring, client.supports_ring_v2)
        # Tell the client which engine to run. Always send so the client
        # knows even if the global default has changed since it connected.
        self._send_sync_mode_to(client, force=True)

    def _send_sync_mode_to(self, client: ClientInfo, force: bool = False):
        """Send DNET_MSG_SET_SYNC_MODE to one client if needed.

        RING mode is only honored on clients that announced ring support;
        everyone else gets LERP unconditionally (so old clients never see
        anything but the raw 22-byte SHIP_SYNC they were built to parse).

        Old (pre-1.1.0) clients never announced CLIENT_CAPS — they would
        also silently ignore SET_SYNC_MODE via their default-case
        dispatcher. We skip sending it to them at all to save 4 bytes
        on the wire per toggle change."""
        if not client.supports_ring:
            return
        global_mode = self.tuning.get("sync_mode", "LERP")
        effective = global_mode  # ring-capable, so just adopt global mode
        if not force and client.last_sent_sync_mode == effective:
            return
        client.send_raw(build_set_sync_mode(effective))
        client.last_sent_sync_mode = effective

    def _broadcast_sync_mode(self):
        """Send SET_SYNC_MODE to all connected, authenticated clients.
        Called when the admin flips the toggle. No-op for clients whose
        last_sent_sync_mode already matches the effective value."""
        for s, c in self.clients.items():
            if c.authenticated:
                self._send_sync_mode_to(c)

    def _handle_ship_state(self, sock, client: ClientInfo, payload: bytes):
        if not self.game_active or not client.in_game:
            return
        if len(payload) < 20:
            return

        # Extended format: [type:1][player_id:1][x:4][y:4][dx:4][dy:4][rot:2][flags:1] = 21
        # Original format: [type:1][x:4][y:4][dx:4][dy:4][rot:2][flags:1] = 20
        if len(payload) >= 21:
            # Extended format with explicit player_id
            player_id = payload[1]
            valid_ids = [client.game_player_id] + client.local_player_ids
            if player_id not in valid_ids:
                return
            x = struct.unpack("!i", payload[2:6])[0]
            y = struct.unpack("!i", payload[6:10])[0]
            dx = struct.unpack("!i", payload[10:14])[0]
            dy = struct.unpack("!i", payload[14:18])[0]
            flags = payload[20]
            raw_data = payload[2:]  # skip type and player_id
        else:
            # Original format — infer player_id
            player_id = client.game_player_id
            x = struct.unpack("!i", payload[1:5])[0]
            y = struct.unpack("!i", payload[5:9])[0]
            dx = struct.unpack("!i", payload[9:13])[0]
            dy = struct.unpack("!i", payload[13:17])[0]
            flags = payload[19]
            raw_data = payload[1:]

        if self.sim:
            self.sim.update_player_pos(player_id, x, y, dx, dy, flags,
                                        server_frame=self._tick_counter)

        # Apply tuning gates (decimate + skip_stationary w/ keepalive).
        # Defaults (PASSTHROUGH) make this block a no-op.
        client.ship_sync_relay_counter += 1
        should_relay = True
        decimate = self.tuning["ship_sync_decimate"]
        if decimate > 1:
            if (client.ship_sync_relay_counter % decimate) != 0:
                should_relay = False
        if should_relay and self.tuning["ship_sync_skip_stationary"]:
            vel_mag = abs(dx) + abs(dy)
            ticks_since = self._tick_counter - client.last_ship_sync_relay_tick
            keepalive = self.tuning["ship_sync_keepalive_ticks"]
            threshold = self.tuning["stationary_vel_threshold"]
            if vel_mag < threshold and ticks_since < keepalive:
                should_relay = False

        if should_relay:
            # Three recipient classes:
            #   - LERP / non-ring (old clients OR global mode == LERP):
            #     raw 22-byte SHIP_SYNC byte-for-byte identical to pre-1.1.0.
            #   - RING v1 (ring-capable but pre-1.1.3 client): legacy
            #     12-byte SHIP_SYNC_Q. Still has the q_angle rot bug,
            #     but we can't fix that without a new binary.
            #   - RING v2 (v1.1.3+ ring-capable client): 13-byte
            #     SHIP_SYNC_Q_V2 with raw int16 rot — correct rotation.
            global_mode = self.tuning.get("sync_mode", "LERP")
            raw_msg = build_ship_sync_raw(player_id, raw_data)
            quant_v1_msg = None
            quant_v2_msg = None
            try:
                rot_val = struct.unpack("!h", raw_data[16:18])[0]
            except struct.error:
                rot_val = 0
            for s, c in self.clients.items():
                if not c.in_game or s == sock:
                    continue
                if global_mode == "RING" and c.supports_ring:
                    if c.supports_ring_v2:
                        if quant_v2_msg is None:
                            quant_v2_msg = build_ship_sync_quant_v2(
                                player_id, self._tick_counter,
                                x, y, dx, dy, rot_val, flags)
                        c.send_raw(quant_v2_msg)
                    else:
                        if quant_v1_msg is None:
                            quant_v1_msg = build_ship_sync_quant(
                                player_id, self._tick_counter,
                                x, y, dx, dy, rot_val, flags)
                        c.send_raw(quant_v1_msg)
                else:
                    c.send_raw(raw_msg)
            client.last_ship_sync_relay_tick = self._tick_counter

    def _handle_asteroid_hit(self, sock, client: ClientInfo, payload: bytes):
        if not self.game_active or not client.in_game or not self.sim:
            return
        if len(payload) < 3:
            return

        slot = payload[1]
        scorer_id = payload[2]

        speed_scale = float(self.tuning.get("asteroid_speed_scale", 1.0))
        evt = self.sim.handle_asteroid_hit(slot, scorer_id, speed_scale)
        if evt:
            self._broadcast_event(evt)

    def _handle_ship_asteroid_hit(self, sock, client: ClientInfo,
                                  payload: bytes):
        """Handle SHIP_ASTEROID_HIT: Saturn detected player hit asteroid or PvP kill.

        For ship-asteroid (slot != 0xFF): target must be sender's own player.
        For PvP projectile kill (slot == 0xFF): target can be any player
        (allows Saturn to report hitting remote players / bots).
        """
        if not self.game_active or not client.in_game or not self.sim:
            return
        if len(payload) < 3:
            return

        slot = payload[1]
        player_id = payload[2]

        # Phase 4 hit-message v2: optional [firer_frame:2 BE] tail. Old
        # clients send 3-byte payload (no frame); new clients send 5-byte.
        # Server simply ignores the extra bytes if absent.
        firer_frame = None
        if len(payload) >= 5:
            firer_frame = (payload[3] << 8) | payload[4]
            # Lag-comp lookup: where was the victim at firer_frame?
            # Logged at debug level for diagnostic purposes; we do not
            # currently REJECT hits based on this (cheating isn't a
            # concern, and rejecting would be a behavior change at risk
            # of breaking legit hits during modem stalls). Once the
            # diagnostic data validates the model in real sessions, we
            # can promote this to authoritative validation.
            if slot == 0xFF and player_id in self.sim.players:
                hist = self.sim.lookup_player_at(player_id, firer_frame)
                if hist:
                    cur = self.sim.players[player_id]
                    drift_x = abs(cur["x"] - hist[0])
                    drift_y = abs(cur["y"] - hist[1])
                    log.debug(
                        "PvP hit lag-comp: pid=%d firer_frame=%d "
                        "now=(%d,%d) then=(%d,%d) drift=(%d,%d)",
                        player_id, firer_frame,
                        cur["x"], cur["y"], hist[0], hist[1],
                        drift_x, drift_y)

        # For ship-asteroid collision, validate target belongs to sender.
        # For PvP kill (slot=0xFF), any valid in-game player is acceptable.
        if slot != 0xFF:
            valid_ids = [client.game_player_id] + client.local_player_ids
            if player_id not in valid_ids:
                return
        else:
            if player_id not in self.sim.players:
                return

        # Validate player is alive and not invulnerable
        p = self.sim.players.get(player_id)
        if not p or not p["alive"] or p["invuln_frames"] > 0:
            return
        if p["respawn_frames"] > 0:
            return

        # Kill the player
        kill_evt = self.sim._kill_player(player_id, slot)
        if kill_evt:
            self._broadcast_event(kill_evt)

        # Destroy the asteroid (if still alive); slot 0xFF = projectile kill, no asteroid
        if slot != 0xFF:
            speed_scale = float(self.tuning.get("asteroid_speed_scale", 1.0))
            destroy_evt = self.sim._destroy_asteroid(slot, 0xFF, speed_scale)
            if destroy_evt:
                self._broadcast_event(destroy_evt)

        # Check game over
        go_evt = self.sim._check_game_over()
        if go_evt:
            self._broadcast_event(go_evt)

    # ------------------------------------------------------------------
    # Game simulation tick + wave management
    # ------------------------------------------------------------------

    def _game_tick(self):
        """Run one server tick and broadcast events."""
        if not self.sim:
            return

        self._tick_counter += 1
        # AUTO mode: re-evaluate once per second (20 ticks) so the
        # downgrade hysteresis can expire without relying on an event.
        if self._auto_mode and (self._tick_counter % 20) == 0:
            self._evaluate_auto_preset()
        # Pass the wrap mode to the simulation so it picks the
        # Saturn-matching edge-snap when asteroid correction is on.
        wrap_saturn = (
            self.tuning.get("sync_mode", "LERP") == "RING" and
            self.tuning.get("asteroid_sync_correct", True))
        speed_scale = float(self.tuning.get("asteroid_speed_scale", 1.0))
        ship_radius_bonus = int(
            self.tuning.get("ship_collision_radius_bonus", 0))
        events = self.sim.tick(wrap_saturn=wrap_saturn,
                                speed_scale=speed_scale,
                                ship_radius_bonus=ship_radius_bonus)
        for evt in events:
            if evt[0] == "wave_over":
                self._start_new_wave()
            else:
                self._broadcast_event(evt)

        # Periodic asteroid drift correction (1.5s @ 20Hz). Belt-and-
        # suspenders: with the catch-up loop and Saturn-matching wrap,
        # client and server asteroid sims should already agree to within
        # a pixel. ASTEROID_SYNC catches anything that slips through —
        # the client snaps if drift < 3 px, lerps 75% otherwise.
        if (wrap_saturn and self._tick_counter > 0 and
                (self._tick_counter % 30) == 0):
            self._broadcast_asteroid_sync()

        # End game when no humans remain alive (bots don't keep game going)
        if self.game_active and self.sim and not self.sim.game_over:
            bot_pids = {b.game_player_id for b in self.bots if b.in_game}
            alive_pids = {pid for pid, p in self.sim.players.items()
                          if p["alive"]}
            human_alive = alive_pids - bot_pids
            if not human_alive and alive_pids:
                self.sim.game_over = True
                winner = next(iter(alive_pids))
                self._broadcast_event(("game_over", winner))

        # Bot AI: generate inputs, update physics, relay to clients
        for bot in self.bots:
            if not bot.in_game or not bot.alive:
                continue

            # Bot is respawning — just tick physics (counts down respawn) and skip AI/sync
            if bot.respawn > 0:
                for _ in range(GameSimulation.TICK_RATIO):
                    bot.update_physics(0)  # no input while respawning
                continue

            bits = bot.ai.tick()
            for _ in range(GameSimulation.TICK_RATIO):
                bot.update_physics(bits)

            # Update simulation with bot position
            flags = 0x01  # alive
            if bot.invuln > 0:
                flags |= 0x02
            if bits & (INPUT_UP | INPUT_B):
                flags |= 0x04
            if self.sim:
                self.sim.update_player_pos(bot.game_player_id,
                                           bot.x, bot.y,
                                           bot.dx, bot.dy, flags,
                                           server_frame=self._tick_counter)

            # Delta compression for bot input relay
            bot.force_send_counter += 1
            if bits != bot.last_sent_bits or bot.force_send_counter >= 15:
                relay_msg = build_input_relay(bot.game_player_id,
                                              bot.ai.frame & 0xFFFF, bits)
                self._broadcast_to_game(relay_msg)
                bot.last_sent_bits = bits
                bot.force_send_counter = 0

            # Relay bot ship state, throttled by tuning.bot_sync_every_n_ticks.
            # Default (1) = every tick (~20 Hz) matching pre-tuning behavior.
            bot.sync_tick_counter += 1
            bot_sync_every_n = max(1, int(self.tuning["bot_sync_every_n_ticks"]))
            if (bot.sync_tick_counter % bot_sync_every_n) == 0:
                # Same three-way dispatch as human SHIP_SYNC relay:
                # raw (LERP / no-caps), quant v1 (legacy ring), quant v2
                # (v1.1.3+ ring with fixed int16 rot).
                global_mode = self.tuning.get("sync_mode", "LERP")
                raw_sync = build_ship_sync(
                    bot.game_player_id, bot.x, bot.y,
                    bot.dx, bot.dy, bot.rot & 0x7FFF, flags)
                quant_v1 = None
                quant_v2 = None
                for s, c in self.clients.items():
                    if not c.in_game:
                        continue
                    if global_mode == "RING" and c.supports_ring:
                        if c.supports_ring_v2:
                            if quant_v2 is None:
                                quant_v2 = build_ship_sync_quant_v2(
                                    bot.game_player_id, self._tick_counter,
                                    bot.x, bot.y, bot.dx, bot.dy,
                                    bot.rot & 0x7FFF, flags)
                            c.send_raw(quant_v2)
                        else:
                            if quant_v1 is None:
                                quant_v1 = build_ship_sync_quant(
                                    bot.game_player_id, self._tick_counter,
                                    bot.x, bot.y, bot.dx, bot.dy,
                                    bot.rot & 0x7FFF, flags)
                            c.send_raw(quant_v1)
                    else:
                        c.send_raw(raw_sync)

    def _start_new_wave(self):
        """Start a new wave: generate asteroids + player spawns."""
        if not self.sim:
            return

        speed_scale = float(self.tuning.get("asteroid_speed_scale", 1.0))
        wave, asteroid_data, player_spawns = self.sim.start_wave(
            speed_scale=speed_scale)

        # Broadcast WAVE_EVENT
        wave_msg = build_wave_event(wave, asteroid_data,
                                    DISASTEROID_SPAWN_TIMER)
        self._broadcast_to_game(wave_msg)

        # Broadcast PLAYER_SPAWN for each alive player
        for pid, angle, invuln in player_spawns:
            spawn_msg = build_player_spawn(pid, angle, invuln)
            self._broadcast_to_game(spawn_msg)
            # Update bot state on spawn (use game-type-correct radii)
            for bot in self.bots:
                if bot.game_player_id == pid:
                    rad = math.radians(angle)
                    if self.sim.game_type == GAME_TYPE_VERSUS:
                        h_radius, v_radius = 120, 80
                    else:
                        h_radius, v_radius = 40, 40
                    bot.x = int(h_radius * math.cos(rad) * FIXED_SCALE)
                    bot.y = int(v_radius * math.sin(rad) * FIXED_SCALE)
                    bot.dx = 0
                    bot.dy = 0
                    bot.rot = (angle + 90) % 360
                    bot.invuln = invuln
                    bot.alive = True
                    break

        log.info("Wave %d started: %d asteroids, %d players spawned",
                 wave, len(asteroid_data), len(player_spawns))

    def _broadcast_event(self, evt):
        """Convert a simulation event to a message and broadcast."""
        if evt[0] == "asteroid_destroy":
            _, slot, scorer_id, children = evt
            msg = build_asteroid_destroy(slot, scorer_id, children)
            self._broadcast_to_game(msg)

        elif evt[0] == "player_kill":
            _, pid, lives, angle, invuln, respawn = evt
            msg = build_player_kill(pid, lives, angle, invuln, respawn)
            self._broadcast_to_game(msg)
            log.info("Player %d killed (lives=%d)", pid, lives)
            # Update bot state if killed player is a bot
            for bot in self.bots:
                if bot.game_player_id == pid:
                    if lives <= 0:
                        bot.alive = False
                        bot.dx = 0
                        bot.dy = 0
                    else:
                        # Reset bot to respawn position
                        rad = math.radians(angle)
                        if self.sim and self.sim.game_type == GAME_TYPE_VERSUS:
                            h_radius, v_radius = 120, 80
                        else:
                            h_radius, v_radius = 40, 40
                        bot.x = int(h_radius * math.cos(rad) * FIXED_SCALE)
                        bot.y = int(v_radius * math.sin(rad) * FIXED_SCALE)
                        bot.dx = 0
                        bot.dy = 0
                        bot.invuln = invuln
                        bot.respawn = respawn
                    break

        elif evt[0] == "game_over":
            _, winner = evt
            msg = build_game_over(winner)
            self._broadcast_to_game(msg)
            self.game_active = False
            log.info("Game over! Winner=%d", winner)
            # Update leaderboard BEFORE resetting in_game flags,
            # because _update_leaderboard checks c.in_game to find participants
            self._update_leaderboard(winner)
            # Reset player states
            for s, c in self.clients.items():
                if c.in_game:
                    c.in_game = False
                    c.ready = False
            # Reset bot states
            for bot in self.bots:
                bot.in_game = False
                bot.ready = True  # Bots auto-ready for next game
            self._broadcast_lobby_state()
            self._broadcast_leaderboard()

    def _broadcast_to_game(self, msg: bytes):
        """Send a message to all in-game clients."""
        for s, c in self.clients.items():
            if c.in_game:
                c.send_raw(msg)

    def _broadcast_asteroid_sync(self):
        """Build + send ASTEROID_SYNC for active asteroids to ring-capable
        clients. Old clients (no CLIENT_CAPS announcement) skip via the
        in_game / supports_ring filter so they never see this message at
        all — they remain on the v1.0.0/v1.1.0 deterministic-only path."""
        if not self.sim:
            return
        entries = []
        for slot, a in enumerate(self.sim.asteroids):
            if a is None or not a.get("alive"):
                continue
            entries.append((slot, a["x"], a["y"], a["dx"], a["dy"]))
        if not entries:
            return
        msg = build_asteroid_sync(entries)
        for s, c in self.clients.items():
            if c.in_game and c.supports_ring:
                c.send_raw(msg)

    # ------------------------------------------------------------------
    # Lobby broadcast
    # ------------------------------------------------------------------

    def _broadcast_lobby_state(self):
        players = []
        for c in self.clients.values():
            if c.authenticated:
                players.append({
                    "id": c.user_id,
                    "name": c.username,
                    "ready": c.ready,
                })
                # Include local extra players
                for ln in c.local_player_names:
                    players.append({
                        "id": c.user_id,
                        "name": ln,
                        "ready": c.ready,
                    })
        # Include bots
        for bot in self.bots:
            players.append({
                "id": 200 + bot.bot_id,  # high IDs for bots
                "name": bot.name,
                "ready": bot.ready,
            })

        msg = build_lobby_state(players)
        for s, c in self.clients.items():
            if c.authenticated:
                c.send_raw(msg)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _remove_client(self, sock, reason: str):
        client = self.clients.get(sock)
        if client:
            log.info("Removing %s (%s): %s",
                     client.username or "unknown", client.address, reason)
            if client.authenticated and client.username:
                event = "kicked-by-admin" if reason == "kicked by admin" else "leave"
                self._log_join(client.username,
                               "%s:%d" % client.address, event)

            if client.in_game and self.game_active and self.sim:
                # Mark this player (and their local extras) as dead in the sim
                # but DON'T end the game for everyone else
                pids_to_kill = [client.game_player_id] + client.local_player_ids
                for pid in pids_to_kill:
                    if pid in self.sim.players:
                        self.sim.players[pid]["alive"] = False
                        self.sim.players[pid]["lives"] = 0

                # Notify other in-game clients
                leave_msg = build_player_leave(client.user_id)
                log_msg = build_log("%s disconnected" % (client.username or "Player"))
                for s, c in self.clients.items():
                    if c.in_game and s != sock:
                        c.send_raw(leave_msg)
                        c.send_raw(log_msg)
                        # Send PLAYER_KILL with 0 lives for each removed player
                        for pid in pids_to_kill:
                            c.send_raw(build_player_kill(pid, 0, 0, 0, 0))

                client.in_game = False
                client.ready = False

                # Check if any real players remain in the game
                remaining = [c for c in self.clients.values()
                             if c.in_game and c is not client]
                if not remaining:
                    # No real players left — end the game
                    self.game_active = False
                    self.sim = None
                    for bot in self.bots:
                        bot.in_game = False
                        bot.ready = True
                    self._broadcast_lobby_state()
            elif client.in_game:
                client.in_game = False
                client.ready = False

            del self.clients[sock]
            # Re-evaluate AUTO tuning preset after player count changes.
            self._evaluate_auto_preset()
        else:
            log.info("Removing unknown socket: %s", reason)

        self.authenticated_bridges.discard(sock)

        try:
            sock.close()
        except OSError:
            pass

        if not self.game_active:
            self._broadcast_lobby_state()

    def _cleanup_dead_sockets(self):
        dead = []
        for sock in list(self.pending_auth.keys()):
            try:
                sock.fileno()
            except OSError:
                dead.append(sock)
        for sock in dead:
            del self.pending_auth[sock]

        dead = []
        for sock in list(self.clients.keys()):
            try:
                sock.fileno()
            except OSError:
                dead.append(sock)
        for sock in dead:
            self._remove_client(sock, "dead socket")

    def _check_timeouts(self, now: float):
        # Auth timeouts
        expired = [s for s, info in self.pending_auth.items()
                   if now > info["deadline"]]
        for sock in expired:
            log.warning("Auth timeout for %s", self.pending_auth[sock]["address"])
            sock.close()
            del self.pending_auth[sock]

        # Heartbeat timeouts
        for sock in list(self.clients.keys()):
            client = self.clients[sock]
            if now - client.last_activity > HEARTBEAT_TIMEOUT:
                self._remove_client(sock, "heartbeat timeout")

    # ------------------------------------------------------------------
    # Admin portal
    # ------------------------------------------------------------------

    def _load_join_history(self):
        try:
            if os.path.exists(self._join_history_path):
                with open(self._join_history_path, "r") as f:
                    self._join_history = json.load(f)
        except Exception as e:
            log.warning("Failed to load join history: %s", e)
            self._join_history = []

    def _save_join_history(self):
        try:
            if len(self._join_history) > 1000:
                self._join_history = self._join_history[-1000:]
            with open(self._join_history_path, "w") as f:
                json.dump(self._join_history, f, indent=2)
        except Exception as e:
            log.warning("Failed to save join history: %s", e)

    def _log_join(self, name: str, ip: str, event: str, reason: str = ""):
        # New entries carry both human-readable `time` (kept for legacy
        # /api/history consumers) and epoch `t` (used by /api/join_history
        # so the unified admin can render relative timestamps). `reason`
        # is optional and defaults to empty for backward compatibility.
        self._join_history.append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "t": int(time.time()),
            "name": name, "ip": ip, "event": event,
            "reason": reason,
        })
        self._save_join_history()

    def _join_history_events(self, limit: int = 200):
        """Return the last `limit` join entries normalized to the shape
        used by the unified admin portal: {events:[{t,ts,event,name,ip,reason}]}.
        Backward-compatible with legacy entries that lack `t` or `reason`."""
        # Cap limit to a reasonable upper bound to avoid pathological queries.
        if limit < 1:
            limit = 1
        if limit > 1000:
            limit = 1000
        out = []
        for entry in self._join_history[-limit:]:
            ts_str = entry.get("time", "")
            t_epoch = entry.get("t")
            # Best-effort epoch from legacy `time` strings if `t` missing.
            if t_epoch is None and ts_str:
                try:
                    t_epoch = int(time.mktime(
                        time.strptime(ts_str, "%Y-%m-%d %H:%M:%S")))
                except (ValueError, OverflowError):
                    t_epoch = 0
            out.append({
                "t": int(t_epoch or 0),
                "ts": ts_str,
                "event": entry.get("event", ""),
                "name": entry.get("name", ""),
                "ip": entry.get("ip", ""),
                "reason": entry.get("reason", ""),
            })
        # Newest first for display.
        out.reverse()
        return out

    # ------------------------------------------------------------------
    # Tuning helpers
    # ------------------------------------------------------------------

    def _apply_tune_command(self, data: dict):
        """Apply a validated tuning command from the admin queue.

        `data` may contain `preset` (including "AUTO" / "CUSTOM") and/or
        individual knob overrides. Setting any individual knob switches
        preset to CUSTOM. Setting preset=AUTO enables AUTO mode and
        immediately re-evaluates.
        """
        if not isinstance(data, dict):
            return
        preset = data.get("preset")
        # sync_mode and asteroid_sync_correct are orthogonal axes — they
        # never force the bandwidth preset to CUSTOM. Pull them aside so
        # the rest of the logic only sees true bandwidth knobs.
        sync_mode_change = data.get("sync_mode")
        asteroid_correct_change = data.get("asteroid_sync_correct")
        asteroid_speed_scale_change = data.get("asteroid_speed_scale")
        ship_radius_bonus_change = data.get("ship_collision_radius_bonus")
        ORTHOGONAL = {"preset", "sync_mode",
                       "asteroid_sync_correct", "asteroid_speed_scale",
                       "ship_collision_radius_bonus"}
        bandwidth_keys = [k for k in data.keys() if k not in ORTHOGONAL]

        if preset == "AUTO":
            # Enable AUTO and snap to the target preset immediately; hysteresis
            # only applies to count changes during ongoing AUTO operation.
            self._auto_mode = True
            self._auto_last_downgrade = 0.0
            n = self._current_player_count()
            target = self._auto_select_preset(n)
            self._apply_tuning_preset(target, from_auto=True)
            log.info("Tuning: AUTO mode enabled, snapped to %s (n=%d)",
                     target, n)
            # sync_mode and asteroid_sync_correct passed alongside AUTO
            # still apply independently.
            if sync_mode_change is not None:
                self.tuning["sync_mode"] = sync_mode_change
                log.info("Tuning: sync_mode=%s", sync_mode_change)
                self._broadcast_sync_mode()
            if asteroid_correct_change is not None:
                self.tuning["asteroid_sync_correct"] = \
                    bool(asteroid_correct_change)
                log.info("Tuning: asteroid_sync_correct=%s",
                         asteroid_correct_change)
            if asteroid_speed_scale_change is not None:
                self.tuning["asteroid_speed_scale"] = float(
                    asteroid_speed_scale_change)
                log.info("Tuning: asteroid_speed_scale=%.2f",
                         float(asteroid_speed_scale_change))
            if ship_radius_bonus_change is not None:
                self.tuning["ship_collision_radius_bonus"] = int(
                    ship_radius_bonus_change)
                log.info("Tuning: ship_collision_radius_bonus=%d",
                         int(ship_radius_bonus_change))
            return

        if preset and preset != "CUSTOM":
            # Named preset bundle — apply and disable AUTO.
            self._apply_tuning_preset(preset, from_auto=False)

        if bandwidth_keys:
            # Individual bandwidth-knob overrides — disable AUTO, mark as CUSTOM.
            for key in bandwidth_keys:
                self.tuning[key] = data[key]
            self.tuning["preset"] = "CUSTOM"
            self._auto_mode = False
            self._auto_current = "CUSTOM"
            log.info("Tuning: CUSTOM (knobs: %s)",
                     ", ".join("%s=%s" % (k, data[k]) for k in bandwidth_keys))

        if sync_mode_change is not None:
            # sync_mode is independent — does NOT force CUSTOM preset.
            self.tuning["sync_mode"] = sync_mode_change
            log.info("Tuning: sync_mode=%s", sync_mode_change)
            # Notify all connected clients. Old/non-ring-capable clients
            # are silently kept on LERP regardless of the global setting.
            self._broadcast_sync_mode()

        if asteroid_correct_change is not None:
            # Pure server-side knob — does NOT force CUSTOM preset and
            # does NOT broadcast a wire message. Takes effect on the
            # next tick when the gate is re-evaluated.
            self.tuning["asteroid_sync_correct"] = bool(asteroid_correct_change)
            log.info("Tuning: asteroid_sync_correct=%s",
                     asteroid_correct_change)

        if asteroid_speed_scale_change is not None:
            # Pure server-side knob — takes effect on the next asteroid
            # spawn (existing in-flight asteroids keep their velocity).
            self.tuning["asteroid_speed_scale"] = float(
                asteroid_speed_scale_change)
            log.info("Tuning: asteroid_speed_scale=%.2f",
                     float(asteroid_speed_scale_change))

        if ship_radius_bonus_change is not None:
            # Pure server-side knob — takes effect on the next tick.
            # Negative = stricter ship-asteroid collision detection;
            # used to reduce ghost-kills from server-side stale player
            # position dead-reckoning.
            self.tuning["ship_collision_radius_bonus"] = int(
                ship_radius_bonus_change)
            log.info("Tuning: ship_collision_radius_bonus=%d",
                     int(ship_radius_bonus_change))

    def _apply_tuning_preset(self, preset_name: str,
                             from_auto: bool = False) -> bool:
        """Apply a preset bundle to self.tuning. Returns True on success."""
        bundle = self._tuning_presets.get(preset_name)
        if bundle is None:
            return False
        for k, v in bundle.items():
            self.tuning[k] = v
        self.tuning["preset"] = preset_name
        if not from_auto:
            # Manual preset selection disables AUTO until user re-enables it.
            self._auto_mode = False
        self._auto_current = preset_name
        log.info("Tuning preset applied: %s%s",
                 preset_name, " (via AUTO)" if from_auto else "")
        return True

    def _current_player_count(self) -> int:
        """Humans (in-game) + bots (in-game) — count against bandwidth ceiling."""
        humans = sum(1 for c in self.clients.values()
                     if c.in_game and c.authenticated)
        human_locals = sum(len(c.local_player_ids) for c in self.clients.values()
                           if c.in_game)
        bots = sum(1 for b in self.bots if b.in_game)
        return humans + human_locals + bots

    def _auto_select_preset(self, n: int) -> str:
        if n <= 4:
            return "PASSTHROUGH"
        if n <= 6:
            return "LIGHT"
        if n <= 8:
            return "MODERATE"
        return "AGGRESSIVE"

    # Preset severity ordering (higher = more aggressive).
    _PRESET_RANK = {
        "PASSTHROUGH": 0,
        "LIGHT": 1,
        "MODERATE": 2,
        "AGGRESSIVE": 3,
    }

    def _evaluate_auto_preset(self):
        """Re-evaluate AUTO preset based on live player count.

        Upgrade (more aggressive) applies immediately.
        Downgrade (less aggressive) applies only after the lower count has
        been stable for self._AUTO_DOWNGRADE_HOLD seconds.
        """
        if not self._auto_mode:
            return
        n = self._current_player_count()
        target = self._auto_select_preset(n)
        current = self._auto_current
        if target == current:
            self._auto_last_downgrade = 0.0
            return
        cur_rank = self._PRESET_RANK.get(current, 0)
        tgt_rank = self._PRESET_RANK.get(target, 0)
        now = time.time()
        if tgt_rank > cur_rank:
            # Upgrade immediately (bandwidth pressure is rising).
            self._apply_tuning_preset(target, from_auto=True)
            self._auto_last_downgrade = 0.0
            log.info("AUTO: upgraded %s -> %s (n=%d)", current, target, n)
        else:
            # Candidate downgrade — require stable hold period.
            if self._auto_last_downgrade == 0.0:
                self._auto_last_downgrade = now
                return
            if (now - self._auto_last_downgrade) >= self._AUTO_DOWNGRADE_HOLD:
                self._apply_tuning_preset(target, from_auto=True)
                self._auto_last_downgrade = 0.0
                log.info("AUTO: downgraded %s -> %s (n=%d, stable for %.1fs)",
                         current, target, n, self._AUTO_DOWNGRADE_HOLD)

    def _start_admin_server(self):
        if not self._admin_port:
            return
        handler_class = _make_disast_admin_handler(self)
        try:
            self._admin_httpd = ThreadingHTTPServer(
                ("0.0.0.0", self._admin_port), handler_class)
            self._admin_httpd.daemon_threads = True
        except OSError as e:
            log.error("Failed to start admin server on port %d: %s",
                      self._admin_port, e)
            return
        self._admin_thread = threading.Thread(
            target=self._admin_httpd.serve_forever, daemon=True)
        self._admin_thread.start()
        log.info("Admin portal listening on http://0.0.0.0:%d/",
                 self._admin_port)

    def _process_admin_commands(self):
        while True:
            try:
                cmd = self._admin_command_queue.get_nowait()
            except queue.Empty:
                break
            action = cmd.get("cmd", "")
            if action == "kick":
                target_uuid = cmd.get("uuid", "")
                for sock, info in list(self.clients.items()):
                    if info.uuid == target_uuid:
                        log.info("Admin kicked %s", info.username)
                        self._remove_client(sock, "kicked by admin")
                        break
            elif action == "end_game":
                if self.game_active and self.sim:
                    log.info("Admin ended game")
                    self.sim.game_over = True
            elif action == "restart":
                log.info("Admin requested restart")
                self._running = False
            elif action == "tune":
                self._apply_tune_command(cmd.get("data", {}))

    def _build_admin_state(self):
        now = time.time()
        players = []
        for sock, info in list(self.clients.items()):
            if not info.authenticated:
                continue
            score = 0
            deaths = 0
            status = "lobby"
            if info.in_game and self.sim:
                pid = info.game_player_id
                score = int(self.sim.scores.get(pid, 0))
                p = self.sim.players.get(pid)
                if p and p.get("alive"):
                    status = "in-game"
                else:
                    status = "dead"
                    deaths = 1
            players.append({
                "username": info.username,
                "uuid": info.uuid,
                "status": status,
                "address": "%s:%d" % info.address,
                "idle": round(now - info.last_activity, 1),
                "ready": info.ready,
                "score": score,
                "deaths": deaths,
                "egress_bps": getattr(info, "_egress_rate", 0),
            })
        game = {
            "active": self.game_active,
            "phase": "Playing" if self.game_active else "Lobby",
            "human_count": sum(1 for c in self.clients.values()
                               if c.authenticated),
            "bot_count": len(self.bots),
            "wave": self.sim.wave if (self.game_active and self.sim) else 0,
            "asteroids_left": sum(1 for a in self.sim.asteroids
                                  if a is not None and a["alive"])
                              if (self.game_active and self.sim) else 0,
            "ships_alive": sum(1 for p in self.sim.players.values()
                               if p.get("alive"))
                           if (self.game_active and self.sim) else 0,
            "game_type": ("Co-op" if self.game_type == GAME_TYPE_COOP
                          else "Versus") if self.game_active else "-",
        }
        tuning = {
            **self.tuning,
            "auto_mode": self._auto_mode,
            "auto_current": self._auto_current,
            "player_count": self._current_player_count(),
        }
        return {
            "uptime": round(now - self._start_time, 1),
            "total_joins": len(self._join_history),
            "players": players,
            "game": game,
            "tuning": tuning,
        }


ADMIN_HTML_STUB = b"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Disasteroids Admin</title></head><body style="font-family:monospace;background:#1a1a2e;color:#e0e0e0;padding:24px">
<h1>Disasteroids Admin</h1>
<p>This service exposes the Disasteroids admin JSON API.</p>
<p>Visit <a style="color:#f5a623" href="/admin/">the unified Saturn admin portal</a> for the dashboard.</p>
</body></html>"""


_TUNING_INT_RANGES = {
    "ship_sync_decimate":        (1, 4),
    "stationary_vel_threshold":  (0, 50),
    "ship_sync_keepalive_ticks": (15, 120),
    "bot_sync_every_n_ticks":    (1, 4),
    "ship_collision_radius_bonus": (-3, 3),
}
_TUNING_BOOL_KEYS = {"ship_sync_skip_stationary", "asteroid_sync_correct"}
_TUNING_STR_ENUMS = {
    "sync_mode": ("LERP", "RING"),
}
# Float-valued tunables with [lo, hi] inclusive range. Accept int or float
# in the JSON payload; validator coerces to float.
_TUNING_FLOAT_RANGES = {
    "asteroid_speed_scale": (0.25, 2.0),
}


def _validate_tuning_update(data, presets):
    """Validate a POST /api/tuning payload. Returns (ok, error_msg)."""
    if not isinstance(data, dict):
        return False, "payload must be a JSON object"
    preset = data.get("preset")
    if preset is not None:
        valid_presets = set(presets.keys()) | {"AUTO", "CUSTOM"}
        if preset not in valid_presets:
            return False, "unknown preset: %s" % preset
    for key, val in data.items():
        if key in ("preset",):
            continue
        if key in _TUNING_INT_RANGES:
            lo, hi = _TUNING_INT_RANGES[key]
            if not isinstance(val, int) or isinstance(val, bool):
                return False, "%s must be int" % key
            if val < lo or val > hi:
                return False, "%s out of range [%d,%d]" % (key, lo, hi)
        elif key in _TUNING_BOOL_KEYS:
            if not isinstance(val, bool):
                return False, "%s must be bool" % key
        elif key in _TUNING_STR_ENUMS:
            allowed = _TUNING_STR_ENUMS[key]
            if not isinstance(val, str) or val not in allowed:
                return False, "%s must be one of %s" % (key, list(allowed))
        elif key in _TUNING_FLOAT_RANGES:
            lo, hi = _TUNING_FLOAT_RANGES[key]
            # JSON serializes 1 as int — coerce explicitly.
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                return False, "%s must be a number" % key
            fv = float(val)
            if fv < lo or fv > hi:
                return False, "%s out of range [%g, %g]" % (key, lo, hi)
        else:
            return False, "unknown key: %s" % key
    return True, ""


def _make_disast_admin_handler(server_ref):
    class DisastAdminHandler(BaseHTTPRequestHandler):
        srv = server_ref

        def log_message(self, fmt, *args):
            log.debug("Admin HTTP: " + fmt, *args)

        def _check_auth(self):
            if self.headers.get("X-Admin-Auth") == "nginx-verified":
                return True
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                self._send_auth_required()
                return False
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                user, pwd = decoded.split(":", 1)
            except Exception:
                self._send_auth_required()
                return False
            srv = self.srv
            if user != srv._admin_user or pwd != srv._admin_password:
                self._send_auth_required()
                return False
            return True

        def _send_auth_required(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate",
                             'Basic realm="Disasteroids Admin"')
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "12")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            self.close_connection = True

        def _send_json(self, data, code=200):
            body = json.dumps(data).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def do_GET(self):
            if not self._check_auth():
                return
            path = urlparse(self.path).path
            if path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(ADMIN_HTML_STUB)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(ADMIN_HTML_STUB)
                self.close_connection = True
            elif path == "/api/state":
                self._send_json(self.srv._build_admin_state())
            elif path == "/api/history":
                entries = list(self.srv._join_history[-200:])
                entries.reverse()
                self._send_json({"entries": entries})
            elif path == "/api/join_history":
                # Mirror of MMM/Utenyaa shape so the unified admin can render
                # this tab the same way. Supports ?limit=N (default 200, capped 1000).
                qs = parse_qs(urlparse(self.path).query)
                try:
                    limit = int(qs.get("limit", ["200"])[0])
                except (ValueError, TypeError):
                    limit = 200
                self._send_json(
                    {"events": self.srv._join_history_events(limit)})
            elif path == "/api/tuning":
                srv = self.srv
                presets = list(srv._tuning_presets.keys()) + ["AUTO", "CUSTOM"]
                self._send_json({
                    "tuning": dict(srv.tuning),
                    "auto_mode": srv._auto_mode,
                    "auto_current": srv._auto_current,
                    "player_count": srv._current_player_count(),
                    "presets": presets,
                    "preset_bundles": srv._tuning_presets,
                })
            else:
                self.send_error(404)

        def do_POST(self):
            if not self._check_auth():
                return
            path = urlparse(self.path).path
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len > 0 else b""
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                data = {}
            srv = self.srv
            if path == "/api/kick":
                target_uuid = data.get("uuid", "")
                if not target_uuid:
                    self._send_json({"error": "missing uuid"}, 400)
                    return
                srv._admin_command_queue.put(
                    {"cmd": "kick", "uuid": target_uuid})
                self._send_json({"message": "Kick queued"})
            elif path == "/api/end_game":
                srv._admin_command_queue.put({"cmd": "end_game"})
                self._send_json({"message": "End game queued"})
            elif path == "/api/restart":
                srv._admin_command_queue.put({"cmd": "restart"})
                self._send_json({"message": "Restart queued"})
            elif path == "/api/tuning":
                # Validate incoming fields up front so bad input is rejected
                # without ever reaching the game loop.
                valid, error = _validate_tuning_update(data,
                                                       srv._tuning_presets)
                if not valid:
                    self._send_json({"error": error}, 400)
                    return
                srv._admin_command_queue.put({"cmd": "tune", "data": data})
                self._send_json({"message": "Tuning queued"})
            else:
                self.send_error(404)

    return DisastAdminHandler


# ==========================================================================
# CLI
# ==========================================================================


def main():
    parser = argparse.ArgumentParser(description="Disasteroids NetLink Game Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=4822, help="Bind port")
    parser.add_argument("--bots", type=int, default=0,
                        help="Number of server-side bot players (0-11)")
    parser.add_argument("--admin-port", type=int, default=0,
                        help="Admin HTTP port (0=disabled)")
    parser.add_argument("--admin-user", default="admin",
                        help="Admin username (for direct-port access)")
    parser.add_argument("--admin-password", default="disast2026",
                        help="Admin password (for direct-port access)")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate quantization round-trip on startup. A botched q_*/d_*
    # edit fails here loudly instead of silently mangling SHIP_SYNC_Q.
    try:
        _q_parity_selftest()
    except AssertionError as e:
        log.error("Quantization self-test FAILED: %s", e)
        sys.exit(1)
    log.info("Quantization self-test passed (q_pos / q_vel / q_angle)")

    # v1.1.1 — validate asteroid drift correction subsystem on startup
    # (Saturn-matching wrap, build_asteroid_sync round-trip, catch-up
    # loop arithmetic). Same fail-loud pattern as the quant test above.
    try:
        _asteroid_sync_selftest()
    except AssertionError as e:
        log.error("Asteroid sync self-test FAILED: %s", e)
        sys.exit(1)
    log.info("Asteroid sync self-test passed (wrap / build / catch-up)")

    server = DisasteroidsServer(host=args.host, port=args.port,
                                num_bots=args.bots,
                                admin_port=args.admin_port,
                                admin_user=args.admin_user,
                                admin_password=args.admin_password)
    if args.bots > 0:
        log.info("Starting with %d bot(s): %s", args.bots,
                 ", ".join(b.name for b in server.bots))
    try:
        server.start()
    except KeyboardInterrupt:
        log.info("Server shutting down")


if __name__ == "__main__":
    main()
