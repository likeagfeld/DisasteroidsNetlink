#!/usr/bin/env python3
"""
Fake Saturn Test Client

Connects to the Disasteroids server (through bridge auth) and simulates
a second player with simple AI: flies around, shoots, and dodges.

Usage:
    python tools/test_client.py --host saturncoup.duckdns.org
    python tools/test_client.py --host saturncoup.duckdns.org --name CPUBOT
"""

import argparse
import math
import random
import socket
import struct
import time

# Bridge auth
SHARED_SECRET = b"SaturnCoup2025!NetLink#SecretKey"
AUTH_MAGIC = b"AUTH"
AUTH_OK = 0x01

# SNCP messages
MSG_CONNECT = 0x01
MSG_SET_USERNAME = 0x02
MSG_HEARTBEAT = 0x04
MSG_DISCONNECT = 0x05

MSG_USERNAME_REQUIRED = 0x81
MSG_WELCOME = 0x82
MSG_WELCOME_BACK = 0x83

DNET_MSG_READY = 0x10
DNET_MSG_INPUT_STATE = 0x11
DNET_MSG_START_GAME_REQ = 0x12

DNET_MSG_LOBBY_STATE = 0xA0
DNET_MSG_GAME_START = 0xA1
DNET_MSG_INPUT_RELAY = 0xA2
DNET_MSG_PLAYER_JOIN = 0xA3
DNET_MSG_PLAYER_LEAVE = 0xA4
DNET_MSG_GAME_OVER = 0xA5
DNET_MSG_LOG = 0xA6
DNET_MSG_PAUSE_ACK = 0xA7

UUID_LEN = 36

# Input bitmask (matches disasteroids_protocol.h)
INPUT_UP    = 1 << 0
INPUT_DOWN  = 1 << 1
INPUT_LEFT  = 1 << 2
INPUT_RIGHT = 1 << 3
INPUT_A     = 1 << 4  # shoot
INPUT_B     = 1 << 5  # thrust
INPUT_C     = 1 << 6  # shoot
INPUT_X     = 1 << 7  # change color


def encode_frame(payload: bytes) -> bytes:
    return struct.pack("!H", len(payload)) + payload


def read_lp_string(data, off):
    if off >= len(data):
        return "", off
    slen = data[off]
    off += 1
    s = data[off:off + slen].decode("utf-8", errors="replace")
    return s, off + slen


class BotAI:
    """Simple bot AI that cycles through behaviors."""

    def __init__(self):
        self.frame = 0
        self.state = "cruise"
        self.state_timer = 0
        self.turn_dir = 0       # -1=left, 0=none, 1=right
        self.thrusting = False
        self.shooting = False
        self._pick_new_state()

    def _pick_new_state(self):
        """Choose a random behavior — heavily weighted toward aggression."""
        self.state = random.choice([
            "attack",       # thrust + rapid fire + weave
            "attack",       # (weighted x3)
            "attack",
            "hunt_left",    # turn left while shooting
            "hunt_right",   # turn right while shooting
            "cruise",       # fly forward, shoot ahead
            "strafe_attack",# strafe while firing
            "evade",        # quick direction changes + shooting
        ])
        # Each state lasts 1-3 seconds (60-180 frames)
        self.state_timer = random.randint(60, 180)

    def tick(self):
        """Returns input bits for this frame."""
        self.frame += 1
        self.state_timer -= 1

        if self.state_timer <= 0:
            self._pick_new_state()

        bits = 0

        if self.state == "attack":
            bits |= INPUT_UP
            # rapid fire — shoot every other frame
            if self.frame % 6 < 3:
                bits |= INPUT_A
            # weave while attacking
            if self.frame % 40 < 20:
                bits |= INPUT_LEFT
            else:
                bits |= INPUT_RIGHT

        elif self.state == "hunt_left":
            bits |= INPUT_LEFT | INPUT_UP
            # constant shooting while hunting
            if self.frame % 5 < 3:
                bits |= INPUT_A

        elif self.state == "hunt_right":
            bits |= INPUT_RIGHT | INPUT_UP
            # constant shooting while hunting
            if self.frame % 5 < 3:
                bits |= INPUT_C

        elif self.state == "cruise":
            bits |= INPUT_UP
            # shoot ahead while cruising
            if self.frame % 10 < 4:
                bits |= INPUT_A
            # slight course corrections
            if self.frame % 80 < 15:
                bits |= INPUT_LEFT

        elif self.state == "strafe_attack":
            # turn, thrust, shoot — aggressive strafing
            cycle = self.frame % 40
            if cycle < 15:
                bits |= INPUT_LEFT | INPUT_A
            elif cycle < 30:
                bits |= INPUT_UP | INPUT_A
            else:
                bits |= INPUT_RIGHT | INPUT_C

        elif self.state == "evade":
            bits |= INPUT_UP
            # rapid direction changes
            cycle = self.frame % 20
            if cycle < 7:
                bits |= INPUT_LEFT
            elif cycle < 14:
                bits |= INPUT_RIGHT
            # shoot while evading
            if self.frame % 8 < 4:
                bits |= INPUT_C

        return bits


class FakeSaturn:
    def __init__(self, host, port, username, auto_ready=True):
        self.host = host
        self.port = port
        self.username = username
        self.auto_ready = auto_ready
        self.sock = None
        self.recv_buf = b""
        self.in_game = False
        self.in_lobby = False
        self.my_player_id = 0
        self.frame_num = 0
        self.running = False
        self.ready = False
        self.ai = BotAI()
        self.last_sent_bits = -1   # Force first send
        self.force_send_counter = 0

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect((self.host, self.port))
        print("[*] Connected to %s:%d" % (self.host, self.port))

        auth_payload = AUTH_MAGIC + bytes([len(SHARED_SECRET)]) + SHARED_SECRET
        self.sock.sendall(auth_payload)
        print("[*] Sent bridge AUTH")

        resp = self.sock.recv(1)
        if not resp or resp[0] != AUTH_OK:
            print("[!] Auth rejected")
            return False
        print("[+] Bridge authenticated")

        self.sock.setblocking(False)

        self.sock.sendall(encode_frame(bytes([MSG_CONNECT])))
        print("[*] Sent CONNECT")
        return True

    def send_username(self):
        name_bytes = self.username.encode("utf-8")[:16]
        payload = bytes([MSG_SET_USERNAME, len(name_bytes)]) + name_bytes
        self.sock.sendall(encode_frame(payload))
        print("[*] Sent SET_USERNAME: %s" % self.username)

    def send_ready(self):
        self.sock.sendall(encode_frame(bytes([DNET_MSG_READY])))
        self.ready = not self.ready
        print("[*] Toggled READY -> %s" % ("READY" if self.ready else "NOT READY"))

    def send_heartbeat(self):
        self.sock.sendall(encode_frame(bytes([MSG_HEARTBEAT])))

    def send_input(self, bits=0):
        payload = bytes([DNET_MSG_INPUT_STATE])
        payload += struct.pack("!HH", self.frame_num & 0xFFFF, bits & 0xFFFF)
        self.sock.sendall(encode_frame(payload))
        self.frame_num += 1

    def send_disconnect(self):
        self.sock.sendall(encode_frame(bytes([MSG_DISCONNECT])))
        print("[*] Sent DISCONNECT")

    def process_messages(self):
        try:
            data = self.sock.recv(4096)
        except BlockingIOError:
            return
        except OSError:
            print("[!] Connection lost")
            self.running = False
            return

        if not data:
            print("[!] Server closed connection")
            self.running = False
            return

        self.recv_buf += data

        while len(self.recv_buf) >= 2:
            payload_len = (self.recv_buf[0] << 8) | self.recv_buf[1]
            total = 2 + payload_len
            if payload_len == 0 or payload_len > 8192:
                self.recv_buf = b""
                return
            if len(self.recv_buf) < total:
                break

            payload = self.recv_buf[2:total]
            self.recv_buf = self.recv_buf[total:]
            self.handle_message(payload)

    def handle_message(self, payload):
        if not payload:
            return
        msg_type = payload[0]

        if msg_type == MSG_USERNAME_REQUIRED:
            print("[<] USERNAME_REQUIRED")
            self.send_username()

        elif msg_type in (MSG_WELCOME, MSG_WELCOME_BACK):
            uid = payload[1] if len(payload) > 1 else 0
            off = 2 + UUID_LEN
            uname, _ = read_lp_string(payload, off)
            print("[<] WELCOME  id=%d  name=%s" % (uid, uname))
            self.in_lobby = True
            if self.auto_ready and not self.ready:
                time.sleep(0.5)
                self.send_ready()

        elif msg_type == DNET_MSG_LOBBY_STATE:
            count = payload[1] if len(payload) > 1 else 0
            off = 2
            players = []
            for _ in range(count):
                if off >= len(payload):
                    break
                pid = payload[off]; off += 1
                pname, off = read_lp_string(payload, off)
                pready = payload[off] if off < len(payload) else 0; off += 1
                players.append((pid, pname, "READY" if pready else "---"))
            print("[<] LOBBY (%d players):" % count)
            for pid, pname, pready in players:
                print("      [%d] %-16s %s" % (pid, pname, pready))

        elif msg_type == DNET_MSG_GAME_START:
            if len(payload) >= 8:
                seed = struct.unpack("!I", payload[1:5])[0]
                self.my_player_id = payload[5]
                opp_count = payload[6]
                self.in_game = True
                self.frame_num = 0
                self.ai = BotAI()  # fresh AI for new game
                self.last_sent_bits = -1  # force first send
                self.force_send_counter = 0
                print("[<] GAME_START  seed=%08X  player_id=%d  opponents=%d" %
                      (seed, self.my_player_id, opp_count))
                print("[*] Bot AI active!")

        elif msg_type == DNET_MSG_INPUT_RELAY:
            pass

        elif msg_type == DNET_MSG_GAME_OVER:
            winner = payload[1] if len(payload) > 1 else 0
            print("[<] GAME_OVER  winner=%d" % winner)
            self.in_game = False

        elif msg_type == DNET_MSG_LOG:
            if len(payload) >= 2:
                text = payload[2:2 + payload[1]].decode("utf-8", errors="replace")
                print("[<] LOG: %s" % text)

        elif msg_type == DNET_MSG_PLAYER_JOIN:
            pid = payload[1] if len(payload) > 1 else 0
            pname, _ = read_lp_string(payload, 2)
            print("[<] PLAYER_JOIN  id=%d  name=%s" % (pid, pname))

        elif msg_type == DNET_MSG_PLAYER_LEAVE:
            pid = payload[1] if len(payload) > 1 else 0
            print("[<] PLAYER_LEAVE  id=%d" % pid)

        else:
            print("[<] msg 0x%02X  (%d bytes)" % (msg_type, len(payload)))

    def run(self):
        if not self.connect():
            return

        self.running = True
        print("[*] Running with bot AI. Ctrl+C to quit.")

        heartbeat_counter = 0
        try:
            while self.running:
                self.process_messages()

                if self.in_game:
                    bits = self.ai.tick()
                    # Delta compression: only send when input changes or every 15 frames
                    self.force_send_counter += 1
                    if bits != self.last_sent_bits or self.force_send_counter >= 15:
                        try:
                            self.send_input(bits)
                        except OSError:
                            break
                        self.last_sent_bits = bits
                        self.force_send_counter = 0
                    time.sleep(1.0 / 60.0)
                else:
                    time.sleep(0.05)

                heartbeat_counter += 1
                threshold = 600 if self.in_game else 120
                if heartbeat_counter >= threshold:
                    heartbeat_counter = 0
                    try:
                        self.send_heartbeat()
                    except OSError:
                        break

        except KeyboardInterrupt:
            print("\n[*] Interrupted")
        finally:
            self.running = False
            try:
                self.send_disconnect()
            except OSError:
                pass
            if self.sock:
                self.sock.close()
            print("[*] Disconnected")


def main():
    parser = argparse.ArgumentParser(description="Fake Saturn Bot Client")
    parser.add_argument("--host", default="localhost", help="Server host")
    parser.add_argument("--port", type=int, default=4821, help="Server port")
    parser.add_argument("--name", default="CPUBOT", help="Player name")
    parser.add_argument("--no-ready", action="store_true", help="Don't auto-ready")
    args = parser.parse_args()

    client = FakeSaturn(args.host, args.port, args.name, auto_ready=not args.no_ready)
    client.run()


if __name__ == "__main__":
    main()
