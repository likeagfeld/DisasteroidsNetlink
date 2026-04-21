#!/usr/bin/env python3
"""
Flicky's Flock NetLink Game Server

Manages online multiplayer for Flicky's Flock. Architecture follows the
Disasteroids/Coup server pattern: bridge-authenticated connections, SNCP
binary framing, lobby management, and server-authoritative state sync.

Networking model: SERVER-AUTHORITATIVE
  Server owns pipe/powerup spawning, collision detection, scoring, and
  game-over conditions.
  Each Saturn sends its local player inputs and player state.
  Server detects pipe passes (scoring), pipe/ground collisions (deaths),
  and powerup pickups.
  Saturns run local physics for smooth rendering; server corrects periodically.

Game constants match main.h exactly:
  FALLING_CONSTANT = 1
  FLAP_Y_SPEED = -15
  MAX_Y_SPEED = 20
  GROUND_COLLISION = 50
  SCREEN_TOP = -120
  VICTORY_CONDITION = 100

Usage:
    python3 tools/flock_server/fserver.py
    python3 tools/flock_server/fserver.py --port 4824 --bots 2
"""

import argparse
import json
import logging
import os
import random
import select
import base64
import queue
import socket
import struct
import sys
import threading
import time
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("flock_server")

# ==========================================================================
# Constants
# ==========================================================================

HEARTBEAT_TIMEOUT = 60.0
MAX_RECV_BUFFER = 8192
USERNAME_MAX_LEN = 16
UUID_LEN = 36

# Bridge authentication
SHARED_SECRET = b"SaturnFlickysFlock2026!NetLink#Key"
AUTH_MAGIC = b"AUTH"
AUTH_OK = 0x01
AUTH_TIMEOUT = 5.0

MAX_BRIDGES = 10
MAX_PLAYERS = 12
MAX_PIPES = 6
MAX_POWER_UPS = 3

# SNCP Auth Messages
MSG_CONNECT = 0x01
MSG_SET_USERNAME = 0x02
MSG_HEARTBEAT = 0x04
MSG_DISCONNECT = 0x05

MSG_USERNAME_REQUIRED = 0x81
MSG_WELCOME = 0x82
MSG_WELCOME_BACK = 0x83
MSG_USERNAME_TAKEN = 0x84

# Flicky's Flock Messages -- Client -> Server
FNET_MSG_READY = 0x10
FNET_MSG_INPUT_STATE = 0x11
FNET_MSG_START_GAME_REQ = 0x12
FNET_MSG_PAUSE_REQ = 0x13
FNET_MSG_PLAYER_STATE = 0x14
FNET_MSG_SPRITE_SELECT = 0x15
FNET_MSG_BOT_ADD = 0x16
FNET_MSG_BOT_REMOVE = 0x17
FNET_MSG_ADD_LOCAL_PLAYER = 0x18
FNET_MSG_REMOVE_LOCAL_PLAYER = 0x19
FNET_MSG_INPUT_STATE_P2 = 0x1A
FNET_MSG_LEADERBOARD_REQ = 0x1B
FNET_MSG_CLIENT_DEATH = 0x1C
FNET_MSG_CLIENT_DEATH_P2 = 0x1D
FNET_MSG_CLIENT_POWERUP_COLLECT = 0x1E

# Flicky's Flock Messages -- Server -> Client
FNET_MSG_LOBBY_STATE = 0xA0
FNET_MSG_GAME_START = 0xA1
FNET_MSG_INPUT_RELAY = 0xA2
FNET_MSG_PLAYER_JOIN = 0xA3
FNET_MSG_PLAYER_LEAVE = 0xA4
FNET_MSG_GAME_OVER = 0xA5
FNET_MSG_LOG = 0xA6
FNET_MSG_PAUSE_ACK = 0xA7
FNET_MSG_PLAYER_SYNC = 0xA9
FNET_MSG_PIPE_SPAWN = 0xAA
FNET_MSG_POWERUP_SPAWN = 0xAC
FNET_MSG_PLAYER_KILL = 0xAE
FNET_MSG_PLAYER_SPAWN = 0xAF
FNET_MSG_SCORE_UPDATE = 0xB0
FNET_MSG_POWERUP_EFFECT = 0xB1
FNET_MSG_LEADERBOARD_DATA = 0xB2
FNET_MSG_LOCAL_PLAYER_ACK = 0xB3

# Input bitmask (matches flock_protocol.h)
INPUT_FLAP = 1 << 0
INPUT_LTRIG = 1 << 1
INPUT_RTRIG = 1 << 2
INPUT_START = 1 << 3

# Flicky states (matches main.h)
FLICKYSTATE_UNINITIALIZED = 0
FLICKYSTATE_FLYING = 1
FLICKYSTATE_DYING = 2
FLICKYSTATE_DEAD = 3

# Game constants (matching main.h exactly)
FALLING_CONSTANT = 1
FLAP_Y_SPEED = -15
MAX_Y_SPEED = 20
GROUND_COLLISION = 50
SCREEN_TOP = -120
SCREEN_BOTTOM = 51
SCREEN_LEFT = -160
SCREEN_RIGHT = 160
VICTORY_CONDITION = 100

# Pipe constants (matching Saturn main.c initPipe() exactly)
PIPE_SPEED_BASE = 256  # fixed-point 8.8: 256 = 1.0 pixel/frame
PIPE_SPEED_PER_GATE = 64  # +0.25 per gate passed (~25% per gate)
PIPE_SPAWN_X = 256     # spawn off right edge (matching Saturn getNextPipePosition)
PIPE_NUM_SECTIONS = 10 # always 10 sections (matching Saturn initPipe)

# Powerup constants
POWERUP_ONE_UP = 0
POWERUP_REVERSE_GRAVITY = 1
POWERUP_LIGHTNING = 2
POWERUP_ROBOTNIK = 3
POWERUP_STONE_SNEAKERS = 4
NUM_POWER_UPS = 5
POWERUP_SPAWN_X = 180

# Collision box dimensions (matching Saturn-side constants)
FLICKY_WIDTH = 16
FLICKY_HEIGHT = 16
PIPE_WIDTH = 32
POWERUP_SIZE = 16

# Timers
SPAWN_FRAME_TIMER = 150
FLICKY_DEATH_FRAME_TIMER = 90
PIPE_SPAWN_INTERVAL_BASE = 180    # frames between pipe spawns (decreases with difficulty)
POWERUP_SPAWN_INTERVAL = 600      # frames between powerup spawns
ALL_DEAD_FRAME_TIMER = 150

# Bot names
BOT_NAMES = [
    "FLICKY", "CHIRPY", "TWITTER", "PECKY",
    "CUCKY", "POCKY", "ROCKY", "RICKY",
    "PICKY", "NICKY", "DICKY", "TICKY",
]


# ==========================================================================
# SNCP Framing
# ==========================================================================

def _clamp16(v: int) -> int:
    """Clamp an integer to signed 16-bit range for struct.pack('!h')."""
    if v > 32767:
        return 32767
    if v < -32768:
        return -32768
    return v


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
    payload = bytes([FNET_MSG_LOBBY_STATE, count])
    for p in players[:count]:
        payload += struct.pack("B", p["id"])
        payload += encode_lp_string(p["name"])
        payload += struct.pack("B", 1 if p["ready"] else 0)
        payload += struct.pack("B", p.get("sprite_id", 0) & 0xFF)
    return encode_frame(payload)


def build_game_start(seed: int, player_id: int, opponent_count: int,
                     num_lives: int, start_pos: int) -> bytes:
    """[seed:4 BE][my_player_id:1][opponent_count:1][num_lives:1][start_pos:1]"""
    payload = bytes([FNET_MSG_GAME_START])
    payload += struct.pack("!I", seed & 0xFFFFFFFF)
    payload += bytes([player_id, opponent_count, num_lives, start_pos])
    return encode_frame(payload)


def build_input_relay(player_id: int, frame_num: int,
                      input_bits: int) -> bytes:
    """[player_id:1][frame:2 BE][input:1]"""
    payload = bytes([FNET_MSG_INPUT_RELAY])
    payload += bytes([player_id])
    payload += struct.pack("!H", frame_num & 0xFFFF)
    payload += bytes([input_bits & 0xFF])
    return encode_frame(payload)


def build_player_join(player_id: int, name: str,
                      sprite_id: int = 0) -> bytes:
    payload = bytes([FNET_MSG_PLAYER_JOIN, player_id])
    payload += encode_lp_string(name)
    payload += struct.pack("B", sprite_id & 0xFF)
    return encode_frame(payload)


def build_player_leave(player_id: int) -> bytes:
    return encode_frame(bytes([FNET_MSG_PLAYER_LEAVE, player_id]))


def build_game_over(winner_id: int) -> bytes:
    return encode_frame(bytes([FNET_MSG_GAME_OVER, winner_id]))


def build_log(text: str) -> bytes:
    raw = text.encode("utf-8")[:255]
    payload = bytes([FNET_MSG_LOG, len(raw)]) + raw
    return encode_frame(payload)


def build_pause_ack(paused: bool) -> bytes:
    return encode_frame(bytes([FNET_MSG_PAUSE_ACK, 1 if paused else 0]))


def build_player_sync(player_id: int, y: int, y_speed: int,
                      state: int, points: int, deaths: int,
                      sprite: int) -> bytes:
    """[player_id:1][y:2s][y_speed:2s][state:1][points:2][deaths:2][sprite:1]"""
    payload = bytes([FNET_MSG_PLAYER_SYNC, player_id & 0xFF])
    payload += struct.pack("!hh", _clamp16(y), _clamp16(y_speed))
    payload += bytes([state & 0xFF])
    payload += struct.pack("!HH", points & 0xFFFF, deaths & 0xFFFF)
    payload += bytes([sprite & 0xFF])
    return encode_frame(payload)


def build_pipe_spawn(slot: int, x: int, y: int, gap: int,
                     sections: int, top_y: int) -> bytes:
    """[slot:1][x:2][y:2s][gap:1][sections:1][top_y:2s]"""
    payload = bytes([FNET_MSG_PIPE_SPAWN, slot & 0xFF])
    payload += struct.pack("!h", _clamp16(x))
    payload += struct.pack("!h", _clamp16(y))
    payload += bytes([gap & 0xFF, sections & 0xFF])
    payload += struct.pack("!h", _clamp16(top_y))
    return encode_frame(payload)


def build_powerup_spawn(slot: int, pu_type: int, x: int, y: int) -> bytes:
    """[slot:1][type:1][x:2][y:2s]"""
    payload = bytes([FNET_MSG_POWERUP_SPAWN, slot & 0xFF])
    payload += bytes([pu_type & 0xFF])
    payload += struct.pack("!h", _clamp16(x))
    payload += struct.pack("!h", _clamp16(y))
    return encode_frame(payload)


def build_player_kill(player_id: int) -> bytes:
    """[player_id:1]"""
    return encode_frame(bytes([FNET_MSG_PLAYER_KILL, player_id & 0xFF]))


def build_player_spawn(player_id: int) -> bytes:
    """[player_id:1]"""
    return encode_frame(bytes([FNET_MSG_PLAYER_SPAWN, player_id & 0xFF]))


def build_score_update(player_id: int, points: int, deaths: int) -> bytes:
    """[player_id:1][points:2][deaths:2]"""
    payload = bytes([FNET_MSG_SCORE_UPDATE, player_id & 0xFF])
    payload += struct.pack("!HH", points & 0xFFFF, deaths & 0xFFFF)
    return encode_frame(payload)


def build_powerup_effect(pu_type: int, picker_id: int) -> bytes:
    """[type:1][picker_id:1]"""
    return encode_frame(bytes([FNET_MSG_POWERUP_EFFECT,
                               pu_type & 0xFF, picker_id & 0xFF]))


def build_local_player_ack(player_id: int) -> bytes:
    """[player_id:1] - acknowledge P2 co-op registration."""
    return encode_frame(bytes([FNET_MSG_LOCAL_PLAYER_ACK, player_id & 0xFF]))


def build_leaderboard_data(entries: list) -> bytes:
    """Build LEADERBOARD_DATA message."""
    count = min(len(entries), 10)
    payload = bytes([FNET_MSG_LEADERBOARD_DATA, count])
    for e in entries[:count]:
        name_bytes = e["name"].encode("utf-8")[:16]
        payload += struct.pack("B", len(name_bytes)) + name_bytes
        payload += struct.pack("!HHH",
                               min(e.get("wins", 0), 65535),
                               min(e.get("best_score", 0), 65535),
                               min(e.get("games_played", 0), 65535))
    return encode_frame(payload)


# ==========================================================================
# Game Simulation (Server-Authoritative)
# ==========================================================================

class FlickyPlayer:
    """Server-side player state."""
    def __init__(self, player_id: int, num_lives: int):
        self.player_id = player_id
        self.state = FLICKYSTATE_FLYING
        self.y_pos = 0
        self.x_pos = 0
        self.y_speed = 0
        self.sprite_id = player_id % 12
        self.num_points = 0
        self.num_deaths = 0
        self.num_lives = num_lives  # 0 = infinite
        self.has_flapped = False
        self.spawn_timer = SPAWN_FRAME_TIMER
        self.death_timer = 0
        self.reverse_gravity_timer = 0
        self.lightning_timer = 0
        self.stone_sneakers_timer = 0
        self.last_input = 0
        self.prev_flap_input = False  # for debouncing flap press


class ServerPipe:
    """Server-side pipe state."""
    def __init__(self):
        self.active = False
        self.x_pos = 0
        self.y_pos = 0
        self.top_y_pos = 0
        self.gap = 0
        self.num_sections = 0
        self.scored_by = set()  # player IDs that already scored on this pipe
        self.speed_bumped = False  # True after first scorer bumped pipe speed


class ServerPowerUp:
    """Server-side powerup state."""
    def __init__(self):
        self.active = False
        self.type = 0
        self.x_pos = 0
        self.y_pos = 0


class GameSimulation:
    """Server-side Flicky's Flock game simulation."""

    TICK_RATE = 20   # Server ticks per second
    TICK_RATIO = 3   # 60fps / 20 ticks = 3 Saturn frames per tick

    def __init__(self, num_lives: int, start_pos: int, num_players: int,
                 game_seed: int = 0):
        self.num_lives = num_lives
        self.start_pos = start_pos
        self.num_players = num_players
        self.game_over = False
        self.game_frame = 0
        self.all_dead_timer = 0
        self.game_seed = game_seed

        # Players
        self.players = {}  # player_id -> FlickyPlayer

        # Pipes
        self.pipes = [ServerPipe() for _ in range(MAX_PIPES)]
        self.next_pipe_slot = 0

        # Powerups
        self.powerups = [ServerPowerUp() for _ in range(MAX_POWER_UPS)]
        self.powerup_spawn_timer = POWERUP_SPAWN_INTERVAL
        self.next_powerup_slot = 0

        # Progressive speed: fixed-point 8.8 (256 = 1.0 pixel/frame)
        self.pipe_speed = PIPE_SPEED_BASE
        self.pipe_speed_accum = 0

        # Track which player IDs are bots (collision is server-authoritative
        # only for bots; human players report their own deaths)
        self.bot_ids: set = set()

        # Starting positions (matching Saturn getStartingPosition exactly)
        self._starting_positions = list(range(num_players))
        if start_pos == 1:
            # Random shuffle using game_seed for determinism
            rng = random.Random(game_seed)
            rng.shuffle(self._starting_positions)

    def _get_starting_x(self, player_id: int) -> int:
        """Match Saturn getStartingPosition() x_pos calculation."""
        spacing = 28
        pid = self._starting_positions[player_id] if player_id < len(self._starting_positions) else player_id
        if pid < 6:
            return 0 - (pid * spacing)
        else:
            return 0 - ((pid - 5) * spacing) + spacing // 2

    def _get_starting_y(self, player_id: int) -> int:
        """Match Saturn getStartingPosition() y_pos calculation."""
        vertical_spacing = 8
        pid = self._starting_positions[player_id] if player_id < len(self._starting_positions) else player_id
        if pid < 6:
            return -25 + (pid * vertical_spacing)
        else:
            return -25 - ((pid - 5) * vertical_spacing)

    def init_player(self, player_id: int):
        """Register a player with correct starting position."""
        p = FlickyPlayer(player_id, self.num_lives)
        p.x_pos = self._get_starting_x(player_id)
        p.y_pos = self._get_starting_y(player_id)
        self.players[player_id] = p

    def get_top_score(self) -> int:
        """Get highest score among all players."""
        top = 0
        for p in self.players.values():
            if p.num_points > top:
                top = p.num_points
        return top

    def get_difficulty(self) -> int:
        """Calculate difficulty based on top score (matching Saturn getDifficulty).
        Difficulty = topScore / 10, clamped to 0-10."""
        diff = self.get_top_score() // 10
        if diff < 0:
            return 0
        if diff > 10:
            return 10
        return diff

    def get_pipe_spawn_interval(self) -> int:
        """Pipe spawn interval decreases with difficulty (matching Saturn spacing)."""
        diff = self.get_difficulty()
        # Matches Saturn: getNextPipePosition returns x_pos + (180 - 10*diff) + random(200 - 10*diff)
        # At speed ~1px/frame, distance translates to frames
        base = 180 - (10 * diff)
        jitter = random.randint(0, max(1, 200 - (10 * diff)))
        return max(60, base + jitter)

    def get_pipe_gap(self) -> int:
        """Pipe gap matches Saturn: 48 + random(40), scaled by difficulty.
        Gap shrinks slightly as difficulty increases."""
        diff = self.get_difficulty()
        base_gap = 48 + random.randint(0, 40)
        # Reduce gap slightly at higher difficulties
        reduction = min(diff * 3, 20)
        return max(40, base_gap - reduction)

    def _get_rightmost_pipe_x(self) -> int:
        """Find the x position of the rightmost active pipe."""
        x = 0
        for pipe in self.pipes:
            if pipe.active and pipe.x_pos > x:
                x = pipe.x_pos
        return x

    def _get_next_pipe_x(self) -> int:
        """Calculate next pipe X position relative to rightmost pipe.
        Matches Saturn getNextPipePosition() exactly."""
        rightmost = self._get_rightmost_pipe_x()
        if rightmost <= 0:
            return PIPE_SPAWN_X  # 256 - same as Saturn
        diff = self.get_difficulty()
        return rightmost + (180 - 10 * diff) + random.randint(0, max(1, 200 - 10 * diff))

    def spawn_pipe(self, x_override: int = None) -> tuple:
        """Spawn a new pipe. Returns (slot, pipe_data) or None.
        Matches Saturn initPipe() exactly:
          y_pos = -20 + random(60)           -> bottom pipe Y
          numSections = 10                    -> always 10
          gap = 48 + random(40)              -> gap between halves
          top_y_pos = y_pos - gap - numSections*16  -> top pipe start
        """
        # Find free slot
        slot = -1
        for i in range(MAX_PIPES):
            idx = (self.next_pipe_slot + i) % MAX_PIPES
            if not self.pipes[idx].active:
                slot = idx
                break
        if slot < 0:
            return None

        self.next_pipe_slot = (slot + 1) % MAX_PIPES

        pipe = self.pipes[slot]
        pipe.active = True
        pipe.x_pos = x_override if x_override is not None else self._get_next_pipe_x()
        pipe.y_pos = -20 + random.randint(0, 60)  # matching Saturn: -20 + jo_random(60)
        pipe.num_sections = PIPE_NUM_SECTIONS      # always 10
        pipe.gap = self.get_pipe_gap()
        # Matching Saturn: top_y_pos = y_pos - gap - numSections*16
        pipe.top_y_pos = pipe.y_pos - pipe.gap - pipe.num_sections * 16

        # Clamp top_y_pos like Saturn does (prevents top pipe from going too far up)
        if pipe.top_y_pos < -220:
            diff = pipe.top_y_pos - (-220)
            pipe.y_pos -= diff
            pipe.top_y_pos = -220

        pipe.scored_by = set()
        pipe.speed_bumped = False

        return (slot, pipe.x_pos, pipe.y_pos, pipe.gap,
                pipe.num_sections, pipe.top_y_pos)

    def _init_pipes(self) -> list:
        """Pre-spawn all 6 pipes at game start, matching offline behavior.
        Returns list of pipe_spawn event tuples for broadcasting."""
        events = []
        # First pipe at x=160 (slightly off-screen right)
        x = 160
        diff = self.get_difficulty()
        for i in range(MAX_PIPES):
            result = self.spawn_pipe(x_override=x)
            if result:
                events.append(("pipe_spawn",) + result)
            # Next pipe offset from this one (matching getNextPipePosition)
            base = 180 - 10 * diff
            jitter = random.randint(0, max(1, 200 - 10 * diff))
            x = x + base + jitter
        return events

    def spawn_powerup(self) -> tuple:
        """Spawn a new powerup. Returns (slot, type, x, y) or None."""
        slot = -1
        for i in range(MAX_POWER_UPS):
            idx = (self.next_powerup_slot + i) % MAX_POWER_UPS
            if not self.powerups[idx].active:
                slot = idx
                break
        if slot < 0:
            return None

        self.next_powerup_slot = (slot + 1) % MAX_POWER_UPS

        pu = self.powerups[slot]
        pu.active = True
        pu.type = random.randint(0, NUM_POWER_UPS - 1)
        pu.x_pos = POWERUP_SPAWN_X
        pu.y_pos = random.randint(-40, 30)

        return (slot, pu.type, pu.x_pos, pu.y_pos)

    def tick(self) -> list:
        """Run one server tick. Returns list of events to broadcast."""
        events = []

        if self.game_over:
            return events

        # Calculate movement per sub-step using progressive speed
        # pipe_speed is fixed-point 8.8 (256 = 1.0 pixel/frame)
        self.pipe_speed_accum += self.pipe_speed * self.TICK_RATIO
        pixels_to_move = self.pipe_speed_accum >> 8
        self.pipe_speed_accum &= 0xFF

        # Sub-step loop for collision accuracy
        for _step in range(self.TICK_RATIO):
            self.game_frame += 1

            # Per-step pixel movement (distribute pixels across sub-steps)
            step_move = 0
            if _step == 0:
                step_move = pixels_to_move  # apply all movement on first sub-step

            # Update player physics
            for pid, p in self.players.items():
                if p.state == FLICKYSTATE_DYING:
                    p.death_timer -= 1
                    if p.death_timer <= 0:
                        p.state = FLICKYSTATE_DEAD
                    continue

                if p.state == FLICKYSTATE_DEAD:
                    continue

                if p.state != FLICKYSTATE_FLYING:
                    continue

                # Spawn protection countdown
                if p.spawn_timer > 0:
                    p.spawn_timer -= 1
                    # Auto-set has_flapped when spawn timer expires
                    # (matching Saturn: spawnFrameTimer >= SPAWN_FRAME_TIMER)
                    if p.spawn_timer <= 0:
                        p.has_flapped = True

                # Debounced flap: only trigger on press (0->1 transition)
                current_flap = bool(p.last_input & INPUT_FLAP)
                flap_pressed = current_flap and not p.prev_flap_input
                p.prev_flap_input = current_flap

                # Apply gravity and movement
                if p.has_flapped:
                    # Apply flap on press only (debounced)
                    if flap_pressed:
                        flap_speed = FLAP_Y_SPEED
                        # Stone sneakers make jumps heavier
                        if p.stone_sneakers_timer > 0:
                            flap_speed += 4
                        # Lightning makes jumps floatier
                        if p.lightning_timer > 0:
                            flap_speed -= 3
                        # Reverse gravity swaps direction
                        if p.reverse_gravity_timer > 0:
                            flap_speed *= -1
                        p.y_speed = flap_speed

                    # Gravity (matching Saturn applyPlayerPhysics exactly)
                    if p.reverse_gravity_timer > 0:
                        p.y_pos += p.y_speed // 5
                        p.y_speed -= FALLING_CONSTANT
                    else:
                        p.y_pos += p.y_speed // 5
                        p.y_speed += FALLING_CONSTANT

                    # Clamp speed (matching Saturn)
                    if p.y_speed > MAX_Y_SPEED:
                        p.y_speed = MAX_Y_SPEED
                    if p.reverse_gravity_timer > 0:
                        if p.y_speed < -MAX_Y_SPEED:
                            p.y_speed = -MAX_Y_SPEED

                # Clamp position (matching Saturn)
                if p.y_pos < SCREEN_TOP:
                    p.y_pos = SCREEN_TOP
                if p.y_pos > SCREEN_BOTTOM:
                    p.y_pos = SCREEN_BOTTOM

                # Powerup timers
                if p.reverse_gravity_timer > 0:
                    p.reverse_gravity_timer -= 1
                    if p.reverse_gravity_timer == 0:
                        p.y_speed = 0  # matching Saturn: zero speed on gravity reset
                if p.lightning_timer > 0:
                    p.lightning_timer -= 1
                if p.stone_sneakers_timer > 0:
                    p.stone_sneakers_timer -= 1

                # Ground collision -- only for bots (humans report own death)
                if pid in self.bot_ids:
                    if p.y_pos > GROUND_COLLISION and p.has_flapped:
                        if p.spawn_timer <= 0:
                            events.append(("player_kill", pid))

            # Move pipes left (using progressive speed)
            for i, pipe in enumerate(self.pipes):
                if not pipe.active:
                    continue
                if _step == 0:
                    pipe.x_pos -= pixels_to_move

                # Remove pipes that go off-screen left
                if pipe.x_pos < -256:
                    pipe.active = False

            # Move powerups left (same speed as pipes)
            for i, pu in enumerate(self.powerups):
                if not pu.active:
                    continue
                if _step == 0:
                    pu.x_pos -= pixels_to_move

                if pu.x_pos < -256:
                    pu.active = False

            # Check pipe collisions and scoring
            for pid, p in self.players.items():
                if p.state != FLICKYSTATE_FLYING:
                    continue
                if not p.has_flapped:
                    continue
                if p.spawn_timer > 0:
                    continue

                for i, pipe in enumerate(self.pipes):
                    if not pipe.active:
                        continue

                    # Scoring: player passed the pipe (pipe x < player x)
                    if (pipe.x_pos < p.x_pos and
                            pid not in pipe.scored_by):
                        pipe.scored_by.add(pid)
                        p.num_points += 1
                        events.append(("score_update", pid,
                                        p.num_points, p.num_deaths))
                        # Progressive speed: increase once per pipe (first scorer only)
                        if not pipe.speed_bumped:
                            pipe.speed_bumped = True
                            self.pipe_speed += PIPE_SPEED_PER_GATE

                    # Collision with pipe -- only for bots
                    if pid in self.bot_ids:
                        if self._check_pipe_collision(p, pipe):
                            events.append(("player_kill", pid))

            # Check powerup collisions (bots only; humans report via CLIENT_POWERUP_COLLECT)
            for pid, p in self.players.items():
                if pid not in self.bot_ids:
                    continue
                if p.state != FLICKYSTATE_FLYING:
                    continue
                if not p.has_flapped:
                    continue

                for i, pu in enumerate(self.powerups):
                    if not pu.active:
                        continue
                    if self._check_powerup_collision(p, pu):
                        pu.active = False
                        events.append(("powerup_effect", pu.type, pid))

        # Process accumulated kills (deduplicate)
        killed_pids = set()
        final_events = []
        for evt in events:
            if evt[0] == "player_kill":
                pid = evt[1]
                if pid not in killed_pids:
                    killed_pids.add(pid)
                    self._kill_player(pid)
                    final_events.append(evt)
            else:
                final_events.append(evt)

        # Pipe spawning: recycle-based (spawn into any inactive slot)
        for i in range(MAX_PIPES):
            if not self.pipes[i].active:
                result = self.spawn_pipe()
                if result:
                    final_events.append(("pipe_spawn",) + result)

        # Powerup spawning
        self.powerup_spawn_timer -= self.TICK_RATIO
        if self.powerup_spawn_timer <= 0:
            self.powerup_spawn_timer = POWERUP_SPAWN_INTERVAL
            result = self.spawn_powerup()
            if result:
                final_events.append(("powerup_spawn",) + result)

        # Victory check
        for pid, p in self.players.items():
            if p.num_points >= VICTORY_CONDITION:
                self.game_over = True
                final_events.append(("game_over", pid))
                return final_events

        # All-dead check
        any_alive = any(p.state == FLICKYSTATE_FLYING
                        for p in self.players.values())
        any_can_respawn = any(
            (p.state in (FLICKYSTATE_DYING, FLICKYSTATE_DEAD) and
             (p.num_lives == 0 or p.num_deaths < p.num_lives))
            for p in self.players.values()
        )

        if not any_alive and not any_can_respawn:
            self.all_dead_timer += self.TICK_RATIO
            if self.all_dead_timer >= ALL_DEAD_FRAME_TIMER:
                self.game_over = True
                # Winner is player with highest score
                best_pid = 0xFF
                best_score = -1
                for pid, p in self.players.items():
                    score = p.num_points - p.num_deaths
                    if score > best_score:
                        best_score = score
                        best_pid = pid
                final_events.append(("game_over", best_pid))
        elif not any_alive and any_can_respawn:
            # Respawn players that still have lives
            self.all_dead_timer += self.TICK_RATIO
            if self.all_dead_timer >= ALL_DEAD_FRAME_TIMER:
                self.all_dead_timer = 0
                for pid, p in self.players.items():
                    if p.state in (FLICKYSTATE_DYING, FLICKYSTATE_DEAD):
                        if p.num_lives == 0 or p.num_deaths < p.num_lives:
                            self._respawn_player(pid)
                            final_events.append(("player_spawn", pid))
        else:
            self.all_dead_timer = 0

        # Per-player respawn (if still has lives and been dead long enough)
        for pid, p in self.players.items():
            if p.state == FLICKYSTATE_DEAD:
                if p.num_lives == 0 or p.num_deaths < p.num_lives:
                    p.death_timer -= self.TICK_RATIO
                    if p.death_timer <= -SPAWN_FRAME_TIMER:
                        self._respawn_player(pid)
                        final_events.append(("player_spawn", pid))

        return final_events

    def _kill_player(self, player_id: int):
        """Kill a player."""
        p = self.players.get(player_id)
        if not p or p.state != FLICKYSTATE_FLYING:
            return
        p.state = FLICKYSTATE_DYING
        p.death_timer = FLICKY_DEATH_FRAME_TIMER
        p.num_deaths += 1
        p.y_speed = 0

    def _respawn_player(self, player_id: int):
        """Respawn a dead player at their original x position."""
        p = self.players.get(player_id)
        if not p:
            return
        p.state = FLICKYSTATE_FLYING
        p.y_pos = self._get_starting_y(player_id) if player_id < len(self._starting_positions) else random.randint(-50, 0)
        p.y_speed = 0
        p.has_flapped = False
        p.prev_flap_input = False
        p.spawn_timer = SPAWN_FRAME_TIMER
        p.death_timer = 0
        p.reverse_gravity_timer = 0
        p.lightning_timer = 0
        p.stone_sneakers_timer = 0

    def _check_pipe_collision(self, player: FlickyPlayer,
                               pipe: ServerPipe) -> bool:
        """Check if player collides with pipe, matching Saturn AABB exactly.
        Saturn checkForFlickyPipeCollisions():
          playerWidth=12, playerHeight=12
          pipeWidth=58, pipeHeight=16*numSections
          pl_x = x_pos - 6, pl_y = y_pos - 6
          pi_x = pipe.x_pos - 25
          Bottom: pi_y = pipe.y_pos - 8
          Top: pi_y = pipe.top_y_pos - 8
        """
        player_w = 12
        player_h = 12
        pipe_w = 58
        pipe_h = 16 * pipe.num_sections

        # Lightning shrink
        if player.lightning_timer > 0:
            player_w -= 2
            player_h -= 2

        pl_x = player.x_pos - 6
        pl_y = player.y_pos - 6
        pi_x = pipe.x_pos - 25

        # Check horizontal overlap first
        if pl_x >= pi_x + pipe_w or pl_x + player_w <= pi_x:
            return False

        # Bottom pipe (pi_y = y_pos - 8, extends down by pipe_h)
        pi_y_bot = pipe.y_pos - 8
        if (pl_y < pi_y_bot + pipe_h and pl_y + player_h > pi_y_bot):
            return True

        # Top pipe (pi_y = top_y_pos - 8, extends down by pipe_h)
        pi_y_top = pipe.top_y_pos - 8
        if (pl_y < pi_y_top + pipe_h and pl_y + player_h > pi_y_top):
            return True

        return False

    def _check_powerup_collision(self, player: FlickyPlayer,
                                  pu: ServerPowerUp) -> bool:
        """Check if a player collides with a powerup."""
        px = player.x_pos if player.x_pos != 0 else (SCREEN_LEFT + 40)
        py = player.y_pos
        dist_x = abs(px - pu.x_pos)
        dist_y = abs(py - pu.y_pos)
        return dist_x < (FLICKY_WIDTH // 2 + POWERUP_SIZE // 2) and \
               dist_y < (FLICKY_HEIGHT // 2 + POWERUP_SIZE // 2)

    def update_player_from_client(self, player_id: int, y: int,
                                   y_speed: int, state: int,
                                   sprite: int):
        """Update server state from client PLAYER_STATE message."""
        p = self.players.get(player_id)
        if not p:
            return
        # Trust client position for smooth sync but server is authoritative on state
        p.y_pos = y
        p.y_speed = y_speed
        p.sprite_id = sprite % 12


# ==========================================================================
# Bot AI
# ==========================================================================

class BotAI:
    """Simple Flappy Bird bot -- flaps to stay above pipes."""

    def __init__(self):
        self.frame = 0

    def tick(self, y_pos: int, y_speed: int, pipes: list) -> int:
        """Returns input bits for this frame."""
        self.frame += 1
        bits = 0

        # Find the nearest active pipe ahead of the player
        target_y = -20  # default: aim for middle-ish

        nearest_x = 9999
        for pipe in pipes:
            if not pipe.active:
                continue
            if pipe.x_pos > SCREEN_LEFT + 20 and pipe.x_pos < nearest_x:
                nearest_x = pipe.x_pos
                # Aim for the middle of the gap
                target_y = pipe.top_y_pos + (pipe.y_pos - pipe.top_y_pos) // 2

        # Flap if below target or falling too fast
        if y_pos > target_y - 5:
            bits |= INPUT_FLAP
        elif y_speed > 3:
            bits |= INPUT_FLAP

        # Don't flap every frame -- add some jitter for realism
        if self.frame % 3 == 0 and y_pos < target_y - 15:
            bits = 0

        return bits


class BotPlayer:
    """Virtual bot player -- no socket, runs inside server."""

    def __init__(self, name: str, bot_id: int):
        self.name = name
        self.bot_id = bot_id
        self.ready = True
        self.in_game = False
        self.game_player_id = 0
        self.sprite_id = bot_id % 12  # Bots get sequential sprites
        self.ai = BotAI()
        self.last_sent_bits = -1
        self.force_send_counter = 0

    def reset_for_game(self):
        self.in_game = True
        self.ai = BotAI()
        self.last_sent_bits = -1
        self.force_send_counter = 0


# ==========================================================================
# Admin Portal HTML + Handler
# ==========================================================================

ADMIN_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Flicky's Flock Admin</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#1a1a2e;color:#e0e0e0;font-family:-apple-system,system-ui,monospace;padding:12px;font-size:14px}
h1{color:#f5a623;margin-bottom:8px;font-size:20px}
h3{font-size:15px;margin-bottom:8px}
.info{color:#888;margin-bottom:12px;font-size:13px}
.panel{background:#16213e;padding:12px;border-radius:5px;margin:8px 0;overflow-x:auto}
table{width:100%;border-collapse:collapse;margin:6px 0;min-width:280px}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid #333;white-space:nowrap;font-size:13px}
th{background:#0f1a2e;color:#f5a623}
tr:hover{background:#1a2744}
.btn{background:#f5a623;color:#000;border:none;padding:8px 16px;cursor:pointer;font-family:inherit;font-size:13px;border-radius:3px;touch-action:manipulation;font-weight:bold}
.btn:active{opacity:0.7}
.btn-warn{background:#e94560;color:#fff}
.btn-danger{background:#d32f2f;color:#fff}
.status{display:inline-block;padding:2px 8px;border-radius:3px;font-size:12px}
.status-lobby{background:#2ecc71;color:#000}
.status-ingame{background:#3498db;color:#fff}
.status-dead{background:#7f8c8d;color:#fff}
#msg{position:fixed;top:10px;left:50%;transform:translateX(-50%);background:#2ecc71;color:#000;padding:10px 20px;border-radius:5px;display:none;font-weight:bold;z-index:9}
.cards{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}
.card{flex:1;min-width:80px;background:#0f1a2e;border-radius:4px;padding:8px;text-align:center}
.card-label{font-size:11px;color:#888;margin-bottom:2px}
.card-value{font-size:16px;font-weight:bold;color:#e0e0e0}
.tabs{display:flex;gap:4px;margin-bottom:8px}
.tab{padding:8px 16px;background:#0f1a2e;color:#888;border:none;cursor:pointer;font-family:inherit;font-size:13px;border-radius:3px 3px 0 0}
.tab.active{background:#16213e;color:#f5a623;font-weight:bold}
.tab-content{display:none}
.tab-content.active{display:block}
.player-row{background:#0f1a2e;border-radius:4px;padding:10px;margin:6px 0}
.player-name{font-weight:bold;font-size:15px;margin-bottom:4px}
.player-details{font-size:12px;color:#999;display:flex;flex-wrap:wrap;gap:8px;margin:4px 0}
.controls{display:flex;gap:10px;flex-wrap:wrap}
@media(max-width:699px){body{padding:8px}}
</style></head><body>
<h1>Flicky's Flock Admin</h1>
<div class="info">Next refresh: <span id="countdown">3</span>s | <span id="uptime">-</span> | <span id="status_dot" style="color:#2ecc71">&#9679;</span></div>
<div id="msg"></div>

<div class="tabs">
<button class="tab active" onclick="showTab('dashboard')">Dashboard</button>
<button class="tab" onclick="showTab('history')">Join History</button>
</div>

<div id="tab-dashboard" class="tab-content active">

<div class="panel">
<h3>Server Status</h3>
<div class="cards">
<div class="card"><div class="card-label">Game</div><div class="card-value" id="g_active">-</div></div>
<div class="card"><div class="card-label">Phase</div><div class="card-value" id="g_phase">-</div></div>
<div class="card"><div class="card-label">Players</div><div class="card-value" id="g_players">-</div></div>
<div class="card"><div class="card-label">Bots</div><div class="card-value" id="g_bots">-</div></div>
<div class="card"><div class="card-label">Pipes</div><div class="card-value" id="g_pipes">-</div></div>
<div class="card"><div class="card-label">Speed</div><div class="card-value" id="g_speed">-</div></div>
<div class="card"><div class="card-label">Total Joins</div><div class="card-value" id="g_total_joins">-</div></div>
</div></div>

<div class="panel">
<h3>Connected Players</h3>
<table><thead><tr><th>Name</th><th>Status</th><th>Score</th><th>Deaths</th><th>IP</th><th>Idle</th><th>Action</th></tr></thead>
<tbody id="ptable"></tbody></table>
</div>

<div class="panel">
<h3>Server Controls</h3>
<div class="controls">
<button class="btn btn-warn" onclick="endGame()">End Game</button>
<button class="btn btn-danger" onclick="restartServer()">Restart Server</button>
</div></div>

</div>

<div id="tab-history" class="tab-content">
<div class="panel">
<h3>Join History (Last 200)</h3>
<table><thead><tr><th>Time</th><th>Name</th><th>IP</th><th>Event</th></tr></thead>
<tbody id="htable"></tbody></table>
</div>
</div>

<script>
var REFRESH_SEC=3,countdown=REFRESH_SEC,BASE='';
(function(){var p=location.pathname;if(p.indexOf('/flickyadmin')===0)BASE='/flickyadmin/';else BASE='/'})();

function showTab(name){
  document.querySelectorAll('.tab').forEach(function(t){t.classList.remove('active')});
  document.querySelectorAll('.tab-content').forEach(function(t){t.classList.remove('active')});
  document.getElementById('tab-'+name).classList.add('active');
  document.querySelectorAll('.tab').forEach(function(t){if(t.textContent.toLowerCase().indexOf(name)>=0||
    (name==='dashboard'&&t.textContent==='Dashboard')||(name==='history'&&t.textContent==='Join History'))t.classList.add('active')});
  if(name==='history')loadHistory();
}
function showMsg(t,c){var m=document.getElementById('msg');m.textContent=t;m.style.background=c||'#2ecc71';m.style.display='block';setTimeout(function(){m.style.display='none'},3000)}
function api(method,path,body){
  var url=BASE+path;
  var opts={method:method};
  if(body){opts.headers={'Content-Type':'application/json'};opts.body=JSON.stringify(body)}
  return fetch(url,opts)
  .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()})
  .catch(function(e){document.getElementById('status_dot').style.color='#d32f2f';return{}})
}
function kick(uuid,name){if(confirm('Kick '+name+'?'))api('POST','api/kick',{uuid:uuid}).then(function(r){if(r.message)showMsg(r.message);refresh()})}
function endGame(){if(confirm('End the current game?'))api('POST','api/end_game').then(function(r){if(r.message)showMsg(r.message);refresh()})}
function restartServer(){if(confirm('RESTART the server? All connections will drop.'))api('POST','api/restart').then(function(r){if(r.message)showMsg(r.message,'#e94560')})}
function fmtTime(s){if(s<0)return'-';var m=Math.floor(s/60),sec=Math.floor(s%60);return m>0?m+'m '+sec+'s':sec+'s'}
function refresh(){
  countdown=REFRESH_SEC;
  api('GET','api/state').then(function(d){
    if(!d.game)return;
    document.getElementById('status_dot').style.color='#2ecc71';
    document.getElementById('uptime').textContent='Up '+fmtTime(d.uptime);
    var g=d.game;
    document.getElementById('g_active').textContent=g.active?'ACTIVE':'Lobby';
    document.getElementById('g_phase').textContent=g.active?'Playing':'Waiting';
    document.getElementById('g_players').textContent=g.human_count;
    document.getElementById('g_bots').textContent=g.bot_count;
    document.getElementById('g_pipes').textContent=g.active_pipes;
    document.getElementById('g_speed').textContent=g.pipe_speed;
    document.getElementById('g_total_joins').textContent=d.total_joins;
    var tb=document.getElementById('ptable');tb.innerHTML='';
    if(d.players.length===0){
      tb.innerHTML='<tr><td colspan="7" style="color:#888;text-align:center">No players connected</td></tr>';
    }
    d.players.forEach(function(p){
      var sc='status-lobby';
      if(p.status==='in-game')sc='status-ingame';
      else if(p.status==='dead')sc='status-dead';
      var tr=document.createElement('tr');
      var kb='<button class="btn" data-uuid="'+p.uuid+'" data-name="'+p.username+'">Kick</button>';
      tr.innerHTML='<td><b>'+p.username+'</b></td>'
        +'<td><span class="status '+sc+'">'+p.status+'</span></td>'
        +'<td>'+p.score+'</td><td>'+p.deaths+'</td>'
        +'<td>'+p.address+'</td>'
        +'<td>'+fmtTime(p.idle)+'</td>'
        +'<td>'+kb+'</td>';
      tb.appendChild(tr);
    })
  })
}
function loadHistory(){
  api('GET','api/history').then(function(d){
    if(!d.entries)return;
    var tb=document.getElementById('htable');tb.innerHTML='';
    d.entries.forEach(function(e){
      var tr=document.createElement('tr');
      tr.innerHTML='<td>'+e.time+'</td><td>'+e.name+'</td><td>'+e.ip+'</td><td>'+e.event+'</td>';
      tb.appendChild(tr);
    })
  })
}
function tick(){countdown--;if(countdown<=0)refresh();document.getElementById('countdown').textContent=Math.max(countdown,0)}
document.addEventListener('click',function(e){var b=e.target;if(b.tagName==='BUTTON'&&b.dataset.uuid){kick(b.dataset.uuid,b.dataset.name)}});
refresh();setInterval(tick,1000);
</script></body></html>"""


def _make_flock_admin_handler(server_ref):
    """Create an AdminHandler class bound to the FlockServer instance."""

    class FlockAdminHandler(BaseHTTPRequestHandler):
        flock_server = server_ref

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
            srv = self.flock_server
            if user != srv._admin_user or pwd != srv._admin_password:
                self._send_auth_required()
                return False
            return True

        def _send_auth_required(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Flicky Admin"')
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
                body = ADMIN_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True
            elif path == "/api/state":
                self._send_json(self._build_state())
            elif path == "/api/history":
                self._send_json(self._build_history())
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

            srv = self.flock_server
            if path == "/api/kick":
                target_uuid = data.get("uuid", "")
                if not target_uuid:
                    self._send_json({"error": "missing uuid"}, 400)
                    return
                srv._admin_command_queue.put({"cmd": "kick", "uuid": target_uuid})
                self._send_json({"message": "Kick queued"})
            elif path == "/api/end_game":
                srv._admin_command_queue.put({"cmd": "end_game"})
                self._send_json({"message": "End game queued"})
            elif path == "/api/restart":
                srv._admin_command_queue.put({"cmd": "restart"})
                self._send_json({"message": "Restart queued"})
            else:
                self.send_error(404)

        def _build_state(self):
            srv = self.flock_server
            now = time.time()
            players = []
            for sock, info in list(srv.clients.items()):
                if not info.authenticated:
                    continue
                if info.in_game:
                    sim_player = srv.sim.players.get(info.game_player_id) if srv.sim else None
                    if sim_player and sim_player.state in (FLICKYSTATE_DYING, FLICKYSTATE_DEAD):
                        status = "dead"
                    else:
                        status = "in-game"
                    score = sim_player.num_points if sim_player else 0
                    deaths = sim_player.num_deaths if sim_player else 0
                else:
                    status = "lobby"
                    score = 0
                    deaths = 0
                players.append({
                    "username": info.username,
                    "uuid": info.uuid,
                    "status": status,
                    "address": "%s:%d" % (info.address[0], info.address[1]),
                    "idle": round(now - info.last_activity, 1),
                    "ready": info.ready,
                    "score": score,
                    "deaths": deaths,
                })

            active_pipes = 0
            pipe_speed = "1.0"
            if srv.sim:
                active_pipes = sum(1 for p in srv.sim.pipes if p.active)
                pipe_speed = "%.1f" % (srv.sim.pipe_speed / 256.0)

            return {
                "uptime": round(now - srv._start_time, 1),
                "total_joins": len(srv._join_history),
                "players": players,
                "game": {
                    "active": srv.game_active,
                    "human_count": len([c for c in srv.clients.values() if c.authenticated]),
                    "bot_count": len(srv.bots),
                    "active_pipes": active_pipes,
                    "pipe_speed": pipe_speed,
                },
            }

        def _build_history(self):
            srv = self.flock_server
            entries = list(srv._join_history[-200:])
            entries.reverse()
            return {"entries": entries}

    return FlockAdminHandler


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
        self.game_player_id = 0
        self.sprite_id = 0  # Selected bird color (0-11)
        # P2 local co-op
        self.local_player_names = []   # names for additional local players
        self.local_player_ids = []     # game_player_ids for local players

    def send_raw(self, data: bytes) -> bool:
        try:
            self.socket.sendall(data)
            return True
        except OSError:
            return False


# ==========================================================================
# Flicky's Flock Server
# ==========================================================================

class FlockServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 4824,
                 num_bots: int = 0, admin_port: int = 0,
                 admin_user: str = "admin",
                 admin_password: str = "flock2026"):
        self.host = host
        self.port = port
        self.clients: dict = {}
        self.uuid_map: dict = {}
        self.server_socket = None
        self._running = False
        self._start_time = time.time()

        # Bridge auth
        self.pending_auth: dict = {}
        self.authenticated_bridges: set = set()

        # Game state
        self.game_active = False
        self.game_seed = 0
        self.game_paused = False
        self.num_lives = 3
        self.start_pos = 0  # 0=fixed, 1=random
        self.sim = None

        # Bots
        self.bots: list = []
        for i in range(num_bots):
            name = BOT_NAMES[i % len(BOT_NAMES)]
            self.bots.append(BotPlayer(name, i))

        # Leaderboard
        self.leaderboard = {}
        self._leaderboard_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "leaderboard.json")
        self._load_leaderboard()

        # Delta compression
        self.last_relayed_input = {}
        self.relay_cooldown = {}

        # Tick timer
        self._last_tick = 0.0
        self._tick_interval = 1.0 / GameSimulation.TICK_RATE

        # Admin portal
        self._admin_port = admin_port
        self._admin_user = admin_user
        self._admin_password = admin_password
        self._admin_command_queue = queue.Queue()
        self._admin_httpd = None
        self._admin_thread = None

        # Join history (persistent across server lifetime)
        self._join_history = []
        self._join_history_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "join_history.json")
        self._load_join_history()

    def _load_leaderboard(self):
        try:
            if os.path.exists(self._leaderboard_path):
                with open(self._leaderboard_path, "r") as f:
                    data = json.load(f)
                self.leaderboard = data.get("players", {})
                log.info("Loaded leaderboard: %d players",
                         len(self.leaderboard))
        except Exception as e:
            log.warning("Failed to load leaderboard: %s", e)
            self.leaderboard = {}

    def _save_leaderboard(self):
        try:
            with open(self._leaderboard_path, "w") as f:
                json.dump({"players": self.leaderboard}, f, indent=2)
        except Exception as e:
            log.warning("Failed to save leaderboard: %s", e)

    def _load_join_history(self):
        try:
            if os.path.exists(self._join_history_path):
                with open(self._join_history_path, "r") as f:
                    self._join_history = json.load(f)
                log.info("Loaded join history: %d entries",
                         len(self._join_history))
        except Exception as e:
            log.warning("Failed to load join history: %s", e)
            self._join_history = []

    def _save_join_history(self):
        try:
            # Keep last 1000 entries
            if len(self._join_history) > 1000:
                self._join_history = self._join_history[-1000:]
            with open(self._join_history_path, "w") as f:
                json.dump(self._join_history, f, indent=2)
        except Exception as e:
            log.warning("Failed to save join history: %s", e)

    def _log_join(self, name: str, ip: str, event: str):
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "name": name,
            "ip": ip,
            "event": event,
        }
        self._join_history.append(entry)
        self._save_join_history()

    def _start_admin_server(self):
        if not self._admin_port:
            return
        handler_class = _make_flock_admin_handler(self)
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
        while not self._admin_command_queue.empty():
            try:
                cmd = self._admin_command_queue.get_nowait()
            except queue.Empty:
                break
            if cmd["cmd"] == "kick":
                target_uuid = cmd.get("uuid", "")
                for sock, info in list(self.clients.items()):
                    if info.uuid == target_uuid:
                        log.info("Admin kicked %s", info.username)
                        self._log_join(info.username,
                                       "%s:%d" % (info.address[0], info.address[1]),
                                       "kicked-by-admin")
                        self._remove_client(sock, "kicked by admin")
                        break
            elif cmd["cmd"] == "end_game":
                if self.game_active and self.sim:
                    log.info("Admin ended game")
                    self.sim.game_over = True
            elif cmd["cmd"] == "restart":
                log.info("Admin requested restart")
                self._running = False

    def _update_leaderboard(self, winner_id):
        if not self.sim:
            return

        game_players = {}
        for c in self.clients.values():
            if c.in_game and c.game_player_id is not None:
                p = self.sim.players.get(c.game_player_id)
                score = p.num_points if p else 0
                game_players[c.username] = score
                # Include local co-op players
                for i, ln in enumerate(c.local_player_names):
                    if i < len(c.local_player_ids):
                        lp = self.sim.players.get(c.local_player_ids[i])
                        lp_score = lp.num_points if lp else 0
                        game_players[ln] = lp_score
        for bot in self.bots:
            if bot.in_game and bot.game_player_id is not None:
                p = self.sim.players.get(bot.game_player_id)
                score = p.num_points if p else 0
                game_players[bot.name] = score

        # Find winner name
        winner_name = None
        if winner_id != 0xFF:
            for c in self.clients.values():
                if c.game_player_id == winner_id:
                    winner_name = c.username
                    break
                # Check local player IDs
                for i, lp_id in enumerate(c.local_player_ids):
                    if lp_id == winner_id and i < len(c.local_player_names):
                        winner_name = c.local_player_names[i]
                        break
                if winner_name:
                    break
            if not winner_name:
                for bot in self.bots:
                    if bot.game_player_id == winner_id:
                        winner_name = bot.name
                        break

        for name, score in game_players.items():
            if name not in self.leaderboard:
                self.leaderboard[name] = {
                    "wins": 0, "best_score": 0, "games_played": 0}
            entry = self.leaderboard[name]
            entry["games_played"] += 1
            if score > entry["best_score"]:
                entry["best_score"] = score
            if winner_name and name == winner_name:
                entry["wins"] += 1

        self._save_leaderboard()
        log.info("Leaderboard updated: %d total players",
                 len(self.leaderboard))

    def _get_leaderboard_top10(self) -> list:
        entries = []
        for name, data in self.leaderboard.items():
            entries.append({
                "name": name,
                "wins": data["wins"],
                "best_score": data["best_score"],
                "games_played": data["games_played"],
            })
        entries.sort(key=lambda e: (e["wins"], e["best_score"]),
                     reverse=True)
        return entries[:10]

    def _send_leaderboard_to_client(self, client):
        entries = self._get_leaderboard_top10()
        msg = build_leaderboard_data(entries)
        client.send_raw(msg)

    def _broadcast_leaderboard(self):
        entries = self._get_leaderboard_top10()
        msg = build_leaderboard_data(entries)
        for c in self.clients.values():
            if c.authenticated:
                c.send_raw(msg)

    def _next_user_id(self) -> int:
        used = {c.user_id for c in self.clients.values() if c.user_id > 0}
        uid = 1
        while uid in used:
            uid += 1
        return uid

    def _next_available_sprite(self) -> int:
        """Find the next sprite_id not already used by another player/bot."""
        used = set()
        for c in self.clients.values():
            if c.authenticated:
                used.add(c.sprite_id)
        for bot in self.bots:
            used.add(bot.sprite_id)
        for sid in range(12):
            if sid not in used:
                return sid
        return 0  # all taken, default to 0

    def start(self):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(MAX_BRIDGES + 5)
        self.server_socket.setblocking(False)

        log.info("Flicky's Flock Server listening on %s:%d",
                 self.host, self.port)
        self._running = True
        self._start_admin_server()
        self._run()

    def _run(self):
        while self._running:
            read_sockets = [self.server_socket]
            read_sockets.extend(self.pending_auth.keys())
            read_sockets.extend(self.clients.keys())

            timeout = self._tick_interval if self.game_active else 1.0

            try:
                readable, _, _ = select.select(read_sockets, [], [], timeout)
            except (ValueError, OSError):
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

            self._check_timeouts(now)
            self._process_admin_commands()

    def _accept_connection(self):
        try:
            client_sock, addr = self.server_socket.accept()
            client_sock.setblocking(False)
        except OSError:
            return

        if len(self.authenticated_bridges) >= MAX_BRIDGES:
            log.warning("Max bridges reached, rejecting %s:%d",
                        addr[0], addr[1])
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

        while len(client.recv_buffer) >= 2:
            payload_len = ((client.recv_buffer[0] << 8) |
                           client.recv_buffer[1])
            total = 2 + payload_len
            if payload_len == 0 or payload_len > MAX_RECV_BUFFER:
                log.warning("Invalid frame from %s, disconnecting",
                            client.address)
                self._remove_client(sock, "invalid frame")
                return
            if len(client.recv_buffer) < total:
                break

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
            pass
        elif msg_type == MSG_DISCONNECT:
            self._remove_client(sock, "disconnect requested")
        elif msg_type == FNET_MSG_READY:
            self._handle_ready(sock, client)
        elif msg_type == FNET_MSG_START_GAME_REQ:
            self._handle_start_game(sock, client)
        elif msg_type == FNET_MSG_INPUT_STATE:
            self._handle_input_state(sock, client, payload)
        elif msg_type == FNET_MSG_PAUSE_REQ:
            self._handle_pause(sock, client)
        elif msg_type == FNET_MSG_PLAYER_STATE:
            self._handle_player_state(sock, client, payload)
        elif msg_type == FNET_MSG_SPRITE_SELECT:
            self._handle_sprite_select(sock, client, payload)
        elif msg_type == FNET_MSG_BOT_ADD:
            self._handle_bot_add(sock, client)
        elif msg_type == FNET_MSG_BOT_REMOVE:
            self._handle_bot_remove(sock, client)
        elif msg_type == FNET_MSG_ADD_LOCAL_PLAYER:
            self._handle_add_local_player(sock, client, payload)
        elif msg_type == FNET_MSG_REMOVE_LOCAL_PLAYER:
            self._handle_remove_local_player(sock, client)
        elif msg_type == FNET_MSG_INPUT_STATE_P2:
            self._handle_input_state_p2(sock, client, payload)
        elif msg_type == FNET_MSG_LEADERBOARD_REQ:
            self._send_leaderboard_to_client(client)
        elif msg_type == FNET_MSG_CLIENT_DEATH:
            self._handle_client_death(sock, client, None)
        elif msg_type == FNET_MSG_CLIENT_DEATH_P2:
            self._handle_client_death(sock, client, payload)
        elif msg_type == FNET_MSG_CLIENT_POWERUP_COLLECT:
            self._handle_client_powerup_collect(sock, client, payload)
        else:
            log.debug("Unknown message type 0x%02X from %s",
                      msg_type, client.address)

    # ------------------------------------------------------------------
    # Auth handlers
    # ------------------------------------------------------------------

    def _handle_connect(self, sock, client: ClientInfo, payload: bytes):
        client_uuid = ""
        if len(payload) > 1 + UUID_LEN - 1:
            client_uuid = payload[1:1 + UUID_LEN].decode(
                "ascii", errors="replace").rstrip("\x00")

        if client_uuid and client_uuid in self.uuid_map:
            client.uuid = client_uuid
            client.username = self.uuid_map[client_uuid]
            client.user_id = self._next_user_id()
            client.authenticated = True
            client.sprite_id = self._next_available_sprite()
            log.info("Player reconnected: %s (uuid=%s..)",
                     client.username, client_uuid[:8])
            client.send_raw(build_welcome_back(
                client.user_id, client.uuid, client.username))
            self._broadcast_lobby_state()
            self._send_leaderboard_to_client(client)
        else:
            if not client.uuid:
                client.uuid = str(uuid.uuid4())
                client.user_id = self._next_user_id()
                self.uuid_map[client.uuid] = ""
            log.info("New player connected (uuid=%s..)", client.uuid[:8])
            client.send_raw(build_username_required())

    def _handle_set_username(self, sock, client: ClientInfo,
                              payload: bytes):
        if len(payload) < 2:
            return
        name_len = payload[1]
        if len(payload) < 2 + name_len:
            return
        username = payload[2:2 + name_len].decode(
            "utf-8", errors="replace")
        username = username[:USERNAME_MAX_LEN].strip()

        if not username:
            client.send_raw(build_username_taken())
            return

        for s, c in self.clients.items():
            if (s != sock and c.authenticated and
                    c.username.lower() == username.lower()):
                log.info("Username '%s' taken", username)
                client.send_raw(build_username_taken())
                return

        # Check lobby capacity (include local co-op players)
        lobby_slots = sum(1 + len(c.local_player_names)
                          for c in self.clients.values()
                          if c.authenticated)
        lobby_slots += len(self.bots)
        if lobby_slots >= MAX_PLAYERS:
            client.send_raw(build_log(
                "Server full (%d/%d)" % (lobby_slots, MAX_PLAYERS)))
            return

        client.username = username
        client.authenticated = True
        client.sprite_id = self._next_available_sprite()
        self.uuid_map[client.uuid] = username

        self._log_join(username,
                       "%s:%d" % (client.address[0], client.address[1]),
                       "joined")
        log.info("Player %d set username: %s", client.user_id, username)
        client.send_raw(build_welcome(
            client.user_id, client.uuid, username))

        if self.game_active:
            client.send_raw(build_log(
                "Game in progress - wait for next round"))

        self._broadcast_lobby_state()
        self._send_leaderboard_to_client(client)

        for s, c in self.clients.items():
            if s != sock and c.authenticated:
                c.send_raw(build_player_join(client.user_id, username,
                                              client.sprite_id))
                c.send_raw(build_log("%s joined!" % username))

    # ------------------------------------------------------------------
    # Lobby handlers
    # ------------------------------------------------------------------

    def _handle_ready(self, sock, client: ClientInfo):
        if not client.authenticated:
            return
        client.ready = not client.ready
        log.info("Player %s ready=%s", client.username, client.ready)
        self._broadcast_lobby_state()

    def _handle_sprite_select(self, sock, client: ClientInfo,
                               payload: bytes):
        if not client.authenticated:
            return
        if len(payload) < 2:
            return

        requested = payload[1] % 12

        # Check if another player already has this sprite
        for s, c in self.clients.items():
            if s != sock and c.authenticated and c.sprite_id == requested:
                # Sprite taken -- send log and don't change
                client.send_raw(build_log("Bird color taken!"))
                return
        for bot in self.bots:
            if bot.sprite_id == requested:
                client.send_raw(build_log("Bird color taken!"))
                return

        client.sprite_id = requested
        log.info("Player %s selected sprite %d", client.username, requested)
        self._broadcast_lobby_state()

    def _handle_bot_add(self, sock, client: ClientInfo):
        if not client.authenticated or self.game_active:
            return
        total = sum(1 + len(c.local_player_names)
                    for c in self.clients.values() if c.authenticated)
        total += len(self.bots)
        if total >= MAX_PLAYERS:
            client.send_raw(build_log("Lobby full!"))
            return
        bot_id = len(self.bots)
        name = BOT_NAMES[bot_id % len(BOT_NAMES)]
        bot = BotPlayer(name, bot_id)
        bot.sprite_id = self._next_available_sprite()
        self.bots.append(bot)
        log.info("Bot '%s' added by %s (total bots: %d)",
                 name, client.username, len(self.bots))
        self._broadcast_lobby_state()

    def _handle_bot_remove(self, sock, client: ClientInfo):
        if not client.authenticated or self.game_active:
            return
        if not self.bots:
            client.send_raw(build_log("No bots to remove"))
            return
        removed = self.bots.pop()
        log.info("Bot '%s' removed by %s (total bots: %d)",
                 removed.name, client.username, len(self.bots))
        self._broadcast_lobby_state()

    def _handle_add_local_player(self, sock, client: ClientInfo,
                                    payload: bytes):
        """Handle ADD_LOCAL_PLAYER: register a second local player."""
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
            for suffix in range(2, 10):
                candidate = name + str(suffix)
                if candidate.lower() not in all_names:
                    name = candidate
                    break

        client.local_player_names.append(name)
        # Provisional ACK with 0xFF; real ID assigned at game start
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
        log.info("Player %s removed local player: %s",
                 client.username, removed_name)

        if self.game_active and self.sim and client.local_player_ids:
            pid = client.local_player_ids.pop()
            if pid in self.sim.players:
                self.sim.players[pid].state = FLICKYSTATE_DEAD
                self.sim.players[pid].num_lives = 1
                self.sim.players[pid].num_deaths = 1
            kill_msg = build_player_kill(pid)
            log_msg = build_log("%s left" % removed_name)
            self._broadcast_to_game(kill_msg)
            self._broadcast_to_game(log_msg)
        elif client.local_player_ids:
            client.local_player_ids.pop()

        self._broadcast_lobby_state()

    def _handle_input_state_p2(self, sock, client: ClientInfo,
                                payload: bytes):
        """Handle INPUT_STATE_P2: input from second local controller."""
        if not self.game_active or not client.in_game:
            return
        if len(payload) < 5:
            return

        # [type:1][player_id:1][frame:2 BE][input:1]
        player_id = payload[1]
        frame_num = (payload[2] << 8) | payload[3]
        input_bits = payload[4]

        # Validate: must be a local player of this client
        if player_id not in client.local_player_ids:
            return

        # Update player input in simulation
        if self.sim and player_id in self.sim.players:
            p = self.sim.players[player_id]
            if (input_bits & INPUT_FLAP) and not p.has_flapped:
                p.has_flapped = True
            p.last_input = input_bits

        # Delta compression relay
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

    def _handle_start_game(self, sock, client: ClientInfo):
        if self.game_active:
            return
        if not client.authenticated:
            return

        ready_players = [c for c in self.clients.values()
                         if c.authenticated and c.ready]
        ready_bots = [b for b in self.bots if b.ready]

        total = len(ready_players) + len(ready_bots)

        # Must have at least 1 ready human player
        if len(ready_players) < 1:
            client.send_raw(build_log("You must be READY first! (Press A)"))
            return

        if total < 2:
            client.send_raw(build_log("Need 2+ ready players"))
            return

        if total > MAX_PLAYERS:
            client.send_raw(build_log(
                "Too many players (max %d)" % MAX_PLAYERS))
            return

        # Start game
        self.game_seed = random.randint(0, 0xFFFFFFFF)
        self.game_active = True
        self.game_paused = False
        self._last_tick = time.time()

        self.last_relayed_input.clear()
        self.relay_cooldown.clear()

        log.info("Game starting! Seed=%08X, %d players",
                 self.game_seed, total)

        self.sim = GameSimulation(self.num_lives, self.start_pos, total,
                                  self.game_seed)

        # Count total slots including local co-op players
        local_extra = sum(len(c.local_player_names) for c in ready_players)
        total_slots = total + local_extra

        if total_slots > MAX_PLAYERS:
            client.send_raw(build_log(
                "Too many players (max %d)" % MAX_PLAYERS))
            return

        # Assign player IDs: primary players first, then local extras, then bots
        pid = 0

        # Primary players
        for c in ready_players:
            c.in_game = True
            c.game_player_id = pid
            c.local_player_ids = []
            self.sim.init_player(pid)
            self.sim.players[pid].sprite_id = c.sprite_id
            pid += 1

        # Additional local players (P2 co-op)
        for c in ready_players:
            for i, ln in enumerate(c.local_player_names):
                c.local_player_ids.append(pid)
                self.sim.init_player(pid)
                self.sim.players[pid].sprite_id = (c.sprite_id + 1 + i) % 12
                pid += 1

        # Bots
        for bot in ready_bots:
            bot.game_player_id = pid
            bot.reset_for_game()
            self.sim.init_player(pid)
            self.sim.players[pid].sprite_id = bot.sprite_id
            self.sim.bot_ids.add(pid)
            pid += 1

        # Update simulation total
        self.sim.num_players = pid

        # Send GAME_START to all real clients
        for c in ready_players:
            opponent_count = pid - 1
            c.send_raw(build_game_start(
                self.game_seed, c.game_player_id, opponent_count,
                self.num_lives, self.start_pos))
            # Send LOCAL_PLAYER_ACK for additional local players
            for lp_id in c.local_player_ids:
                c.send_raw(build_local_player_ack(lp_id))

        # Send PLAYER_JOIN roster with sprite_id
        roster = []
        for c in ready_players:
            roster.append((c.game_player_id, c.username, c.sprite_id))
            for i, ln in enumerate(c.local_player_names):
                lp_sprite = (c.sprite_id + 1 + i) % 12
                roster.append((c.local_player_ids[i], ln, lp_sprite))
        for bot in ready_bots:
            roster.append((bot.game_player_id, bot.name, bot.sprite_id))
        for c in ready_players:
            for r_pid, name, sprite in roster:
                c.send_raw(build_player_join(r_pid, name, sprite))

        # Pre-spawn all 6 pipes and broadcast to all clients
        pipe_events = self.sim._init_pipes()
        for evt in pipe_events:
            self._broadcast_event(evt)

    # ------------------------------------------------------------------
    # In-game handlers
    # ------------------------------------------------------------------

    def _handle_input_state(self, sock, client: ClientInfo,
                             payload: bytes):
        if not self.game_active or not client.in_game:
            return
        if len(payload) < 4:
            return

        # [type:1][frame:2 BE][input:1]
        frame_num = (payload[1] << 8) | payload[2]
        input_bits = payload[3]
        player_id = client.game_player_id

        # Update player input in simulation
        if self.sim and player_id in self.sim.players:
            p = self.sim.players[player_id]
            # Check for initial flap
            if (input_bits & INPUT_FLAP) and not p.has_flapped:
                p.has_flapped = True
            p.last_input = input_bits

        # Delta compression: relay when changed or every 15 frames
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
        # Flicky's Flock online mode doesn't support pause
        pass

    def _handle_player_state(self, sock, client: ClientInfo,
                              payload: bytes):
        if not self.game_active or not client.in_game:
            return
        if len(payload) < 7:
            return

        # [type:1][y:2s][y_speed:2s][state:1][sprite:1]
        y = struct.unpack("!h", payload[1:3])[0]
        y_speed = struct.unpack("!h", payload[3:5])[0]
        state = payload[5]
        sprite = payload[6]

        player_id = client.game_player_id

        if self.sim:
            self.sim.update_player_from_client(player_id, y, y_speed,
                                                state, sprite)

        # Relay as PLAYER_SYNC to other clients
        p = self.sim.players.get(player_id) if self.sim else None
        points = p.num_points if p else 0
        deaths = p.num_deaths if p else 0
        sync_msg = build_player_sync(player_id, y, y_speed, state,
                                      points, deaths, sprite)
        for s, c in self.clients.items():
            if c.in_game and s != sock:
                c.send_raw(sync_msg)

    # ------------------------------------------------------------------
    # Client-authoritative death
    # ------------------------------------------------------------------

    def _handle_client_death(self, sock, client: ClientInfo,
                              payload: bytes):
        """Client reports their own death (client-authoritative collision).
        Kill the player on the server side and broadcast to all OTHER clients."""
        if not self.game_active or not client.in_game:
            return

        # Determine which player died
        if payload is not None and len(payload) >= 2:
            # CLIENT_DEATH_P2: [type:1][player_id:1]
            player_id = payload[1]
            # Verify this P2 belongs to this client
            if player_id not in client.local_player_ids:
                return
        else:
            # CLIENT_DEATH: the client's own player
            player_id = client.game_player_id

        if self.sim:
            self.sim._kill_player(player_id)

        # Broadcast PLAYER_KILL to all OTHER clients (sender already killed locally)
        kill_msg = build_player_kill(player_id)
        for s, c in self.clients.items():
            if c.in_game and s != sock:
                c.send_raw(kill_msg)

        # Send updated score to ALL
        p = self.sim.players.get(player_id) if self.sim else None
        if p:
            score_msg = build_score_update(
                player_id, p.num_points, p.num_deaths)
            self._broadcast_to_game(score_msg)

        log.info("Player %d death reported by client", player_id)

    def _handle_client_powerup_collect(self, sock, client: ClientInfo,
                                        payload: bytes):
        """Client reports collecting a powerup (client-authoritative).
        Deactivate the powerup on server and broadcast effect to OTHER clients."""
        if not self.game_active or not client.in_game:
            return
        if len(payload) < 2:
            return

        slot = payload[1]
        if slot >= MAX_POWER_UPS:
            return
        if not self.sim:
            return

        pu = self.sim.powerups[slot]
        if not pu.active:
            return

        pu_type = pu.type
        pu.active = False

        # Determine picker_id (primary player for this client)
        picker_id = client.game_player_id

        # Broadcast POWERUP_EFFECT to all OTHER clients (sender already applied locally)
        effect_msg = build_powerup_effect(pu_type, picker_id)
        for s, c in self.clients.items():
            if c.in_game and s != sock:
                c.send_raw(effect_msg)

        log.info("Player %d collected powerup slot %d (type %d)",
                 picker_id, slot, pu_type)

    # ------------------------------------------------------------------
    # Game simulation tick
    # ------------------------------------------------------------------

    def _game_tick(self):
        if not self.sim:
            return

        events = self.sim.tick()
        for evt in events:
            self._broadcast_event(evt)

        # Bot AI
        for bot in self.bots:
            if not bot.in_game:
                continue

            p = self.sim.players.get(bot.game_player_id)
            if not p or p.state != FLICKYSTATE_FLYING:
                continue

            bits = bot.ai.tick(p.y_pos, p.y_speed, self.sim.pipes)

            # Apply input to bot player (server-side only, no relay needed)
            if (bits & INPUT_FLAP) and not p.has_flapped:
                p.has_flapped = True
            p.last_input = bits

            # Send bot PLAYER_SYNC periodically (clients use this for position)
            # No INPUT_RELAY for bots - client doesn't use it, saves bandwidth
            if not hasattr(bot, 'sync_counter'):
                bot.sync_counter = 0
            bot.sync_counter += 1
            if bot.sync_counter >= 8:
                bot.sync_counter = 0
                sync_msg = build_player_sync(
                    bot.game_player_id, p.y_pos, p.y_speed,
                    p.state, p.num_points, p.num_deaths, p.sprite_id)
                self._broadcast_to_game(sync_msg)

    def _broadcast_event(self, evt):
        """Convert a simulation event to a message and broadcast."""
        if evt[0] == "pipe_spawn":
            _, slot, x, y, gap, sections, top_y = evt
            msg = build_pipe_spawn(slot, x, y, gap, sections, top_y)
            self._broadcast_to_game(msg)

        elif evt[0] == "powerup_spawn":
            _, slot, pu_type, x, y = evt
            msg = build_powerup_spawn(slot, pu_type, x, y)
            self._broadcast_to_game(msg)

        elif evt[0] == "player_kill":
            _, pid = evt
            msg = build_player_kill(pid)
            self._broadcast_to_game(msg)
            log.info("Player %d killed", pid)

            # Send updated score
            p = self.sim.players.get(pid) if self.sim else None
            if p:
                score_msg = build_score_update(
                    pid, p.num_points, p.num_deaths)
                self._broadcast_to_game(score_msg)

        elif evt[0] == "player_spawn":
            _, pid = evt
            msg = build_player_spawn(pid)
            self._broadcast_to_game(msg)
            log.info("Player %d respawned", pid)

        elif evt[0] == "score_update":
            _, pid, points, deaths = evt
            msg = build_score_update(pid, points, deaths)
            self._broadcast_to_game(msg)

        elif evt[0] == "powerup_effect":
            _, pu_type, picker_id = evt
            msg = build_powerup_effect(pu_type, picker_id)
            self._broadcast_to_game(msg)
            log.info("Powerup %d picked by player %d", pu_type, picker_id)

            # Apply server-side effects
            if self.sim:
                if pu_type == POWERUP_ONE_UP:
                    p = self.sim.players.get(picker_id)
                    if p and p.num_deaths > 0:
                        p.num_deaths -= 1
                elif pu_type == POWERUP_REVERSE_GRAVITY:
                    for p in self.sim.players.values():
                        if p.state == FLICKYSTATE_FLYING:
                            p.reverse_gravity_timer = 600
                elif pu_type == POWERUP_LIGHTNING:
                    for p in self.sim.players.values():
                        if p.state == FLICKYSTATE_FLYING:
                            p.lightning_timer = 600
                elif pu_type == POWERUP_ROBOTNIK:
                    p = self.sim.players.get(picker_id)
                    if p and p.state == FLICKYSTATE_FLYING:
                        self.sim._kill_player(picker_id)
                        kill_msg = build_player_kill(picker_id)
                        self._broadcast_to_game(kill_msg)
                elif pu_type == POWERUP_STONE_SNEAKERS:
                    for p in self.sim.players.values():
                        if p.state == FLICKYSTATE_FLYING:
                            p.stone_sneakers_timer = 600

        elif evt[0] == "game_over":
            _, winner = evt
            msg = build_game_over(winner)
            self._broadcast_to_game(msg)
            self.game_active = False
            log.info("Game over! Winner=%d", winner)

            self._update_leaderboard(winner)

            for c in self.clients.values():
                if c.in_game:
                    c.in_game = False
                    c.ready = False
            for bot in self.bots:
                bot.in_game = False
                bot.ready = True
            self._broadcast_lobby_state()
            self._broadcast_leaderboard()

    def _broadcast_to_game(self, msg: bytes):
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
                    "sprite_id": c.sprite_id,
                })
                # Include local co-op players in lobby listing
                for ln in c.local_player_names:
                    players.append({
                        "id": c.user_id + 100,
                        "name": ln,
                        "ready": c.ready,
                        "sprite_id": (c.sprite_id + 1) % 12,
                    })
        for bot in self.bots:
            players.append({
                "id": 200 + bot.bot_id,
                "name": bot.name,
                "ready": bot.ready,
                "sprite_id": bot.sprite_id,
            })

        msg = build_lobby_state(players)
        for c in self.clients.values():
            if c.authenticated:
                c.send_raw(msg)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _remove_client(self, sock, reason: str):
        client = self.clients.get(sock)
        if client:
            if client.authenticated:
                self._log_join(client.username or "unknown",
                               "%s:%d" % (client.address[0], client.address[1]),
                               "left: %s" % reason)
            log.info("Removing %s (%s): %s",
                     client.username or "unknown", client.address, reason)

            if client.in_game and self.game_active and self.sim:
                pid = client.game_player_id
                if pid in self.sim.players:
                    self.sim.players[pid].state = FLICKYSTATE_DEAD
                    self.sim.players[pid].num_lives = 1
                    self.sim.players[pid].num_deaths = 1

                # Also kill any local co-op players
                for lp_id in client.local_player_ids:
                    if lp_id in self.sim.players:
                        self.sim.players[lp_id].state = FLICKYSTATE_DEAD
                        self.sim.players[lp_id].num_lives = 1
                        self.sim.players[lp_id].num_deaths = 1

                leave_msg = build_player_leave(client.user_id)
                log_msg = build_log(
                    "%s disconnected" % (client.username or "Player"))
                kill_msg = build_player_kill(pid)
                for s, c in self.clients.items():
                    if c.in_game and s != sock:
                        c.send_raw(leave_msg)
                        c.send_raw(log_msg)
                        c.send_raw(kill_msg)
                        # Also send kill for local co-op players
                        for lp_id in client.local_player_ids:
                            c.send_raw(build_player_kill(lp_id))

                client.in_game = False
                client.ready = False

                remaining = [c for c in self.clients.values()
                             if c.in_game and c is not client]
                if not remaining:
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
        expired = [s for s, info in self.pending_auth.items()
                   if now > info["deadline"]]
        for sock in expired:
            log.warning("Auth timeout for %s",
                        self.pending_auth[sock]["address"])
            sock.close()
            del self.pending_auth[sock]

        for sock in list(self.clients.keys()):
            client = self.clients[sock]
            if now - client.last_activity > HEARTBEAT_TIMEOUT:
                self._remove_client(sock, "heartbeat timeout")


# ==========================================================================
# CLI
# ==========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Flicky's Flock NetLink Game Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=4824,
                        help="Bind port")
    parser.add_argument("--bots", type=int, default=0,
                        help="Number of server-side bot players (0-11)")
    parser.add_argument("--admin-port", type=int, default=0,
                        help="Admin HTTP port (0=disabled)")
    parser.add_argument("--admin-user", default="admin",
                        help="Admin username")
    parser.add_argument("--admin-password", default="flock2026",
                        help="Admin password")
    parser.add_argument("--verbose", action="store_true",
                        help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    server = FlockServer(host=args.host, port=args.port,
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
