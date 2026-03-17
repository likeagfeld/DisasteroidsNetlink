#!/usr/bin/env python3
"""
Disasteroids NetLink Game Server

Manages online multiplayer for Disasteroids. Architecture follows the
Coup server pattern: bridge-authenticated connections, SNCP binary framing,
lobby management, and per-frame input relay.

Networking model: INPUT RELAY
  Each Saturn sends its local player inputs every frame.
  The server broadcasts all inputs to all clients.
  Both Saturns run the same deterministic game simulation.

Usage:
    python3 tools/disasteroids_server/server.py
    python3 tools/disasteroids_server/server.py --port 4821
"""

import argparse
import logging
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
SHARED_SECRET = b"SaturnCoup2025!NetLink#SecretKey"
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

# Disasteroids Messages
DNET_MSG_READY = 0x10
DNET_MSG_INPUT_STATE = 0x11
DNET_MSG_START_GAME_REQ = 0x12
DNET_MSG_PAUSE_REQ = 0x13

DNET_MSG_LOBBY_STATE = 0xA0
DNET_MSG_GAME_START = 0xA1
DNET_MSG_INPUT_RELAY = 0xA2
DNET_MSG_PLAYER_JOIN = 0xA3
DNET_MSG_PLAYER_LEAVE = 0xA4
DNET_MSG_GAME_OVER = 0xA5
DNET_MSG_LOG = 0xA6
DNET_MSG_PAUSE_ACK = 0xA7

# Game types (matching Disasteroids GAME_TYPE enum)
GAME_TYPE_COOP = 0
GAME_TYPE_VERSUS = 1

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


def build_log(text: str) -> bytes:
    raw = text.encode("utf-8")[:255]
    payload = bytes([DNET_MSG_LOG, len(raw)]) + raw
    return encode_frame(payload)


def build_pause_ack(paused: bool) -> bytes:
    return encode_frame(bytes([DNET_MSG_PAUSE_ACK, 1 if paused else 0]))


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
    def __init__(self, host: str = "0.0.0.0", port: int = 4821):
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

        # Delta compression: track last relayed input per player_id
        self.last_relayed_input = {}   # {player_id: input_bits}
        self.relay_cooldown = {}       # {player_id: frames_since_relay}

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

            try:
                readable, _, _ = select.select(read_sockets, [], [], 1.0)
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

        client.username = username
        client.authenticated = True
        self.uuid_map[client.uuid] = username

        log.info("Player %d set username: %s", client.user_id, username)
        client.send_raw(build_welcome(client.user_id, client.uuid, username))
        self._broadcast_lobby_state()

        # Announce join
        for s, c in self.clients.items():
            if s != sock and c.authenticated:
                c.send_raw(build_player_join(client.user_id, username))
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

    def _handle_start_game(self, sock, client: ClientInfo):
        if self.game_active:
            return
        if not client.authenticated:
            return

        # Only ready players join the game
        ready_players = [c for c in self.clients.values()
                         if c.authenticated and c.ready]
        if len(ready_players) < 2:
            client.send_raw(build_log("Need 2+ ready players"))
            return

        # Start game
        self.game_seed = random.randint(0, 0xFFFFFFFF)
        self.game_active = True
        self.game_paused = False

        # Reset delta compression state for new game
        self.last_relayed_input.clear()
        self.relay_cooldown.clear()

        log.info("Game starting! Seed=%08X, %d players",
                 self.game_seed, len(ready_players))

        # Assign player IDs (0-indexed) and send GAME_START
        for i, c in enumerate(ready_players):
            c.in_game = True
            c.alive = True
            c.game_player_id = i
            opponent_count = len(ready_players) - 1
            c.send_raw(build_game_start(
                self.game_seed, i, opponent_count,
                self.game_type, self.num_lives))

    # ------------------------------------------------------------------
    # In-game handlers
    # ------------------------------------------------------------------

    def _handle_input_state(self, sock, client: ClientInfo, payload: bytes):
        if not self.game_active or not client.in_game:
            return
        if len(payload) < 5:
            return

        # [type:1][frame:2 BE][input:2 BE]
        frame_num = (payload[1] << 8) | payload[2]
        input_bits = (payload[3] << 8) | payload[4]

        # Use the stable player_id assigned during GAME_START
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

            if client.in_game:
                # Notify other players and end the game for everyone
                authenticated = [c for c in self.clients.values()
                                 if c.authenticated and c.in_game and c is not client]
                leave_msg = build_player_leave(client.user_id)
                log_msg = build_log("%s disconnected" % (client.username or "Player"))
                self.game_active = False
                for c in authenticated:
                    c.send_raw(leave_msg)
                    c.send_raw(log_msg)
                    c.send_raw(build_game_over(c.game_player_id))
                    c.in_game = False
                    c.ready = False

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
    parser.add_argument("--port", type=int, default=4821, help="Bind port")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    server = DisasteroidsServer(host=args.host, port=args.port)
    try:
        server.start()
    except KeyboardInterrupt:
        log.info("Server shutting down")


if __name__ == "__main__":
    main()
