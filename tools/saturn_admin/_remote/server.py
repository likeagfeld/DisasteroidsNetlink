#!/usr/bin/env python3
"""
Coup Card Game Server - Saturn NetLink Edition

Multi-player TCP server with optional WebSocket listener for web clients.
Game logic is provided by the C rule engine (libcoup_rules) via ctypes bridge.
This server handles networking, authentication, lobby management, event
translation, and timeouts.

Fallback: if the shared library isn't built, falls back to Python game logic
(set USE_C_ENGINE = False).

Run:  python server.py [--port 4821] [--ws-port 4823]
"""

import argparse
import asyncio
import base64
import csv
import json
import logging
import os
import queue
import random
import select
import socket
import struct
import threading
import time
import uuid as uuid_mod
from enum import Enum
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# Optional WebSocket support
try:
    import websockets
    import websockets.asyncio.server
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("coup_server")

# ---------------------------------------------------------------------------
# C Engine Integration
# ---------------------------------------------------------------------------

USE_C_ENGINE = True
try:
    from coup_engine import (
        CoupEngine,
        PHASE_WAITING_FOR_ACTION, PHASE_WAITING_FOR_INFLUENCE_LOSS,
        PHASE_WAITING_FOR_EXCHANGE, PHASE_CHALLENGE_WINDOW, PHASE_BLOCK_WINDOW,
        PHASE_BLOCK_CHALLENGE_WINDOW, PHASE_RESOLVING, PHASE_LOBBY,
    )
    log.info("C rule engine loaded successfully")
except Exception as e:
    USE_C_ENGINE = False
    log.warning("C rule engine not available (%s), using Python fallback", e)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEARTBEAT_TIMEOUT = 60.0
MAX_RECV_BUFFER = 8192
USERNAME_MAX_LEN = 16
UUID_LEN = 36

# Shared secret authentication
SHARED_SECRET = b"SaturnCoup2025!NetLink#SecretKey"
AUTH_MAGIC = b"AUTH"
AUTH_OK = 0x01
AUTH_TIMEOUT = 5.0  # seconds to complete handshake

# Characters
CHAR_DUKE = 0
CHAR_ASSASSIN = 1
CHAR_CAPTAIN = 2
CHAR_AMBASSADOR = 3
CHAR_CONTESSA = 4
CHAR_FACEDOWN = 5
CHAR_NONE = 6
# Actions
ACT_INCOME = 0
ACT_FOREIGN_AID = 1
ACT_COUP = 2
ACT_TAX = 3
ACT_ASSASSINATE = 4
ACT_STEAL = 5
ACT_EXCHANGE = 6

# Action claims (-1 = no character claim)
ACTION_CLAIM = [-1, -1, -1, CHAR_DUKE, CHAR_ASSASSIN, CHAR_CAPTAIN, CHAR_AMBASSADOR]
ACTION_NEEDS_TARGET = [False, False, True, False, True, True, False]

# Challenge/block timeout
# Note: engine supports COUP_RULES_MAX_PLAYERS (7) but official Coup rules
# cap at 6 players, so the server enforces this lower limit.
MAX_GAME_PLAYERS = 6
MAX_BRIDGES = 10

CHALLENGE_TIMEOUT = 12.0
BLOCK_TIMEOUT = 12.0
INFLUENCE_TIMEOUT = 30.0
EXCHANGE_TIMEOUT = 30.0
TURN_TIMEOUT = 60.0

# In-process bot settings
BOT_THINK_DELAY_MIN = 3.0   # seconds — gives human time to read game state
BOT_THINK_DELAY_MAX = 6.0   # seconds
BOT_DIFFICULTY_DEFAULT = 1   # medium
BOT_NAMES = ["DANTE", "RANDAL", "JAY", "SILENT BOB", "ELIAS", "BECKY"]

# Inactivity / idle kick
LOBBY_INACTIVITY_TIMEOUT = 300.0   # 5 minutes of no meaningful action in lobby
MAX_CONSECUTIVE_TURN_TIMEOUTS = 2  # kick after 2 missed turns in a row

# ---------------------------------------------------------------------------
# SNCP Message Types
# ---------------------------------------------------------------------------

# Client -> Server (auth)
MSG_CONNECT = 0x01
MSG_SET_USERNAME = 0x02
MSG_HEARTBEAT = 0x04
MSG_DISCONNECT = 0x05

# Client -> Server (game)
COUP_MSG_READY = 0x10
COUP_MSG_ACTION = 0x11
COUP_MSG_RESPONSE = 0x12
COUP_MSG_BLOCK_CLAIM = 0x13
COUP_MSG_LOSE_INFLUENCE = 0x14
COUP_MSG_EXCHANGE_CHOICE = 0x15
COUP_MSG_START_GAME_REQ = 0x16
COUP_MSG_ADD_BOT = 0x17
COUP_MSG_REMOVE_BOT = 0x18
COUP_MSG_SET_BOT_DIFFICULTY = 0x19
COUP_MSG_RESYNC_REQ = 0x1A

# Server -> Client (auth)
MSG_USERNAME_REQUIRED = 0x81
MSG_WELCOME = 0x82
MSG_WELCOME_BACK = 0x83
MSG_USERNAME_TAKEN = 0x84

# Server -> Client (game)
COUP_MSG_LOBBY_STATE = 0xA0
COUP_MSG_GAME_START = 0xA1
COUP_MSG_LOG = 0xAE
COUP_MSG_INPUT_RELAY = 0xB2
COUP_MSG_RESYNC = 0xB3
COUP_MSG_RESYNC_FULL = 0xB4
COUP_MSG_ACTION_REJECTED = 0xB5

# INPUT_RELAY input type codes
RELAY_START_GAME = 0
RELAY_ACTION = 1
RELAY_RESPONSE = 2
RELAY_BLOCK_CLAIM = 3
RELAY_LOSE_INFLUENCE = 4
RELAY_EXCHANGE_CHOICE = 5
RELAY_TIMEOUT = 6

# Responses
RESP_PASS = 0
RESP_CHALLENGE = 1
RESP_BLOCK = 2


class TurnPhase(Enum):
    LOBBY = 0
    WAITING_FOR_ACTION = 1
    CHALLENGE_WINDOW = 2
    BLOCK_WINDOW = 3
    BLOCK_CHALLENGE_WINDOW = 4
    WAITING_FOR_INFLUENCE_LOSS = 5
    WAITING_FOR_EXCHANGE = 6
    RESOLVING = 7


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------

def encode_frame(payload: bytes) -> bytes:
    return struct.pack("!H", len(payload)) + payload


def encode_lp_string(s: str) -> bytes:
    raw = s.encode("utf-8")[:255]
    return struct.pack("B", len(raw)) + raw


def encode_uuid(uuid_str: str) -> bytes:
    raw = uuid_str.encode("ascii")
    if len(raw) < UUID_LEN:
        raw += b"\x00" * (UUID_LEN - len(raw))
    return raw[:UUID_LEN]


# ---------------------------------------------------------------------------
# Server-to-client message builders
# ---------------------------------------------------------------------------

def build_username_required() -> bytes:
    return encode_frame(bytes([MSG_USERNAME_REQUIRED]))


def build_welcome(user_id, uuid_str, username) -> bytes:
    payload = (bytes([MSG_WELCOME])
               + struct.pack("B", user_id & 0xFF)
               + encode_uuid(uuid_str)
               + encode_lp_string(username))
    return encode_frame(payload)


def build_welcome_back(user_id, uuid_str, username) -> bytes:
    payload = (bytes([MSG_WELCOME_BACK])
               + struct.pack("B", user_id & 0xFF)
               + encode_uuid(uuid_str)
               + encode_lp_string(username))
    return encode_frame(payload)


def build_username_taken() -> bytes:
    return encode_frame(bytes([MSG_USERNAME_TAKEN]))


def build_lobby_state(players) -> bytes:
    count = min(len(players), 8)
    payload = bytes([COUP_MSG_LOBBY_STATE, count])
    for p in players[:count]:
        payload += struct.pack("B", p["id"])
        payload += encode_lp_string(p["name"])
        payload += struct.pack("B", 1 if p["ready"] else 0)
        payload += struct.pack("B", 1 if p.get("is_bot", False) else 0)
        payload += struct.pack("B", p.get("difficulty", 0) & 0xFF)
    return encode_frame(payload)


def build_game_start(engine_pid, seed, player_order) -> bytes:
    """GAME_START: [0xA1][seed:4 BE][my_engine_pid:1][count:1][uid_0:1]...[uid_n:1]
    player_order is a list of user_ids in engine PID order (index=PID)."""
    payload = bytes([COUP_MSG_GAME_START])
    payload += struct.pack("!I", seed & 0xFFFFFFFF)
    payload += bytes([engine_pid, len(player_order)])
    for uid in player_order:
        payload += bytes([uid & 0xFF])
    return encode_frame(payload)


def build_input_relay(input_type, player_id, data=b"", seq=0) -> bytes:
    """INPUT_RELAY: [0xB2][seq_hi:1][seq_lo:1][input_type:1][player_id:1][data...]"""
    payload = bytes([COUP_MSG_INPUT_RELAY,
                     (seq >> 8) & 0xFF, seq & 0xFF,
                     input_type, player_id]) + data
    return encode_frame(payload)


def build_action_rejected(current_seq, phase) -> bytes:
    """ACTION_REJECTED: [0xB5][current_seq:2 BE][phase:1]"""
    payload = bytes([COUP_MSG_ACTION_REJECTED,
                     (current_seq >> 8) & 0xFF, current_seq & 0xFF,
                     phase & 0xFF])
    return encode_frame(payload)


def build_resync(entries) -> bytes:
    """RESYNC: [0xB3][count:1][{entry_len:1,seq:2 BE,type:1,pid:1,data...}...]"""
    payload = bytes([COUP_MSG_RESYNC, len(entries)])
    for seq, input_type, pid, data in entries:
        entry = struct.pack("!H", seq) + bytes([input_type, pid]) + data
        payload += bytes([len(entry)]) + entry
    return encode_frame(payload)


def build_resync_full(seed, my_pid, total_relays) -> bytes:
    """RESYNC_FULL: [0xB4][seed:4 BE][my_pid:1][total:2 BE]"""
    payload = bytes([COUP_MSG_RESYNC_FULL])
    payload += struct.pack("!I", seed & 0xFFFFFFFF)
    payload += bytes([my_pid])
    payload += struct.pack("!H", total_relays)
    return encode_frame(payload)


def build_log(text) -> bytes:
    raw = text.encode("utf-8")[:255]
    payload = bytes([COUP_MSG_LOG, len(raw)]) + raw
    return encode_frame(payload)


# ---------------------------------------------------------------------------
# WebSocket Proxy
# ---------------------------------------------------------------------------

class WSClientProxy:
    """Bridges a WebSocket connection into the select()-based server loop.

    Creates a socket.socketpair(). The server_sock end goes into select()
    read_list and clients dict.  The ws_sock end is written by the async
    WS receive coroutine so that incoming data appears on server_sock.

    Outgoing data is intercepted in send_to() — if the sock is a WS proxy,
    we push the bytes through the async WS send instead of sock.sendall().
    """
    def __init__(self, websocket, addr):
        self.websocket = websocket
        self.addr = addr
        self.server_sock, self.ws_sock = socket.socketpair()
        self.server_sock.setblocking(False)
        self.ws_sock.setblocking(False)
        self._closed = False

    def inject(self, data: bytes):
        """Write data from WS receive into the socketpair (server will read it)."""
        if self._closed:
            return
        try:
            self.ws_sock.sendall(data)
        except OSError:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.server_sock.close()
        except OSError:
            pass
        try:
            self.ws_sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Client / Player info
# ---------------------------------------------------------------------------

class ClientInfo:
    def __init__(self, sock, address):
        self.socket = sock
        self.address = address
        self.uuid = ""
        self.username = ""
        self.user_id = 0
        self.authenticated = False
        self.recv_buffer = b""
        self.last_activity = time.time()
        self.last_user_action = time.time()       # meaningful interaction (not heartbeat)
        self.consecutive_turn_timeouts = 0        # forced WAITING_FOR_ACTION timeouts
        # Game state
        self.ready = False
        self.in_game = False
        self.alive = True
        self.engine_pid = -1    # C engine player_id (0-indexed)
        self.is_spectating = False


# ---------------------------------------------------------------------------
# Admin Web Portal
# ---------------------------------------------------------------------------

ADMIN_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Coup Server Admin</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#1a1a2e;color:#e0e0e0;font-family:-apple-system,system-ui,monospace;padding:12px;font-size:14px}
h1{color:#e94560;margin-bottom:8px;font-size:20px}
h3{font-size:15px;margin-bottom:8px}
.info{color:#888;margin-bottom:12px;font-size:13px}
.panel{background:#16213e;padding:12px;border-radius:5px;margin:8px 0;overflow-x:auto}
table{width:100%;border-collapse:collapse;margin:6px 0;min-width:280px}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid #333;white-space:nowrap;font-size:13px}
th{background:#0f1a2e;color:#e94560}
tr:hover{background:#1a2744}
.btn{background:#e94560;color:#fff;border:none;padding:8px 16px;cursor:pointer;font-family:inherit;font-size:13px;border-radius:3px;touch-action:manipulation}
.btn:active{opacity:0.7}
.btn-warn{background:#f5a623}
.btn-danger{background:#d32f2f}
.status{display:inline-block;padding:2px 8px;border-radius:3px;font-size:12px}
.status-lobby{background:#2ecc71;color:#000}
.status-ingame{background:#3498db;color:#fff}
.status-spectating{background:#9b59b6;color:#fff}
.status-dead{background:#7f8c8d;color:#fff}
#msg{position:fixed;top:10px;left:50%;transform:translateX(-50%);background:#2ecc71;color:#000;padding:10px 20px;border-radius:5px;display:none;font-weight:bold;z-index:9}
.cards{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}
.card{flex:1;min-width:80px;background:#0f1a2e;border-radius:4px;padding:8px;text-align:center}
.card-label{font-size:11px;color:#888;margin-bottom:2px}
.card-value{font-size:16px;font-weight:bold;color:#e0e0e0}
.player-row{background:#0f1a2e;border-radius:4px;padding:10px;margin:6px 0}
.player-name{font-weight:bold;font-size:15px;margin-bottom:4px}
.player-details{font-size:12px;color:#999;display:flex;flex-wrap:wrap;gap:8px;margin:4px 0}
.player-details span{white-space:nowrap}
.controls{display:flex;gap:10px;flex-wrap:wrap}
@media(min-width:700px){
  .mobile-only{display:none}
  .desktop-only{display:table}
}
@media(max-width:699px){
  .mobile-only{display:block}
  .desktop-only{display:none}
  body{padding:8px}
}
</style></head><body>
<h1>Coup Server Admin</h1>
<div class="info">Next refresh: <span id="countdown">3</span>s | <span id="uptime">-</span> | <span id="status_dot" style="color:#2ecc71">&#9679;</span></div>
<div id="msg"></div>

<div class="panel">
<h3>Server Status</h3>
<div class="cards">
<div class="card"><div class="card-label">Game</div><div class="card-value" id="g_active">-</div></div>
<div class="card"><div class="card-label">Phase</div><div class="card-value" id="g_phase">-</div></div>
<div class="card"><div class="card-label">Current</div><div class="card-value" id="g_current">-</div></div>
<div class="card"><div class="card-label">Deadline</div><div class="card-value" id="g_deadline">-</div></div>
<div class="card"><div class="card-label">Relays</div><div class="card-value" id="g_relays">-</div></div>
<div class="card"><div class="card-label">Players</div><div class="card-value" id="g_players">-</div></div>
<div class="card"><div class="card-label">Bots</div><div class="card-value" id="g_bots">-</div></div>
</div></div>

<div class="panel">
<h3>Connected Players</h3>
<div id="ptable_mobile" class="mobile-only"></div>
<table class="desktop-only"><thead><tr><th>Username</th><th>Status</th><th>Activity</th><th>Last Action</th><th>Timeouts</th><th>Action</th></tr></thead>
<tbody id="ptable"></tbody></table>
</div>

<div class="panel">
<h3>Server Controls</h3>
<div class="controls">
<button class="btn btn-warn" onclick="endGame()">End Game</button>
<button class="btn btn-danger" onclick="restartServer()">Restart Server</button>
</div></div>

<script>
var REFRESH_SEC=3,countdown=REFRESH_SEC,BASE='';
// Determine API base: works on /admin/ (nginx) and / (direct :9090)
(function(){var p=location.pathname;if(p.indexOf('/admin')===0)BASE='/admin/';else BASE='/'})();

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
function restartServer(){if(confirm('RESTART the server? All connections will drop.'))api('POST','api/restart').then(function(r){if(r.message)showMsg(r.message,'#f5a623')})}
function fmtTime(s){if(s<0)return'-';var m=Math.floor(s/60),sec=Math.floor(s%60);return m>0?m+'m '+sec+'s':sec+'s'}
function fmtPhase(p){var n={'0':'Lobby','1':'Action','2':'Challenge','3':'Block','4':'Blk-Challenge','5':'Inf. Loss','6':'Exchange','7':'Resolving','-':'Lobby'};return n[p]||p}
function refresh(){
  countdown=REFRESH_SEC;
  api('GET','api/state').then(function(d){
    if(!d.game)return;
    document.getElementById('status_dot').style.color='#2ecc71';
    document.getElementById('uptime').textContent='Up '+fmtTime(d.uptime);
    var g=d.game;
    document.getElementById('g_active').textContent=g.active?'YES':'No';
    document.getElementById('g_phase').textContent=fmtPhase(g.phase);
    document.getElementById('g_current').textContent=g.current_player||'-';
    document.getElementById('g_deadline').textContent=g.deadline_remaining>0?fmtTime(g.deadline_remaining):'-';
    document.getElementById('g_relays').textContent=g.relay_count;
    document.getElementById('g_players').textContent=g.human_count;
    document.getElementById('g_bots').textContent=g.bot_count;
    // Desktop table
    var tb=document.getElementById('ptable');tb.innerHTML='';
    // Mobile cards
    var mb=document.getElementById('ptable_mobile');mb.innerHTML='';
    if(d.players.length===0){
      tb.innerHTML='<tr><td colspan="6" style="color:#888;text-align:center">No players connected</td></tr>';
      mb.innerHTML='<div style="color:#888;text-align:center;padding:12px">No players connected</div>';
    }
    d.players.forEach(function(p){
      var sc='status-lobby';
      if(p.status==='in-game')sc='status-ingame';
      else if(p.status==='spectating')sc='status-spectating';
      else if(p.status==='dead')sc='status-dead';
      // Desktop row
      var tr=document.createElement('tr');
      tr.innerHTML='<td><b>'+p.username+'</b></td>'
        +'<td><span class="status '+sc+'">'+p.status+'</span></td>'
        +'<td>'+fmtTime(p.idle_activity)+'</td>'
        +'<td>'+fmtTime(p.idle_action)+'</td>'
        +'<td>'+p.consecutive_timeouts+'</td>'
        +'<td><button class="btn" data-uuid="'+p.uuid+'" data-name="'+p.username+'">Kick</button></td>';
      tb.appendChild(tr);
      // Mobile card
      var card=document.createElement('div');card.className='player-row';
      card.innerHTML='<div class="player-name">'+p.username+' <span class="status '+sc+'">'+p.status+'</span></div>'
        +'<div class="player-details"><span>Active: '+fmtTime(p.idle_activity)+'</span><span>Action: '+fmtTime(p.idle_action)+'</span><span>Timeouts: '+p.consecutive_timeouts+'</span></div>'
        +'<div style="margin-top:6px"><button class="btn" data-uuid="'+p.uuid+'" data-name="'+p.username+'">Kick</button></div>';
      mb.appendChild(card);
    })
  })
}
function tick(){countdown--;if(countdown<=0)refresh();document.getElementById('countdown').textContent=Math.max(countdown,0)}
document.addEventListener('click',function(e){var b=e.target;if(b.tagName==='BUTTON'&&b.dataset.uuid){kick(b.dataset.uuid,b.dataset.name)}});
refresh();setInterval(tick,1000);
</script></body></html>"""


def _make_admin_handler(server_ref):
    """Create an AdminHandler class bound to the CoupServer instance."""

    class AdminHandler(BaseHTTPRequestHandler):
        coup_server = server_ref

        def log_message(self, fmt, *args):
            log.debug("Admin HTTP: " + fmt, *args)

        def _check_auth(self):
            # Trust nginx proxy (sets X-Admin-Auth after its own auth_basic)
            if self.headers.get("X-Admin-Auth") == "nginx-verified":
                return True
            # Direct access on :9090 — check Basic Auth
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
            srv = self.coup_server
            if user != srv._admin_user or pwd != srv._admin_password:
                self._send_auth_required()
                return False
            return True

        def _send_auth_required(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Coup Admin"')
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

            srv = self.coup_server
            if path == "/api/kick":
                uuid = data.get("uuid", "")
                if not uuid:
                    self._send_json({"error": "missing uuid"}, 400)
                    return
                srv._admin_command_queue.put({"cmd": "kick", "uuid": uuid})
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
            srv = self.coup_server
            now = time.time()
            players = []
            for sock, info in list(srv.clients.items()):
                if not info.authenticated:
                    continue
                if info.in_game and info.alive:
                    status = "in-game"
                elif info.in_game and not info.alive:
                    status = "dead"
                elif info.is_spectating:
                    status = "spectating"
                else:
                    status = "lobby"
                players.append({
                    "username": info.username,
                    "uuid": info.uuid,
                    "status": status,
                    "address": str(info.address),
                    "idle_activity": round(now - info.last_activity, 1),
                    "idle_action": round(now - info.last_user_action, 1),
                    "consecutive_timeouts": info.consecutive_turn_timeouts,
                    "ready": info.ready,
                })

            # Game info
            engine = srv.engine
            phase_name = "-"
            current_player = "-"
            if engine:
                phase_name = str(engine.phase())
                pid = engine.current_player()
                current_player = srv._pid_to_name(pid)

            deadline_remaining = max(0, srv.deadline - now) if srv.game_active else -1

            return {
                "uptime": round(now - srv._start_time, 1),
                "players": players,
                "game": {
                    "active": srv.game_active,
                    "phase": phase_name,
                    "current_player": current_player,
                    "deadline_remaining": round(deadline_remaining, 1),
                    "relay_count": len(srv.relay_log),
                    "human_count": len([i for i in srv.clients.values() if i.authenticated and i.in_game]),
                    "bot_count": len(srv.in_process_bots) + len(srv.lobby_bots),
                },
            }

    return AdminHandler


# ---------------------------------------------------------------------------
# Game Server
# ---------------------------------------------------------------------------

class CoupServer:
    def __init__(self, host="0.0.0.0", port=4821, ws_port=0,
                 admin_port=0, admin_user="admin", admin_password="coup2025"):
        self.host = host
        self.port = port
        self.ws_port = ws_port
        self.clients = {}       # {socket: ClientInfo}
        self.uuid_map = {}      # {uuid: username}
        self.next_user_id = 1
        self.server_socket = None
        self._running = False
        self._start_time = time.time()

        # Admin portal
        self._admin_port = admin_port
        self._admin_user = admin_user
        self._admin_password = admin_password
        self._admin_command_queue = queue.Queue()
        self._admin_httpd = None
        self._admin_thread = None

        # Shared secret auth
        self.pending_auth = {}      # {socket: {"addr": addr, "buf": b"", "time": float}}
        self.port_locked = False
        self.authenticated_bridges = set()  # sockets of authenticated bridges

        # Game state
        self.game_active = False
        self.turn_order = []            # list of ClientInfo in play order

        # C engine (if available)
        self.engine = None              # CoupEngine instance
        self.engine_seed = 0            # seed used for engine init
        self.engine_pid_to_client = {}  # {engine_pid: ClientInfo}
        self.engine_uid_to_pid = {}     # {user_id: engine_pid}

        # Pending responses (used by translator for timeout tracking)
        self.pending_from = set()       # set of sockets we're waiting on
        self.deadline = 0.0             # time.time() when window expires

        # Relay sequence tracking
        self.relay_seq = 0              # next sequence number to assign
        self.relay_log = []             # list of (input_type, pid, data) for resync

        # In-process bots
        self.in_process_bots = {}       # {engine_pid: {difficulty, rng_state, name}}
        self.lobby_bots = []            # list of bot names in lobby (before game start)
        self._bot_next_tick = None      # shared timer for all bot evaluation

        # WebSocket support
        self.ws_proxies = {}        # {server_sock: WSClientProxy}
        self._ws_thread = None
        self._ws_loop = None        # asyncio event loop for WS thread

        # Persistence
        pdir = os.path.dirname(os.path.abspath(__file__))
        self._uuid_file = os.path.join(pdir, "uuid_mapping.txt")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load_persistence(self):
        if os.path.exists(self._uuid_file):
            try:
                with open(self._uuid_file, "r", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    for row in reader:
                        if len(row) >= 2 and row[0] and row[1]:
                            self.uuid_map[row[0]] = row[1]
                log.info("Loaded %d UUID mappings", len(self.uuid_map))
            except Exception as e:
                log.warning("Failed to load UUIDs: %s", e)

    def save_uuid_mapping(self):
        try:
            from io import StringIO
            buf = StringIO()
            writer = csv.writer(buf)
            for uid, username in self.uuid_map.items():
                writer.writerow([uid, username])
            with open(self._uuid_file, "w", encoding="utf-8") as f:
                f.write(buf.getvalue())
        except Exception as e:
            log.warning("Failed to save UUIDs: %s", e)

    # ------------------------------------------------------------------
    # Networking
    # ------------------------------------------------------------------

    def send_to(self, sock, frame):
        if sock not in self.clients:
            return
        proxy = self.ws_proxies.get(sock)
        if proxy:
            # Route through WebSocket as a binary message
            try:
                ws = proxy.websocket
                loop = self._ws_loop
                if loop and not loop.is_closed():
                    asyncio.run_coroutine_threadsafe(ws.send(frame), loop)
            except Exception:
                self.disconnect_client(sock)
            return
        try:
            sock.sendall(frame)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.disconnect_client(sock)

    def broadcast(self, frame, exclude=None):
        for sock, info in list(self.clients.items()):
            if info.authenticated and sock is not exclude:
                self.send_to(sock, frame)

    def broadcast_to_game(self, frame, exclude=None):
        for sock, info in list(self.clients.items()):
            if (info.in_game or info.is_spectating) and sock is not exclude:
                self.send_to(sock, frame)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def handle_new_connection(self, sock):
        try:
            client_sock, addr = sock.accept()
            client_sock.setblocking(False)
            self.pending_auth[client_sock] = {
                "addr": addr,
                "buf": b"",
                "time": time.time(),
            }
            log.info("New connection from %s (awaiting auth)", addr)
        except OSError as e:
            log.warning("Accept failed: %s", e)

    def handle_pending_auth(self, sock):
        """Read auth handshake from a pending connection."""
        if sock not in self.pending_auth:
            return
        entry = self.pending_auth[sock]
        try:
            data = sock.recv(256)
        except (ConnectionResetError, OSError):
            self._silent_close_pending(sock)
            return
        if not data:
            self._silent_close_pending(sock)
            return

        entry["buf"] += data
        buf = entry["buf"]

        if len(buf) < 5:
            return

        if buf[:4] != AUTH_MAGIC:
            log.warning("Bad auth magic from %s — silent close", entry["addr"])
            self._silent_close_pending(sock)
            return

        secret_len = buf[4]
        expected_total = 5 + secret_len
        if len(buf) < expected_total:
            return

        received_secret = buf[5:expected_total]
        if received_secret == SHARED_SECRET:
            log.info("Auth SUCCESS from %s", entry["addr"])
            addr = entry["addr"]
            del self.pending_auth[sock]
            try:
                sock.sendall(bytes([AUTH_OK]))
            except OSError:
                try:
                    sock.close()
                except OSError:
                    pass
                return
            self.clients[sock] = ClientInfo(sock, addr)
            self.authenticated_bridges.add(sock)
            if len(self.authenticated_bridges) >= MAX_BRIDGES:
                self.lock_port()
        else:
            log.warning("Auth FAILED from %s — silent close", entry["addr"])
            self._silent_close_pending(sock)

    def _silent_close_pending(self, sock):
        self.pending_auth.pop(sock, None)
        try:
            sock.close()
        except OSError:
            pass

    def check_auth_timeouts(self):
        now = time.time()
        for sock in list(self.pending_auth.keys()):
            entry = self.pending_auth[sock]
            if now - entry["time"] > AUTH_TIMEOUT:
                log.warning("Auth timeout from %s — silent close", entry["addr"])
                self._silent_close_pending(sock)

    def lock_port(self):
        if self.server_socket:
            log.info("Port locked — closing listener on port %d", self.port)
            try:
                self.server_socket.close()
            except OSError:
                pass
            self.server_socket = None
            self.port_locked = True
            for sock in list(self.pending_auth.keys()):
                self._silent_close_pending(sock)

    def reopen_listener(self):
        if self.server_socket:
            return
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.setblocking(False)
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(16)
            self.port_locked = False
            log.info("Listener re-opened on %s:%d — waiting for authenticated bridge",
                     self.host, self.port)
        except OSError as e:
            log.error("Failed to re-open listener: %s", e)
            self.server_socket = None

    def disconnect_client(self, sock, ws_close_code=None):
        if sock not in self.clients:
            return
        info = self.clients.pop(sock)
        was_bridge = (sock in self.authenticated_bridges)
        self.authenticated_bridges.discard(sock)

        # Clean up WebSocket proxy if applicable
        proxy = self.ws_proxies.pop(sock, None)
        if proxy:
            proxy.close()
            # Close the WS connection from the async side
            loop = self._ws_loop
            if loop and not loop.is_closed():
                try:
                    asyncio.run_coroutine_threadsafe(proxy.websocket.close(ws_close_code or 1000), loop)
                except Exception:
                    pass
        else:
            try:
                sock.close()
            except OSError:
                pass

        log.info("Disconnected %s (user=%s)", info.address, info.username or "?")

        if was_bridge and self.port_locked:
            log.info("Authenticated bridge disconnected — re-opening listener")
            self.reopen_listener()

        self.pending_from.discard(sock)

        if info.in_game and info.alive and USE_C_ENGINE and self.engine:
            self._handle_disconnect_engine(info)
        elif info.in_game and info.alive and not USE_C_ENGINE:
            # Python fallback disconnect handling would go here
            pass

        if info.authenticated and not self.game_active:
            self.broadcast_lobby_state()

    def _handle_disconnect_engine(self, info):
        """Handle disconnect of a player mid-game using C engine."""
        pid = info.engine_pid
        if pid < 0:
            return

        engine = self.engine
        phase = engine.phase()

        # Auto-submit appropriate actions to keep the game moving
        if phase == PHASE_WAITING_FOR_ACTION and engine.current_player() == pid:
            valid = engine.valid_actions()
            if valid & (1 << ACT_COUP) and not (valid & (1 << ACT_INCOME)):
                target = 0xFF
                for t in range(engine.player_count()):
                    if t != pid and engine.player_alive(t):
                        target = t
                        break
                self._relay_input(RELAY_ACTION, pid, bytes([ACT_COUP, target]))
            else:
                self._relay_input(RELAY_ACTION, pid, bytes([ACT_INCOME, 0xFF]))
        elif phase in (PHASE_CHALLENGE_WINDOW, PHASE_BLOCK_WINDOW, PHASE_BLOCK_CHALLENGE_WINDOW):
            if engine.pending_response(pid):
                self._relay_input(RELAY_RESPONSE, pid, bytes([RESP_PASS]))
        elif phase == PHASE_RESOLVING and engine.blocker_id() == pid:
            self._relay_input(RELAY_TIMEOUT, pid)
        elif phase == PHASE_WAITING_FOR_INFLUENCE_LOSS and engine.influence_loser() == pid:
            self._relay_input(RELAY_LOSE_INFLUENCE, pid, bytes([0]))
        elif phase == PHASE_WAITING_FOR_EXCHANGE and engine.exchange_player() == pid:
            self._relay_input(RELAY_EXCHANGE_CHOICE, pid, bytes([0, 1]))

        # Check if game ended
        if not engine.game_active():
            self._end_game()

    def handle_client_data(self, sock):
        if sock not in self.clients:
            return
        info = self.clients[sock]
        try:
            data = sock.recv(4096)
        except (ConnectionResetError, OSError):
            self.disconnect_client(sock)
            return
        if not data:
            self.disconnect_client(sock)
            return

        info.last_activity = time.time()
        info.recv_buffer += data

        if len(info.recv_buffer) > MAX_RECV_BUFFER:
            self.disconnect_client(sock)
            return

        while len(info.recv_buffer) >= 2:
            payload_len = struct.unpack("!H", info.recv_buffer[:2])[0]
            total = 2 + payload_len
            if len(info.recv_buffer) < total:
                break
            payload = info.recv_buffer[2:total]
            info.recv_buffer = info.recv_buffer[total:]
            if payload_len > 0:
                self.process_message(sock, payload)

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------

    def process_message(self, sock, payload):
        if sock not in self.clients:
            return
        info = self.clients[sock]
        if len(payload) < 1:
            return
        msg_type = payload[0]

        if msg_type == MSG_CONNECT:
            self._handle_connect(sock, info, payload)
        elif msg_type == MSG_SET_USERNAME:
            self._handle_set_username(sock, info, payload)
        elif msg_type == MSG_HEARTBEAT:
            pass
        elif msg_type == MSG_DISCONNECT:
            self.disconnect_client(sock)
        elif msg_type == COUP_MSG_READY:
            self._handle_ready(sock, info, payload)
        elif msg_type == COUP_MSG_ACTION:
            self._handle_action(sock, info, payload)
        elif msg_type == COUP_MSG_RESPONSE:
            self._handle_response(sock, info, payload)
        elif msg_type == COUP_MSG_BLOCK_CLAIM:
            self._handle_block_claim(sock, info, payload)
        elif msg_type == COUP_MSG_LOSE_INFLUENCE:
            self._handle_lose_influence(sock, info, payload)
        elif msg_type == COUP_MSG_EXCHANGE_CHOICE:
            self._handle_exchange_choice(sock, info, payload)
        elif msg_type == COUP_MSG_START_GAME_REQ:
            self._handle_start_game_request(sock, info)
        elif msg_type == COUP_MSG_ADD_BOT:
            self._handle_add_bot(sock, info, payload)
        elif msg_type == COUP_MSG_REMOVE_BOT:
            self._handle_remove_bot(sock, info, payload)
        elif msg_type == COUP_MSG_SET_BOT_DIFFICULTY:
            self._handle_set_bot_difficulty(sock, info, payload)
        elif msg_type == COUP_MSG_RESYNC_REQ:
            self._handle_resync_req(sock, info, payload)

    # ------------------------------------------------------------------
    # Auth handlers
    # ------------------------------------------------------------------

    def _handle_connect(self, sock, info, payload):
        client_uuid = ""
        if len(payload) >= 1 + UUID_LEN:
            client_uuid = payload[1:1 + UUID_LEN].decode("ascii", errors="replace").rstrip("\x00")

        if client_uuid and client_uuid in self.uuid_map:
            username = self.uuid_map[client_uuid]
            info.uuid = client_uuid
            info.username = username
            info.user_id = self.next_user_id
            self.next_user_id += 1
            info.authenticated = True
            info.last_user_action = time.time()
            log.info("Returning user %s from %s", username, info.address)
            self.send_to(sock, build_welcome_back(info.user_id, client_uuid, username))
            self.broadcast_lobby_state()
        else:
            self.send_to(sock, build_username_required())

    def _handle_set_username(self, sock, info, payload):
        if info.authenticated:
            return
        if len(payload) < 2:
            return
        name_len = payload[1]
        if len(payload) < 2 + name_len:
            return
        username = payload[2:2 + name_len].decode("utf-8", errors="replace").strip()

        if not username or len(username) > USERNAME_MAX_LEN:
            self.send_to(sock, build_username_taken())
            return

        lower = username.lower()
        for ci in self.clients.values():
            if ci.authenticated and ci.username.lower() == lower:
                self.send_to(sock, build_username_taken())
                return

        new_uuid = str(uuid_mod.uuid4())
        self.uuid_map[new_uuid] = username
        self.save_uuid_mapping()

        info.uuid = new_uuid
        info.username = username
        info.user_id = self.next_user_id
        self.next_user_id += 1
        info.authenticated = True
        info.last_user_action = time.time()

        log.info("New user %s (id=%d) from %s", username, info.user_id, info.address)
        self.send_to(sock, build_welcome(info.user_id, new_uuid, username))
        self.broadcast_lobby_state()

        if self.game_active:
            # Enroll as spectator
            info.is_spectating = True
            total_relays = len(self.relay_log)
            # Send GAME_START with spectator sentinel pid (0xFF)
            player_order = [p.user_id for p in self.turn_order]
            for _ in self.in_process_bots:
                player_order.append(0xFF)
            self.send_to(sock, build_game_start(0xFF, self.engine_seed, player_order))
            # Send RESYNC_FULL with spectator pid + relay count
            self.send_to(sock, build_resync_full(self.engine_seed, 0xFF, total_relays))
            # Replay all relays
            for idx, (input_type, pid, data) in enumerate(self.relay_log):
                self.send_to(sock, build_input_relay(input_type, pid, data, seq=idx))
            self.send_to(sock, build_log("Spectating game in progress..."))
        else:
            players = self.get_auth_players()
            idx = next((i for i, p in enumerate(players) if p is info), -1)
            if idx >= MAX_GAME_PLAYERS:
                queue_pos = idx - MAX_GAME_PLAYERS + 1
                self.send_to(sock, build_log(f"Lobby full - queue position #{queue_pos}"))
                self.send_to(sock, build_log("You'll join next game"))

    # ------------------------------------------------------------------
    # Lobby
    # ------------------------------------------------------------------

    def get_auth_players(self):
        return [info for info in self.clients.values() if info.authenticated]

    def broadcast_lobby_state(self):
        players = []
        for info in self.get_auth_players():
            players.append({"id": info.user_id, "name": info.username,
                            "ready": info.ready, "is_bot": False, "difficulty": 0})
        # Include lobby bots (always ready, IDs after human players)
        next_id = max((p["id"] for p in players), default=-1) + 1
        for bot in self.lobby_bots:
            players.append({"id": next_id, "name": bot["name"], "ready": True,
                            "is_bot": True, "difficulty": bot.get("difficulty", BOT_DIFFICULTY_DEFAULT)})
            next_id += 1
        frame = build_lobby_state(players)
        # Only send to non-in-game clients (LOBBY_STATE corrupts Saturn game state)
        for sock, info in list(self.clients.items()):
            if info.authenticated and not info.in_game:
                self.send_to(sock, frame)

    def _handle_ready(self, sock, info, payload):
        if not info.authenticated or self.game_active:
            return
        info.last_user_action = time.time()

        players = self.get_auth_players()
        idx = next((i for i, p in enumerate(players) if p is info), -1)
        if idx >= MAX_GAME_PLAYERS:
            queue_pos = idx - MAX_GAME_PLAYERS + 1
            self.send_to(sock, build_log(f"Game full ({MAX_GAME_PLAYERS}/{MAX_GAME_PLAYERS})"))
            self.send_to(sock, build_log(f"You are #{queue_pos} in queue"))
            self.send_to(sock, build_log("Waiting for next game..."))
            return

        if len(payload) >= 2:
            info.ready = bool(payload[1])
        else:
            info.ready = not info.ready
        log.info("%s is now %s", info.username, "READY" if info.ready else "not ready")
        self.broadcast_lobby_state()

    def _handle_start_game_request(self, sock, info):
        """Any player can start once ALL eligible humans are ready and 2+ total."""
        if not info.authenticated or self.game_active:
            return
        info.last_user_action = time.time()

        players = self.get_auth_players()
        eligible_humans = players[:MAX_GAME_PLAYERS]

        if not all(p.ready for p in eligible_humans):
            self.send_to(sock, build_log("All players must be ready"))
            return

        total_ready = len(eligible_humans) + len(self.lobby_bots)
        if total_ready < 2:
            self.send_to(sock, build_log("Need 2+ players to start"))
            return

        game_players = eligible_humans
        log.info("%s requested game start with %d humans + %d bots",
                 info.username, len(game_players), len(self.lobby_bots))

        self.broadcast_lobby_state()
        self.broadcast(build_log(f"{info.username} started the game!"))
        self.start_game(game_players)

        for qp in players[MAX_GAME_PLAYERS:]:
            queue_pos = players.index(qp) - MAX_GAME_PLAYERS + 1
            self.send_to(qp.socket, build_log("Game started - you're in queue"))
            self.send_to(qp.socket, build_log(f"Queue position: #{queue_pos}"))

    # ------------------------------------------------------------------
    # Input relay helpers
    # ------------------------------------------------------------------

    def _pid_to_name(self, pid):
        """Engine player_id -> username string."""
        client = self.engine_pid_to_client.get(pid)
        if client:
            return client.username
        bot_state = self.in_process_bots.get(pid)
        if bot_state:
            return bot_state["name"]
        return f"Player {pid}"

    def _relay_input(self, input_type, pid, data=b""):
        """Submit input to the engine then broadcast INPUT_RELAY to all clients.

        Performs the engine submit, broadcasts the relay, updates pending/deadline,
        and broadcasts any log messages derived from engine events.
        """
        engine = self.engine
        if not engine:
            return

        # Perform the engine submit based on input_type
        if input_type == RELAY_START_GAME:
            log.info("Engine submit_start: %d players, phase=%d", engine.player_count(), engine.phase())
            result = engine.submit_start()
            log.info("Engine submit_start result=%d, new phase=%d", result, engine.phase())
        elif input_type == RELAY_ACTION:
            result = engine.submit_action(pid, data[0], data[1])
        elif input_type == RELAY_RESPONSE:
            result = engine.submit_response(pid, data[0])
        elif input_type == RELAY_BLOCK_CLAIM:
            result = engine.submit_block_claim(pid, data[0])
        elif input_type == RELAY_LOSE_INFLUENCE:
            result = engine.submit_lose_influence(pid, data[0])
        elif input_type == RELAY_EXCHANGE_CHOICE:
            result = engine.submit_exchange(pid, data[0], data[1])
        elif input_type == RELAY_TIMEOUT:
            result = engine.submit_timeout()
        else:
            return

        if result < 0:
            detail = ""
            if input_type == RELAY_ACTION and len(data) >= 2:
                detail = (f" action={data[0]} target={data[1]}"
                          f" phase={engine.phase()}"
                          f" current={engine.current_player()}"
                          f" coins={engine.player_coins(pid)}"
                          f" valid=0x{engine.valid_actions():02x}")
            log.warning("Engine rejected input_type=%d from pid=%d%s",
                        input_type, pid, detail)
            # Send ACTION_REJECTED to the submitting client
            if pid in self.engine_pid_to_client:
                info = self.engine_pid_to_client[pid]
                current_seq = (self.relay_seq - 1) & 0xFFFF if self.relay_seq > 0 else 0
                self.send_to(info.socket, build_action_rejected(current_seq, engine.phase()))
            return

        # Assign sequence number and log for resync
        seq = self.relay_seq & 0xFFFF
        self.relay_seq += 1
        self.relay_log.append((input_type, pid, data))

        # Broadcast the INPUT_RELAY to all game clients
        self.broadcast_to_game(build_input_relay(input_type, pid, data, seq=seq))

        # Note: clients generate their own logs from their local engine
        # via process_rule_events(), so no server-side log broadcast needed.

        # Update info.alive for eliminated players
        for epid, client in self.engine_pid_to_client.items():
            if client.alive and not engine.player_alive(epid):
                client.alive = False

        # Reset bot timer when state changes so bots start fresh think delay
        if self.in_process_bots:
            self._bot_next_tick = None

        # Update pending_from and deadline from engine state
        self._update_pending_and_deadline()

    def _update_pending_and_deadline(self):
        """Set pending_from and deadline based on current engine phase."""
        engine = self.engine
        if not engine:
            return

        phase = engine.phase()
        self.pending_from.clear()

        if phase == PHASE_WAITING_FOR_ACTION:
            pid = engine.current_player()
            client = self.engine_pid_to_client.get(pid)
            if client and client.socket in self.clients:
                self.pending_from.add(client.socket)
            self.deadline = time.time() + TURN_TIMEOUT

        elif phase in (PHASE_CHALLENGE_WINDOW, PHASE_BLOCK_WINDOW, PHASE_BLOCK_CHALLENGE_WINDOW):
            for pid in range(engine.player_count()):
                if engine.pending_response(pid):
                    client = self.engine_pid_to_client.get(pid)
                    if client and client.socket in self.clients:
                        self.pending_from.add(client.socket)
            if phase == PHASE_CHALLENGE_WINDOW or phase == PHASE_BLOCK_CHALLENGE_WINDOW:
                self.deadline = time.time() + CHALLENGE_TIMEOUT
            else:
                self.deadline = time.time() + BLOCK_TIMEOUT

        elif phase == PHASE_WAITING_FOR_INFLUENCE_LOSS:
            pid = engine.influence_loser()
            if pid != 0xFF:
                client = self.engine_pid_to_client.get(pid)
                if client and client.socket in self.clients:
                    self.pending_from.add(client.socket)
            self.deadline = time.time() + INFLUENCE_TIMEOUT

        elif phase == PHASE_WAITING_FOR_EXCHANGE:
            pid = engine.exchange_player()
            if pid != 0xFF:
                client = self.engine_pid_to_client.get(pid)
                if client and client.socket in self.clients:
                    self.pending_from.add(client.socket)
            self.deadline = time.time() + EXCHANGE_TIMEOUT

        elif phase == PHASE_RESOLVING:
            # Waiting for block claim
            pid = engine.blocker_id()
            if pid != 0xFF:
                client = self.engine_pid_to_client.get(pid)
                if client and client.socket in self.clients:
                    self.pending_from.add(client.socket)
            self.deadline = time.time() + BLOCK_TIMEOUT

    # ------------------------------------------------------------------
    # Game start
    # ------------------------------------------------------------------

    def start_game(self, players):
        log.info("Starting game with %d players", len(players))
        self.game_active = True

        # Shuffle player order (test hook can override)
        self.turn_order = list(players)
        if hasattr(self, '_test_turn_order') and self._test_turn_order:
            # Reorder turn_order so the player with the specified username goes first
            order = []
            by_name = {p.username: p for p in self.turn_order}
            for name in self._test_turn_order:
                if name in by_name:
                    order.append(by_name[name])
            # Append any remaining players not in the override list
            for p in self.turn_order:
                if p not in order:
                    order.append(p)
            self.turn_order = order
        else:
            random.shuffle(self.turn_order)

        if USE_C_ENGINE:
            self._start_game_engine(self.turn_order)
        else:
            log.error("Python fallback game logic not available in this build")

    def _start_game_engine(self, players):
        """Initialize C engine and start the game.

        Sends GAME_START with seed to each client (per-player engine_pid),
        then broadcasts INPUT_RELAY(START_GAME) so all clients init their
        local engines identically.

        In-process bots from self.lobby_bots are added after human players.
        """
        bots = list(self.lobby_bots)
        total_count = len(players) + len(bots)
        if hasattr(self, '_test_seed') and self._test_seed is not None:
            seed = self._test_seed
        else:
            seed = random.randint(1, 0xFFFFFFFF)

        self.engine_seed = seed

        # Create engine with lobby flow
        self.engine = CoupEngine()
        self.engine.init(seed)
        # Add human players
        for i in range(len(players)):
            self.engine.submit_add_player()
            self.engine.submit_set_ready(i, 1)
        # Add in-process bots
        for i in range(len(bots)):
            self.engine.submit_add_bot()

        # Build ID mappings for human players
        self.engine_pid_to_client = {}
        self.engine_uid_to_pid = {}
        for pid, info in enumerate(players):
            info.in_game = True
            info.alive = True
            info.engine_pid = pid
            info.consecutive_turn_timeouts = 0
            self.engine_pid_to_client[pid] = info
            self.engine_uid_to_pid[info.user_id] = pid

        # Register in-process bots
        self.in_process_bots = {}
        self._bot_next_tick = None
        for i, bot_info in enumerate(bots):
            bot_pid = len(players) + i
            self.in_process_bots[bot_pid] = {
                "difficulty": bot_info.get("difficulty", BOT_DIFFICULTY_DEFAULT),
                "rng_state": seed ^ ((bot_pid + 1) * 2654435761) & 0xFFFFFFFF,
                "name": bot_info["name"],
            }

        # Build player order: list of user_ids in engine PID order
        player_order = [info.user_id for info in players]
        # Append bot placeholder IDs (bots don't have user_ids, use 0xFF)
        for i in range(len(bots)):
            player_order.append(0xFF)

        # Send GAME_START to each player with their own engine_pid + full order
        for pid, info in enumerate(players):
            frame = build_game_start(pid, seed, player_order)
            self.send_to(info.socket, frame)

        # Broadcast INPUT_RELAY(START_GAME) — triggers engine init + submit on clients
        self._relay_input(RELAY_START_GAME, 0)

    # ------------------------------------------------------------------
    # Game action handlers (C engine)
    # ------------------------------------------------------------------

    def _handle_action(self, sock, info, payload):
        if not info.in_game or not info.alive:
            log.debug("Action dropped: in_game=%s alive=%s user=%s",
                      info.in_game, info.alive, info.username)
            return
        if not USE_C_ENGINE or not self.engine:
            return
        if len(payload) < 3:
            return

        engine = self.engine
        phase = engine.phase()
        if phase != PHASE_WAITING_FOR_ACTION:
            log.warning("Action dropped from %s (pid=%d): phase=%d not WAITING_FOR_ACTION, "
                        "current_player=%d",
                        info.username, info.engine_pid, phase, engine.current_player())
            current_seq = (self.relay_seq - 1) & 0xFFFF if self.relay_seq > 0 else 0
            self.send_to(sock, build_action_rejected(current_seq, phase))
            return

        pid = info.engine_pid
        if pid < 0 or engine.current_player() != pid:
            log.warning("Action dropped from %s (pid=%d): not current player (current=%d)",
                        info.username, pid, engine.current_player())
            current_seq = (self.relay_seq - 1) & 0xFFFF if self.relay_seq > 0 else 0
            self.send_to(sock, build_action_rejected(current_seq, phase))
            return

        info.consecutive_turn_timeouts = 0  # voluntary action resets timeout counter

        action = payload[1]
        target = payload[2]

        # Client sends engine_pid as target, but Saturn clients may send
        # user_id if LOBBY_STATE corrupted their PID mapping — translate
        target_pid = 0xFF
        if ACTION_NEEDS_TARGET[action] if action < 7 else False:
            target_pid = target
            # Safety: if target looks like a user_id, translate to engine_pid
            if target_pid >= engine.player_count():
                translated = self.engine_uid_to_pid.get(target_pid, 0xFF)
                if translated != 0xFF:
                    log.info("Translated target uid=%d -> pid=%d for %s",
                             target_pid, translated, info.username)
                    target_pid = translated

        log.info("Action from %s (pid=%d): action=%d target=%d coins=%d valid=0x%02x",
                 info.username, pid, action, target_pid,
                 engine.player_coins(pid), engine.valid_actions())
        self._relay_input(RELAY_ACTION, pid, bytes([action, target_pid]))

        if not engine.game_active():
            self._end_game()

    def _handle_response(self, sock, info, payload):
        if not info.in_game or not info.alive:
            return
        if not USE_C_ENGINE or not self.engine:
            return
        if len(payload) < 2:
            return

        pid = info.engine_pid
        response = payload[1]

        # Only accept responses during response windows
        phase = self.engine.phase()
        if phase not in (PHASE_CHALLENGE_WINDOW, PHASE_BLOCK_WINDOW, PHASE_BLOCK_CHALLENGE_WINDOW):
            log.debug("Response dropped from %s (pid=%d): phase=%d not a response window",
                      info.username, pid, phase)
            current_seq = (self.relay_seq - 1) & 0xFFFF if self.relay_seq > 0 else 0
            self.send_to(sock, build_action_rejected(current_seq, phase))
            return
        if not self.engine.pending_response(pid):
            log.debug("Response dropped from %s (pid=%d): not pending in phase=%d",
                      info.username, pid, phase)
            current_seq = (self.relay_seq - 1) & 0xFFFF if self.relay_seq > 0 else 0
            self.send_to(sock, build_action_rejected(current_seq, phase))
            return

        self.pending_from.discard(sock)
        self._relay_input(RELAY_RESPONSE, pid, bytes([response]))

        if not self.engine.game_active():
            self._end_game()

    def _handle_block_claim(self, sock, info, payload):
        if not info.in_game or not info.alive:
            return
        if not USE_C_ENGINE or not self.engine:
            return
        if len(payload) < 2:
            return

        pid = info.engine_pid
        block_char = payload[1]

        # Only accept block claims during RESOLVING phase from the blocker
        if self.engine.phase() != PHASE_RESOLVING:
            return
        if self.engine.blocker_id() != pid:
            return

        self.pending_from.discard(sock)
        self._relay_input(RELAY_BLOCK_CLAIM, pid, bytes([block_char]))

        if not self.engine.game_active():
            self._end_game()

    def _handle_lose_influence(self, sock, info, payload):
        if not info.in_game or not info.alive:
            return
        if not USE_C_ENGINE or not self.engine:
            return
        if len(payload) < 2:
            return

        pid = info.engine_pid
        card_idx = payload[1]

        self.pending_from.discard(sock)
        self._relay_input(RELAY_LOSE_INFLUENCE, pid, bytes([card_idx]))

        if not self.engine.game_active():
            self._end_game()

    def _handle_exchange_choice(self, sock, info, payload):
        if not info.in_game or not info.alive:
            return
        if not USE_C_ENGINE or not self.engine:
            return
        if len(payload) < 3:
            return

        pid = info.engine_pid
        keep0 = payload[1]
        keep1 = payload[2]

        self.pending_from.discard(sock)
        self._relay_input(RELAY_EXCHANGE_CHOICE, pid, bytes([keep0, keep1]))

        if not self.engine.game_active():
            self._end_game()

    # ------------------------------------------------------------------
    # Resync handler
    # ------------------------------------------------------------------

    def _handle_resync_req(self, sock, info, payload):
        """Handle RESYNC_REQ: send missed relays or full resync."""
        if not info.in_game:
            return
        if len(payload) < 3:
            return

        last_seen_seq = (payload[1] << 8) | payload[2]
        total = len(self.relay_log)

        # Calculate how many relays the client is missing
        # last_seen_seq is the seq of the last relay the client received
        # Relays are numbered 0..total-1
        start_idx = last_seen_seq + 1
        if start_idx < 0 or start_idx > total:
            # Out of range — send full resync
            self.send_to(sock, build_resync_full(
                self.engine_seed, info.engine_pid, total))
            # Replay all relays
            for idx, (input_type, pid, data) in enumerate(self.relay_log):
                self.send_to(sock, build_input_relay(
                    input_type, pid, data, seq=idx))
            return

        gap = total - start_idx
        if gap <= 0:
            # Client is up to date — send empty RESYNC so client knows
            self.send_to(sock, build_resync([]))
            return

        if gap > 50:
            # Gap too large — send full resync
            self.send_to(sock, build_resync_full(
                self.engine_seed, info.engine_pid, total))
            for idx, (input_type, pid, data) in enumerate(self.relay_log):
                self.send_to(sock, build_input_relay(
                    input_type, pid, data, seq=idx))
            return

        # Incremental resync: batch missed relays
        entries = []
        for idx in range(start_idx, total):
            input_type, pid, data = self.relay_log[idx]
            entries.append((idx, input_type, pid, data))
        self.send_to(sock, build_resync(entries))

    # ------------------------------------------------------------------
    # Game end / reset
    # ------------------------------------------------------------------

    def _end_game(self):
        """Reset server state after game over."""
        self.game_active = False
        self.pending_from.clear()

        for info in self.clients.values():
            info.in_game = False
            info.ready = False
            info.alive = True
            info.engine_pid = -1
            info.is_spectating = False
            info.last_user_action = time.time()  # prevent instant lobby kick after long game
            info.consecutive_turn_timeouts = 0

        self.engine = None
        self.engine_seed = 0
        self.engine_pid_to_client = {}
        self.engine_uid_to_pid = {}
        self.in_process_bots = {}
        self._bot_next_tick = None
        # lobby_bots intentionally preserved — bots persist across games
        self.turn_order = []
        self.relay_log = []
        self.relay_seq = 0

        self.broadcast_lobby_state()

        # Notify queued players
        all_players = self.get_auth_players()
        if len(all_players) > MAX_GAME_PLAYERS:
            for qp in all_players[MAX_GAME_PLAYERS:]:
                queue_pos = all_players.index(qp) - MAX_GAME_PLAYERS + 1
                self.send_to(qp.socket, build_log("Game ended! Spots available."))
                self.send_to(qp.socket, build_log(f"Queue position: #{queue_pos}"))

    # ------------------------------------------------------------------
    # Timeout handling
    # ------------------------------------------------------------------

    def _force_timeout_for_phase(self, phase):
        """Force-resolve a phase when no clients are pending (e.g. disconnect)."""
        engine = self.engine
        if not engine:
            return

        if phase == PHASE_WAITING_FOR_ACTION:
            pid = engine.current_player()
            # Use valid_actions to pick an allowed action (Coup when forced)
            valid = engine.valid_actions()
            if valid & (1 << ACT_COUP) and not (valid & (1 << ACT_INCOME)):
                action = ACT_COUP
                # Need a living target for Coup
                target = 0xFF
                for t in range(engine.player_count()):
                    if t != pid and engine.player_alive(t):
                        target = t
                        break
                action_name = "Coup"
            else:
                action = ACT_INCOME
                target = 0xFF
                action_name = "Income"
            name = self.engine_pid_to_client.get(pid)
            if name:
                self.broadcast_to_game(
                    build_log(f"{name.username} disconnected, forced {action_name}"))
            self._relay_input(RELAY_ACTION, pid, bytes([action, target]))

        elif phase in (PHASE_CHALLENGE_WINDOW, PHASE_BLOCK_WINDOW,
                       PHASE_BLOCK_CHALLENGE_WINDOW, PHASE_RESOLVING):
            self._relay_input(RELAY_TIMEOUT, 0)

        elif phase == PHASE_WAITING_FOR_INFLUENCE_LOSS:
            pid = engine.influence_loser()
            if pid != 0xFF:
                self._relay_input(RELAY_LOSE_INFLUENCE, pid, bytes([0]))

        elif phase == PHASE_WAITING_FOR_EXCHANGE:
            pid = engine.exchange_player()
            if pid != 0xFF:
                self._relay_input(RELAY_EXCHANGE_CHOICE, pid, bytes([0, 1]))

        if not engine.game_active():
            self._end_game()

    def check_timeouts(self):
        """Check if any pending response windows have expired."""
        if not self.game_active:
            return
        if not USE_C_ENGINE or not self.engine:
            return

        engine = self.engine

        # Abort game if no human clients remain connected
        # Note: in_game is enough — a dead player is still watching
        has_human = any(
            info.in_game
            for info in self.clients.values()
        )
        if not has_human:
            log.info("All human players disconnected — aborting game")
            self._end_game()
            return

        # Prune dead bots so they never block anything
        dead = [pid for pid in self.in_process_bots
                if not engine.player_alive(pid)]
        for pid in dead:
            del self.in_process_bots[pid]

        # If no one is pending (e.g. the pending player disconnected),
        # force an immediate timeout to unstick the game.
        # But skip if a living bot still needs to act — tick_bots handles them.
        if not self.pending_from:
            phase = engine.phase()
            if phase in (PHASE_WAITING_FOR_ACTION, PHASE_CHALLENGE_WINDOW,
                         PHASE_BLOCK_WINDOW, PHASE_BLOCK_CHALLENGE_WINDOW,
                         PHASE_RESOLVING, PHASE_WAITING_FOR_INFLUENCE_LOSS,
                         PHASE_WAITING_FOR_EXCHANGE):
                bot_pending = False
                if self.in_process_bots:
                    if phase == PHASE_WAITING_FOR_ACTION:
                        bot_pending = engine.current_player() in self.in_process_bots
                    elif phase in (PHASE_CHALLENGE_WINDOW, PHASE_BLOCK_WINDOW,
                                   PHASE_BLOCK_CHALLENGE_WINDOW):
                        for pid in self.in_process_bots:
                            if engine.pending_response(pid):
                                bot_pending = True
                                break
                    elif phase == PHASE_WAITING_FOR_INFLUENCE_LOSS:
                        bot_pending = engine.influence_loser() in self.in_process_bots
                    elif phase == PHASE_WAITING_FOR_EXCHANGE:
                        bot_pending = engine.exchange_player() in self.in_process_bots
                    elif phase == PHASE_RESOLVING:
                        blocker = engine.blocker_id()
                        bot_pending = blocker != 0xFF and blocker in self.in_process_bots
                if not bot_pending:
                    log.info("No pending clients in phase %d — forcing timeout", phase)
                    self._force_timeout_for_phase(phase)
            return

        if time.time() < self.deadline:
            return

        engine = self.engine
        phase = engine.phase()

        log.info("Timeout in phase %d, %d pending", phase, len(self.pending_from))

        if phase == PHASE_WAITING_FOR_ACTION:
            pid = engine.current_player()
            # Use valid_actions to pick an allowed action (Coup when forced)
            valid = engine.valid_actions()
            if valid & (1 << ACT_COUP) and not (valid & (1 << ACT_INCOME)):
                action = ACT_COUP
                target = 0xFF
                for t in range(engine.player_count()):
                    if t != pid and engine.player_alive(t):
                        target = t
                        break
                action_name = "Coup"
            else:
                action = ACT_INCOME
                target = 0xFF
                action_name = "Income"
            client = self.engine_pid_to_client.get(pid)
            if client:
                self.broadcast_to_game(
                    build_log(f"{client.username} timed out, forced {action_name}"))
            self._relay_input(RELAY_ACTION, pid, bytes([action, target]))

            # Track consecutive turn timeouts for idle kick
            if client:
                client.consecutive_turn_timeouts += 1
                if client.consecutive_turn_timeouts >= MAX_CONSECUTIVE_TURN_TIMEOUTS:
                    self.broadcast_to_game(build_log(f"{client.username} kicked (idle 2 turns)"))
                    self.disconnect_client(client.socket, ws_close_code=4001)

        elif phase in (PHASE_CHALLENGE_WINDOW, PHASE_BLOCK_WINDOW, PHASE_BLOCK_CHALLENGE_WINDOW):
            self.pending_from.clear()
            self._relay_input(RELAY_TIMEOUT, 0)

        elif phase == PHASE_RESOLVING:
            self.pending_from.clear()
            self._relay_input(RELAY_TIMEOUT, 0)

        elif phase == PHASE_WAITING_FOR_INFLUENCE_LOSS:
            pid = engine.influence_loser()
            if pid != 0xFF:
                self.pending_from.clear()
                self._relay_input(RELAY_LOSE_INFLUENCE, pid, bytes([0]))

        elif phase == PHASE_WAITING_FOR_EXCHANGE:
            pid = engine.exchange_player()
            if pid != 0xFF:
                self.pending_from.clear()
                self._relay_input(RELAY_EXCHANGE_CHOICE, pid, bytes([0, 1]))

        if not engine.game_active():
            self._end_game()

    # ------------------------------------------------------------------
    # In-process bot management
    # ------------------------------------------------------------------

    def _handle_add_bot(self, sock, info, payload):
        """Add an in-process bot to the lobby."""
        if self.game_active:
            self.send_to(sock, build_log("Can't add bots during a game"))
            return
        if not info.authenticated:
            return
        info.last_user_action = time.time()
        difficulty = payload[1] if len(payload) >= 2 else BOT_DIFFICULTY_DEFAULT
        if len(self.lobby_bots) >= MAX_GAME_PLAYERS - 1:
            self.send_to(sock, build_log("Too many bots"))
            return
        name_idx = len(self.lobby_bots) % len(BOT_NAMES)
        bot_name = BOT_NAMES[name_idx]
        self.lobby_bots.append({"name": bot_name, "difficulty": difficulty})
        log.info("Bot added: %s (difficulty=%d)", bot_name, difficulty)
        self.broadcast_lobby_state()

    def _handle_remove_bot(self, sock, info, payload):
        """Remove last in-process bot from the lobby."""
        if self.game_active:
            self.send_to(sock, build_log("Can't remove bots during a game"))
            return
        if not info.authenticated:
            return
        info.last_user_action = time.time()
        if not self.lobby_bots:
            return
        removed = self.lobby_bots.pop()
        log.info("Bot removed: %s", removed["name"])
        self.broadcast_lobby_state()

    def _handle_set_bot_difficulty(self, sock, info, payload):
        """Set difficulty for an in-process bot."""
        if self.game_active:
            return
        if not info.authenticated:
            return
        info.last_user_action = time.time()
        if len(payload) < 3:
            return
        bot_index = payload[1]
        difficulty = payload[2]
        if bot_index >= len(self.lobby_bots):
            return
        self.lobby_bots[bot_index]["difficulty"] = difficulty
        log.info("Bot %s difficulty set to %d", self.lobby_bots[bot_index]["name"], difficulty)
        self.broadcast_lobby_state()

    def _submit_bot_decision(self, engine_pid, decision):
        """Submit a bot decision to the engine via _relay_input."""
        input_type = decision["type"]
        pid = decision["player_id"]

        if input_type == 1:  # ACTION
            self._relay_input(RELAY_ACTION, pid,
                              bytes([decision["action"], decision["target"]]))
        elif input_type == 2:  # RESPONSE
            self._relay_input(RELAY_RESPONSE, pid,
                              bytes([decision["response"]]))
            # If the bot decided to block, also send the block claim
            if decision.get("block_claim"):
                engine = self.engine
                if engine and engine.phase() == PHASE_RESOLVING:
                    self._relay_input(
                        RELAY_BLOCK_CLAIM, pid,
                        bytes([decision["block_claim"]["character"]]))
        elif input_type == 4:  # LOSE_INFLUENCE
            self._relay_input(RELAY_LOSE_INFLUENCE, pid,
                              bytes([decision["card_idx"]]))
        elif input_type == 5:  # EXCHANGE_CHOICE
            self._relay_input(RELAY_EXCHANGE_CHOICE, pid,
                              bytes([decision["keep0"], decision["keep1"]]))

    def tick_bots(self):
        """Evaluate all bots every 1-3 seconds. Dead bots are pruned immediately."""
        if not self.game_active or not self.engine:
            return
        if not self.in_process_bots:
            return

        engine = self.engine
        now = time.time()

        # Prune dead bots immediately — they should never hold anything up
        dead = [pid for pid in self.in_process_bots
                if not engine.player_alive(pid)]
        for pid in dead:
            del self.in_process_bots[pid]
        if not self.in_process_bots:
            return

        # Single shared timer — all bots evaluated together
        if self._bot_next_tick is not None and now < self._bot_next_tick:
            return

        # Process all bots that need to act. After each submission the phase
        # may change, so loop until no bot needs to act.
        max_iter = len(self.in_process_bots) * 3  # safety bound
        for _ in range(max_iter):
            if not engine.game_active():
                break
            phase = engine.phase()
            acted = False

            for pid, bot_state in list(self.in_process_bots.items()):
                if not engine.player_alive(pid):
                    continue

                needs_action = False
                if phase == PHASE_WAITING_FOR_ACTION:
                    needs_action = (engine.current_player() == pid)
                elif phase in (PHASE_CHALLENGE_WINDOW, PHASE_BLOCK_WINDOW,
                               PHASE_BLOCK_CHALLENGE_WINDOW):
                    needs_action = engine.pending_response(pid)
                elif phase == PHASE_WAITING_FOR_INFLUENCE_LOSS:
                    needs_action = (engine.influence_loser() == pid)
                elif phase == PHASE_WAITING_FOR_EXCHANGE:
                    needs_action = (engine.exchange_player() == pid)
                elif phase == PHASE_RESOLVING:
                    needs_action = (engine.blocker_id() == pid)

                if not needs_action:
                    continue

                new_rng, decision = engine.bot_decide(
                    pid, bot_state["difficulty"], bot_state["rng_state"])
                bot_state["rng_state"] = new_rng

                if decision:
                    log.info("Bot %s (pid=%d) decides: type=%d phase=%d",
                             bot_state["name"], pid, decision["type"], phase)
                    self._submit_bot_decision(pid, decision)
                    acted = True
                    break  # phase may have changed, re-check from top

            if not acted:
                break  # no bot needed to act

        # Schedule next evaluation
        delay = BOT_THINK_DELAY_MIN + random.random() * (BOT_THINK_DELAY_MAX - BOT_THINK_DELAY_MIN)
        self._bot_next_tick = now + delay

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def check_heartbeats(self):
        now = time.time()
        for sock in list(self.clients.keys()):
            info = self.clients.get(sock)
            if info and (now - info.last_activity) > HEARTBEAT_TIMEOUT:
                log.warning("Heartbeat timeout: %s (%s)", info.username or "?", info.address)
                self.disconnect_client(sock)

    def check_lobby_inactivity(self):
        """Kick lobby players idle for LOBBY_INACTIVITY_TIMEOUT seconds."""
        if self.game_active:
            return
        now = time.time()
        for sock in list(self.clients.keys()):
            info = self.clients.get(sock)
            if not info or not info.authenticated:
                continue
            if info.in_game or info.is_spectating:
                continue
            idle = now - info.last_user_action
            if idle > LOBBY_INACTIVITY_TIMEOUT:
                log.info("Lobby inactivity kick: %s (idle %.0fs)", info.username, idle)
                self.send_to(sock, build_log("Kicked for inactivity (5 min idle)"))
                self.disconnect_client(sock, ws_close_code=4001)

    # ------------------------------------------------------------------
    # WebSocket support
    # ------------------------------------------------------------------

    def _start_ws_server(self):
        """Start WebSocket listener in a background thread."""
        if not self.ws_port or not HAS_WEBSOCKETS:
            return

        def ws_thread_main():
            self._ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._ws_loop)

            async def run_ws():
                async with websockets.asyncio.server.serve(
                    self._ws_handler,
                    self.host,
                    self.ws_port,
                    max_size=MAX_RECV_BUFFER,
                ) as server:
                    log.info("WebSocket server listening on %s:%d",
                             self.host, self.ws_port)
                    self._ws_server = server
                    await asyncio.Future()  # run forever

            try:
                self._ws_loop.run_until_complete(run_ws())
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.error("WebSocket server error: %s", e)
            finally:
                self._ws_loop.close()

        self._ws_thread = threading.Thread(target=ws_thread_main, daemon=True)
        self._ws_thread.start()

    async def _ws_handler(self, websocket):
        """Handle a single WebSocket connection: auth then relay."""
        addr = websocket.remote_address or ("ws-client", 0)
        log.info("WebSocket connection from %s", addr)

        # --- Phase 1: AUTH handshake (same protocol as TCP) ---
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=AUTH_TIMEOUT)
        except (asyncio.TimeoutError, Exception):
            log.warning("WS auth timeout/error from %s", addr)
            return

        if not isinstance(raw, (bytes, bytearray)):
            raw = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)

        if len(raw) < 5 or raw[:4] != AUTH_MAGIC:
            log.warning("WS bad auth magic from %s", addr)
            return

        secret_len = raw[4]
        if len(raw) < 5 + secret_len:
            log.warning("WS auth incomplete from %s", addr)
            return

        received_secret = raw[5:5 + secret_len]
        if received_secret != SHARED_SECRET:
            log.warning("WS auth FAILED from %s", addr)
            return

        await websocket.send(bytes([AUTH_OK]))
        log.info("WS auth SUCCESS from %s", addr)

        # --- Phase 2: Create proxy and register with server ---
        proxy = WSClientProxy(websocket, addr)
        self.ws_proxies[proxy.server_sock] = proxy
        self.pending_auth[proxy.server_sock] = None  # skip pending_auth — already authed
        del self.pending_auth[proxy.server_sock]
        self.clients[proxy.server_sock] = ClientInfo(proxy.server_sock, addr)
        self.authenticated_bridges.add(proxy.server_sock)
        if len(self.authenticated_bridges) >= MAX_BRIDGES:
            self.lock_port()

        # --- Phase 3: Relay WS messages into socketpair ---
        try:
            async for message in websocket:
                if not isinstance(message, (bytes, bytearray)):
                    continue
                proxy.inject(message)
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            log.debug("WS receive error from %s: %s", addr, e)
        finally:
            # Trigger disconnect on the main thread via socketpair close
            if proxy.server_sock in self.clients:
                self.disconnect_client(proxy.server_sock)

    def _stop_ws_server(self):
        """Shut down the WebSocket server thread."""
        if self._ws_loop and not self._ws_loop.is_closed():
            self._ws_loop.call_soon_threadsafe(self._ws_loop.stop)
        if self._ws_thread:
            self._ws_thread.join(timeout=3)

    # ------------------------------------------------------------------
    # Admin portal
    # ------------------------------------------------------------------

    def _start_admin_server(self):
        """Start admin HTTP server in a daemon thread."""
        if not self._admin_port:
            return
        handler_class = _make_admin_handler(self)
        try:
            self._admin_httpd = ThreadingHTTPServer(("0.0.0.0", self._admin_port), handler_class)
            self._admin_httpd.daemon_threads = True
        except OSError as e:
            log.error("Failed to start admin server on port %d: %s", self._admin_port, e)
            return
        self._admin_thread = threading.Thread(target=self._admin_httpd.serve_forever, daemon=True)
        self._admin_thread.start()
        log.info("Admin portal listening on http://0.0.0.0:%d/", self._admin_port)

    def _stop_admin_server(self):
        """Shut down admin HTTP server."""
        if self._admin_httpd:
            self._admin_httpd.shutdown()
        if self._admin_thread:
            self._admin_thread.join(timeout=3)

    def _drain_admin_commands(self):
        """Process queued admin commands (called from main loop)."""
        while True:
            try:
                cmd = self._admin_command_queue.get_nowait()
            except queue.Empty:
                break

            action = cmd.get("cmd")
            if action == "kick":
                target_uuid = cmd.get("uuid", "")
                found = False
                for sock, info in list(self.clients.items()):
                    if info.uuid == target_uuid:
                        log.info("Admin kick: %s", info.username)
                        self.send_to(sock, build_log("Kicked by admin"))
                        self.disconnect_client(sock, ws_close_code=4001)
                        found = True
                        break
                if not found:
                    log.warning("Admin kick: UUID %s not found", target_uuid)

            elif action == "end_game":
                if self.game_active:
                    log.info("Admin: ending game")
                    self.broadcast_to_game(build_log("Game ended by admin"))
                    self._end_game()
                else:
                    log.info("Admin: end_game requested but no active game")

            elif action == "restart":
                log.info("Admin: restart requested")
                self._running = False

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        self.load_persistence()

        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.setblocking(False)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(16)
        self.port = self.server_socket.getsockname()[1]
        self._running = True

        engine_status = "C engine" if USE_C_ENGINE else "Python fallback"
        log.info("Coup server listening on %s:%d (%s)", self.host, self.port, engine_status)

        # Start WebSocket listener if configured
        self._start_ws_server()

        # Start admin portal if configured
        self._start_admin_server()

        try:
            while self._running:
                read_list = list(self.clients.keys()) + list(self.pending_auth.keys())
                if self.server_socket and not self.port_locked:
                    read_list.append(self.server_socket)
                try:
                    readable, _, _ = select.select(read_list, [], [], 0.5)
                except (ValueError, OSError):
                    continue

                for sock in readable:
                    if sock is self.server_socket:
                        self.handle_new_connection(sock)
                    elif sock in self.pending_auth:
                        self.handle_pending_auth(sock)
                    else:
                        self.handle_client_data(sock)

                self.check_auth_timeouts()
                self.check_timeouts()
                self.tick_bots()
                self.check_heartbeats()
                self.check_lobby_inactivity()
                self._drain_admin_commands()

        except KeyboardInterrupt:
            log.info("Shutting down (keyboard interrupt)")
        finally:
            self.shutdown()

    def shutdown(self):
        self._running = False
        self._stop_ws_server()
        self._stop_admin_server()
        for sock in list(self.pending_auth.keys()):
            self._silent_close_pending(sock)
        for sock in list(self.clients.keys()):
            self.disconnect_client(sock)
        if self.server_socket:
            try:
                self.server_socket.close()
            except OSError:
                pass
        log.info("Server shut down.")


def main():
    parser = argparse.ArgumentParser(description="Coup Card Game Server (Saturn NetLink Edition)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=4821, help="Listen port (default: 4821)")
    parser.add_argument("--ws-port", type=int, default=0,
                        help="WebSocket listen port (default: disabled)")
    parser.add_argument("--admin-port", type=int, default=0,
                        help="Admin portal HTTP port (default: disabled)")
    parser.add_argument("--admin-user", default="admin",
                        help="Admin portal username (default: admin)")
    parser.add_argument("--admin-password", default="coup2025",
                        help="Admin portal password (default: coup2025)")
    args = parser.parse_args()

    if args.ws_port and not HAS_WEBSOCKETS:
        log.error("--ws-port requires the 'websockets' package: pip install websockets")
        return

    server = CoupServer(host=args.host, port=args.port, ws_port=args.ws_port,
                        admin_port=args.admin_port, admin_user=args.admin_user,
                        admin_password=args.admin_password)
    server.run()


if __name__ == "__main__":
    main()
