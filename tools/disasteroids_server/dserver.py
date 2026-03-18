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
import json
import logging
import math
import os
import random
import select
import socket
import struct
import sys
import time
import uuid

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


def _wrap_coord(val: int, lo: int, hi: int) -> int:
    """Wrap a jo_fixed coordinate within screen bounds."""
    lo_f = lo * FIXED_SCALE
    hi_f = hi * FIXED_SCALE
    span = hi_f - lo_f
    if val > hi_f:
        val -= span
    elif val < lo_f:
        val += span
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
        }

    def update_player_pos(self, player_id: int, x: int, y: int,
                          dx: int, dy: int, flags: int):
        """Update player position and velocity from SHIP_STATE."""
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

    def start_wave(self) -> tuple:
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
            a = self._randomize_asteroid(speed_increment)
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

    def _randomize_asteroid(self, speed_increment: int) -> dict:
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

        size = random.randint(0, DISASTEROID_SIZE_MAX - 1)
        atype = random.randint(0, NUM_DISASTEROID_VARIATIONS - 1)

        return {
            "x": x, "y": y, "dx": dx, "dy": dy,
            "size": size, "type": atype, "alive": True,
        }

    def tick(self) -> list:
        """Run one server tick (~50ms). Returns list of events to broadcast.

        Runs TICK_RATIO sub-steps per tick (one per Saturn frame) so that
        collision detection effectively operates at 60Hz, preventing fast
        objects from tunneling through each other.
        """
        events = []

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
                p["x"] = _wrap_coord(p["x"], SCREEN_MIN_X, SCREEN_MAX_X)
                p["y"] = _wrap_coord(p["y"], SCREEN_MIN_Y, SCREEN_MAX_Y)

            # Advance asteroid positions by 1 frame
            for i, a in enumerate(self.asteroids):
                if a is None or not a["alive"]:
                    continue
                a["x"] += a["dx"]
                a["y"] += a["dy"]
                a["x"] = _wrap_coord(a["x"], SCREEN_MIN_X, SCREEN_MAX_X)
                a["y"] = _wrap_coord(a["y"], SCREEN_MIN_Y, SCREEN_MAX_Y)

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
                    if _circle_collision(ax, ay, ar, px, py, PLAYER_SHIP_RADIUS):
                        kill_evt = self._kill_player(pid, i)
                        if kill_evt:
                            events.append(kill_evt)
                        destroy_evt = self._destroy_asteroid(i, 0xFF)
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

    def handle_asteroid_hit(self, slot: int, scorer_id: int):
        """Handle ASTEROID_HIT from a client. Returns destroy event or None."""
        if slot < 0 or slot >= MAX_DISASTEROIDS:
            return None
        a = self.asteroids[slot]
        if a is None or not a["alive"]:
            return None
        result = self._destroy_asteroid(slot, scorer_id)
        if result is not None and scorer_id != 0xFF and scorer_id < MAX_PLAYERS:
            self.scores[scorer_id] = self.scores.get(scorer_id, 0) + 1
        return result

    def _destroy_asteroid(self, slot: int, scorer_id: int):
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
                child = self._randomize_asteroid(0x100 * self.wave)
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

    def send_raw(self, data: bytes) -> bool:
        try:
            self.socket.sendall(data)
            return True
        except OSError:
            return False


# ==========================================================================
# Disasteroids Server
# ==========================================================================


class DisasteroidsServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 4822,
                 num_bots: int = 0):
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

            # Game simulation tick
            if self.game_active and self.sim and not self.game_paused:
                if now - self._last_tick >= self._tick_interval:
                    self._last_tick = now
                    self._game_tick()

            # Periodic tasks
            self._check_timeouts(now)

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
            self.sim.update_player_pos(player_id, x, y, dx, dy, flags)

        # Relay as SHIP_SYNC to all other clients
        sync_msg = build_ship_sync_raw(player_id, raw_data)
        for s, c in self.clients.items():
            if c.in_game and s != sock:
                c.send_raw(sync_msg)

    def _handle_asteroid_hit(self, sock, client: ClientInfo, payload: bytes):
        if not self.game_active or not client.in_game or not self.sim:
            return
        if len(payload) < 3:
            return

        slot = payload[1]
        scorer_id = payload[2]

        evt = self.sim.handle_asteroid_hit(slot, scorer_id)
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
            destroy_evt = self.sim._destroy_asteroid(slot, 0xFF)
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

        events = self.sim.tick()
        for evt in events:
            if evt[0] == "wave_over":
                self._start_new_wave()
            else:
                self._broadcast_event(evt)

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
                                           bot.dx, bot.dy, flags)

            # Delta compression for bot input relay
            bot.force_send_counter += 1
            if bits != bot.last_sent_bits or bot.force_send_counter >= 15:
                relay_msg = build_input_relay(bot.game_player_id,
                                              bot.ai.frame & 0xFFFF, bits)
                self._broadcast_to_game(relay_msg)
                bot.last_sent_bits = bits
                bot.force_send_counter = 0

            # Relay bot ship state periodically (every ~10 frames = 0.5s at 20 tick rate)
            if True:  # send every tick (~3 Saturn frames) for smooth bot movement
                sync_msg = build_ship_sync(
                    bot.game_player_id, bot.x, bot.y,
                    bot.dx, bot.dy, bot.rot & 0x7FFF, flags)
                self._broadcast_to_game(sync_msg)

    def _start_new_wave(self):
        """Start a new wave: generate asteroids + player spawns."""
        if not self.sim:
            return

        wave, asteroid_data, player_spawns = self.sim.start_wave()

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
            self._update_leaderboard(winner)
            self._broadcast_leaderboard()

    def _broadcast_to_game(self, msg: bytes):
        """Send a message to all in-game clients."""
        for s, c in self.clients.items():
            if c.in_game:
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


# ==========================================================================
# CLI
# ==========================================================================


def main():
    parser = argparse.ArgumentParser(description="Disasteroids NetLink Game Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=4822, help="Bind port")
    parser.add_argument("--bots", type=int, default=0,
                        help="Number of server-side bot players (0-11)")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    server = DisasteroidsServer(host=args.host, port=args.port,
                                num_bots=args.bots)
    if args.bots > 0:
        log.info("Starting with %d bot(s): %s", args.bots,
                 ", ".join(b.name for b in server.bots))
    try:
        server.start()
    except KeyboardInterrupt:
        log.info("Server shutting down")


if __name__ == "__main__":
    main()
