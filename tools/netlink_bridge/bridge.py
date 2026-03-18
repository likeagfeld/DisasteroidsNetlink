#!/usr/bin/env python3
"""
Saturn Coup NetLink Modem-to-TCP Bridge

Answers incoming modem calls from the Saturn NetLink and relays data
bidirectionally to/from Coup Server.  The bridge is transparent —
it does not parse or modify the data stream.

Architecture:
    Saturn NetLink ──phone cable──> USB modem ──serial──> Bridge ──TCP──> Coup Server

Usage:
    python3 tools/netlink_bridge/bridge.py                              # auto-detect serial
    python3 tools/netlink_bridge/bridge.py --serial-port /dev/cu.usbmodem1101
    python3 tools/netlink_bridge/bridge.py --mock                       # test without hardware
    python3 tools/netlink_bridge/bridge.py --mock --server localhost:4821
"""

import argparse
import logging
import select
import socket
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("netlink_bridge")

DEFAULT_BAUD = 9600
DEFAULT_SERVER = "localhost:4821"
DEFAULT_MOCK_PORT = 2337

# Shared secret authentication
SHARED_SECRET = b"SaturnCoup2025!NetLink#SecretKey"
AUTH_MAGIC = b"AUTH"
AUTH_OK = 0x01
AUTH_TIMEOUT = 5.0  # seconds to wait for server response

# How many bytes to check at the tail of serial input for NO CARRIER
NO_CARRIER_WINDOW = 32


# ---------------------------------------------------------------------------
# SNCP frame decoder for verbose logging
# ---------------------------------------------------------------------------

# Message type names
_CLIENT_MSG_NAMES = {
    0x01: "CONNECT",
    0x02: "SET_USERNAME",
    0x03: "MESSAGE",
    0x04: "HEARTBEAT",
    0x05: "DISCONNECT",
}

_SERVER_MSG_NAMES = {
    0x81: "USERNAME_REQUIRED",
    0x82: "WELCOME",
    0x83: "WELCOME_BACK",
    0x84: "USERNAME_TAKEN",
    0x85: "USER_LIST",
    0x86: "USER_JOIN",
    0x87: "USER_LEAVE",
    0x88: "MESSAGE",
    0x89: "HISTORY",
}

_UUID_LEN = 36


def _read_lp_string(data: bytes, off: int) -> tuple:
    """Read a length-prefixed string. Returns (string, new_offset) or (None, off) on error."""
    if off >= len(data):
        return None, off
    slen = data[off]
    off += 1
    if off + slen > len(data):
        return None, off
    s = data[off:off + slen].decode("utf-8", errors="replace")
    return s, off + slen


def _decode_client_frame(payload: bytes) -> str:
    """Decode a client->server SNCP payload into a human-readable string."""
    if not payload:
        return "EMPTY"
    msg_type = payload[0]
    name = _CLIENT_MSG_NAMES.get(msg_type, "0x%02X" % msg_type)

    if msg_type == 0x01:  # CONNECT
        if len(payload) >= 1 + _UUID_LEN:
            uuid = payload[1:1 + _UUID_LEN].decode("ascii", errors="replace").rstrip("\x00")
            return "%s uuid=%s" % (name, uuid)
        return "%s (new)" % name

    if msg_type == 0x02:  # SET_USERNAME
        s, _ = _read_lp_string(payload, 1)
        if s is not None:
            return '%s name="%s"' % (name, s)

    if msg_type == 0x03:  # MESSAGE
        s, _ = _read_lp_string(payload, 1)
        if s is not None:
            return '%s text="%s"' % (name, s)

    return name


def _decode_server_frame(payload: bytes) -> str:
    """Decode a server->client SNCP payload into a human-readable string."""
    if not payload:
        return "EMPTY"
    msg_type = payload[0]
    name = _SERVER_MSG_NAMES.get(msg_type, "0x%02X" % msg_type)

    if msg_type in (0x82, 0x83):  # WELCOME / WELCOME_BACK
        off = 1
        if off + 2 > len(payload):
            return name
        uid = (payload[off] << 8) | payload[off + 1]
        off += 2
        if off + _UUID_LEN > len(payload):
            return "%s id=%d" % (name, uid)
        uuid = payload[off:off + _UUID_LEN].decode("ascii", errors="replace").rstrip("\x00")
        off += _UUID_LEN
        uname, _ = _read_lp_string(payload, off)
        return '%s id=%d uuid=%s..%s name="%s"' % (
            name, uid, uuid[:4], uuid[-4:], uname or "?")

    if msg_type == 0x85:  # USER_LIST
        if len(payload) < 2:
            return name
        count = payload[1]
        users = []
        off = 2
        for _ in range(count):
            if off + 2 > len(payload):
                break
            uid = (payload[off] << 8) | payload[off + 1]
            off += 2
            uname, off = _read_lp_string(payload, off)
            if uname is not None:
                users.append("%d:%s" % (uid, uname))
        return "%s [%s]" % (name, ", ".join(users))

    if msg_type in (0x86, 0x87):  # USER_JOIN / USER_LEAVE
        off = 1
        if off + 2 > len(payload):
            return name
        uid = (payload[off] << 8) | payload[off + 1]
        off += 2
        uname, _ = _read_lp_string(payload, off)
        return '%s id=%d name="%s"' % (name, uid, uname or "?")

    if msg_type == 0x88:  # MESSAGE
        off = 1
        if off + 2 + 5 > len(payload):
            return name
        uid = (payload[off] << 8) | payload[off + 1]
        off += 2
        ts = payload[off:off + 5].decode("ascii", errors="replace")
        off += 5
        uname, off = _read_lp_string(payload, off)
        text, _ = _read_lp_string(payload, off)
        return '%s [%s] %s: "%s"' % (name, ts, uname or "?", text or "")

    if msg_type == 0x89:  # HISTORY
        if len(payload) < 2:
            return name
        count = payload[1]
        return "%s (%d msgs)" % (name, count)

    return name


class SncpLogger:
    """
    Accumulates bytes from a stream direction and logs decoded SNCP frames.
    Does not modify the data — purely observational.
    """

    def __init__(self, direction: str, is_client: bool):
        self.direction = direction
        self.is_client = is_client
        self._buf = b""

    def feed(self, data: bytes) -> None:
        self._buf += data
        while len(self._buf) >= 2:
            payload_len = (self._buf[0] << 8) | self._buf[1]
            total = 2 + payload_len
            if len(self._buf) < total:
                break
            payload = self._buf[2:total]
            self._buf = self._buf[total:]
            if payload_len == 0:
                continue
            if self.is_client:
                desc = _decode_client_frame(payload)
            else:
                desc = _decode_server_frame(payload)
            log.debug("%s %s (%d bytes)", self.direction, desc, total)

    def reset(self) -> None:
        self._buf = b""


# ---------------------------------------------------------------------------
# Modem handler interface + implementations
# ---------------------------------------------------------------------------

class ModemHandler:
    """Real serial modem via pyserial."""

    def __init__(self, port: str, baud: int = DEFAULT_BAUD,
                 manual_answer: bool = False, direct: bool = False):
        self.port = port
        self.baud = baud
        self.manual_answer = manual_answer
        self.direct = direct
        self._serial = None
        self._leftover = b""  # Data read during CONNECT that belongs to relay

    def open(self) -> None:
        import serial
        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0,
            rtscts=False,
            xonxoff=False,
        )
        log.info("Opened serial port %s at %d baud", self.port, self.baud)

    def close(self) -> None:
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    def read(self, size: int = 4096) -> bytes:
        if self._leftover:
            data = self._leftover
            self._leftover = b""
            return data
        n = self._serial.in_waiting
        if n:
            return self._serial.read(min(n, size))
        return b""

    def write(self, data: bytes) -> None:
        self._serial.write(data)
        self._serial.flush()

    def hangup(self) -> None:
        """Hang up the modem by escaping to command mode and sending ATH."""
        log.info("Hanging up modem...")
        # Guard time silence (S12 register, typically 1s)
        time.sleep(1.1)
        self._serial.write(b"+++")
        self._serial.flush()
        time.sleep(1.1)
        # Drain OK response from +++
        self._serial.reset_input_buffer()
        self._serial.write(b"ATH\r")
        self._serial.flush()
        time.sleep(0.5)
        self._serial.reset_input_buffer()
        log.info("Modem hung up")

    def _send_at(self, cmd: str, timeout: float = 3.0) -> str:
        """Send an AT command and return the response text."""
        self._serial.reset_input_buffer()
        self._serial.write((cmd + "\r").encode("ascii"))
        self._serial.flush()

        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            n = self._serial.in_waiting
            if n:
                chunk = self._serial.read(n)
                if chunk:
                    buf += chunk
                    text = buf.decode("ascii", errors="replace")
                    for sentinel in ("OK", "ERROR", "CONNECT", "NO CARRIER",
                                     "NO DIALTONE", "BUSY", "NO ANSWER"):
                        if sentinel in text:
                            return text.strip()
            else:
                time.sleep(0.05)
        return buf.decode("ascii", errors="replace").strip()

    def drain_serial(self) -> None:
        """Discard all pending data in the serial input buffer."""
        if self._serial:
            try:
                self._serial.reset_input_buffer()
            except Exception:
                pass

    def init_modem(self) -> None:
        """Send initialization AT commands."""
        log.info("Initializing modem...")

        # Drain any stale data before sending commands
        self.drain_serial()

        resp = self._send_at("ATZ", timeout=5.0)
        log.info("ATZ -> %s", resp.replace("\r\n", " ").strip())
        if "OK" not in resp:
            raise RuntimeError("ATZ failed: %r" % resp)

        resp = self._send_at("ATE0")
        log.info("ATE0 -> %s", resp.replace("\r\n", " ").strip())

        resp = self._send_at("ATV1")
        log.info("ATV1 -> %s", resp.replace("\r\n", " ").strip())

        if self.direct:
            log.info("Direct connect mode — will send ATA to answer")
        elif self.manual_answer:
            log.info("Manual answer mode — will send ATA on RING")
        else:
            resp = self._send_at("ATS0=1")
            log.info("ATS0=1 -> %s", resp.replace("\r\n", " ").strip())
            if "OK" not in resp:
                raise RuntimeError("ATS0=1 failed: %r" % resp)

        log.info("Modem initialized, waiting for call...")

    def check_alive(self) -> bool:
        """Send AT to verify the modem is still responsive."""
        try:
            resp = self._send_at("AT", timeout=3.0)
            return "OK" in resp
        except Exception:
            return False

    def wait_for_connect(self) -> bool:
        """
        Block until CONNECT is received from the modem.

        Modes:
        - direct: Send ATA to go off-hook in answer mode. Retry on NO CARRIER
          (S7 timeout). This works without a ring signal — the modem listens
          for the originating carrier from the Saturn.
        - manual_answer: Wait for RING, then send ATA.
        - default (ATS0=1): Wait for auto-answer on RING.

        Returns True on CONNECT, False on hard failure.
        """
        if self.direct:
            return self._wait_direct()

        buf = b""
        last_keepalive = time.time()
        while True:
            n = self._serial.in_waiting
            if n:
                chunk = self._serial.read(n)
                if not chunk:
                    return False
                buf += chunk
                text = buf.decode("ascii", errors="replace")

                if "RING" in text:
                    if self.manual_answer:
                        log.info("RING detected, answering with ATA...")
                        self._serial.write(b"ATA\r")
                        self._serial.flush()
                        buf = b""
                    else:
                        log.info("RING detected, auto-answering...")

                if "CONNECT" in text:
                    time.sleep(0.1)
                    # Drain remaining response bytes
                    while True:
                        time.sleep(0.05)
                        n2 = self._serial.in_waiting
                        if not n2:
                            break
                        extra = self._serial.read(n2)
                        if not extra:
                            break
                        buf += extra
                    text = buf.decode("ascii", errors="replace")
                    nl = text.find("\n", text.find("CONNECT"))
                    if nl >= 0 and nl + 1 < len(buf):
                        self._leftover = buf[nl + 1:]
                    log.info("CONNECT: %s", text.strip().replace("\r\n", " "))
                    return True

                if "NO CARRIER" in text or "ERROR" in text:
                    log.warning("Modem error during wait: %s", text.strip())
                    return False

                # Keep buffer from growing indefinitely
                if len(buf) > 4096:
                    buf = buf[-256:]
            else:
                time.sleep(0.1)

                # Periodic keepalive: every 5 minutes, send AT to keep USB
                # serial port alive and verify modem is still responsive.
                # This prevents USB auto-suspend from killing the port.
                now = time.time()
                if now - last_keepalive >= 300.0:
                    last_keepalive = now
                    log.debug("Keepalive: checking modem with AT...")
                    if not self.check_alive():
                        log.warning("Modem not responding to keepalive AT!")
                        return False
                    # Re-drain and reset buffer after AT/OK exchange
                    buf = b""

    def _wait_direct(self) -> bool:
        """
        Direct connect: send ATA to go off-hook and wait for carrier.

        Without a phone exchange or line voltage inducer, there is no ring
        signal. ATA puts the modem in answer mode immediately — it goes
        off-hook and sends answer-mode carrier tones. When the Saturn dials,
        its originate carrier is detected and both sides get CONNECT.

        If the Saturn hasn't dialed yet, the modem's S7 register times out
        (~50s) and sends NO CARRIER. We retry ATA until a connection is made.
        """
        while True:
            self._serial.reset_input_buffer()
            log.info("Sending ATA (direct answer mode)...")
            self._serial.write(b"ATA\r")
            self._serial.flush()

            buf = b""
            while True:
                n = self._serial.in_waiting
                if n:
                    chunk = self._serial.read(n)
                    if not chunk:
                        return False
                    buf += chunk
                    text = buf.decode("ascii", errors="replace")

                    if "CONNECT" in text:
                        time.sleep(0.1)
                        # Drain remaining response bytes
                        while True:
                            time.sleep(0.05)
                            n2 = self._serial.in_waiting
                            if not n2:
                                break
                            extra = self._serial.read(n2)
                            if not extra:
                                break
                            buf += extra
                        text = buf.decode("ascii", errors="replace")
                        nl = text.find("\n", text.find("CONNECT"))
                        if nl >= 0 and nl + 1 < len(buf):
                            self._leftover = buf[nl + 1:]
                        log.info("CONNECT: %s",
                                 text.strip().replace("\r\n", " "))
                        return True

                    if "NO CARRIER" in text:
                        log.info("ATA timed out (no carrier yet), retrying...")
                        break  # retry ATA

                    if "ERROR" in text:
                        log.warning("ATA error: %s", text.strip())
                        return False
                else:
                    time.sleep(0.1)


class MockModemHandler:
    """
    Simulates a modem by accepting a TCP connection.
    Used for testing the bridge without hardware.
    """

    def __init__(self, port: int = DEFAULT_MOCK_PORT):
        self.listen_port = port
        self._listen_sock = None
        self._client_sock = None

    def open(self) -> None:
        self._listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen_sock.bind(("127.0.0.1", self.listen_port))
        self._listen_sock.listen(1)
        # Record actual port (useful when port=0)
        self.listen_port = self._listen_sock.getsockname()[1]
        log.info("Mock modem listening on 127.0.0.1:%d", self.listen_port)

    def close(self) -> None:
        if self._client_sock:
            try:
                self._client_sock.close()
            except OSError:
                pass
            self._client_sock = None
        if self._listen_sock:
            try:
                self._listen_sock.close()
            except OSError:
                pass
            self._listen_sock = None

    def read(self, size: int = 4096) -> bytes:
        try:
            return self._client_sock.recv(size)
        except BlockingIOError:
            return b""

    def write(self, data: bytes) -> None:
        self._client_sock.sendall(data)

    def hangup(self) -> None:
        """Close mock client connection (simulates hangup)."""
        if self._client_sock:
            try:
                self._client_sock.close()
            except OSError:
                pass
            self._client_sock = None
        log.info("Mock modem: hung up")

    def init_modem(self) -> None:
        log.info("Mock modem ready (no AT init needed)")

    def wait_for_connect(self) -> bool:
        """Accept a TCP connection (simulates CONNECT)."""
        log.info("Mock modem: waiting for TCP connection on port %d...",
                 self.listen_port)
        try:
            ready, _, _ = select.select([self._listen_sock], [], [], None)
            if ready:
                self._client_sock, addr = self._listen_sock.accept()
                self._client_sock.setblocking(False)
                log.info("Mock modem: CONNECT from %s:%d", addr[0], addr[1])
                return True
        except OSError:
            pass
        return False


# ---------------------------------------------------------------------------
# Bridge orchestrator
# ---------------------------------------------------------------------------

class NetlinkBridge:
    """
    Orchestrates the modem-to-TCP relay.

    Lifecycle: init modem -> wait for call -> connect to server -> relay -> cleanup -> loop
    """

    def __init__(self, modem, server_host: str, server_port: int,
                 verbose: bool = False):
        self.modem = modem
        self.server_host = server_host
        self.server_port = server_port
        self.verbose = verbose
        self._server_sock = None
        self._running = False
        # Tail buffer for NO CARRIER detection on serial data
        self._modem_tail = b""
        # SNCP frame decoders for verbose logging
        self._modem_logger = SncpLogger("MODEM->SERVER", is_client=True)
        self._server_logger = SncpLogger("SERVER->MODEM", is_client=False)

    def connect_to_server(self) -> bool:
        """Open TCP connection to the chat server and authenticate."""
        try:
            self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_sock.connect((self.server_host, self.server_port))
            log.info("Connected to server %s:%d — authenticating...",
                     self.server_host, self.server_port)

            # Send auth handshake: AUTH + 1-byte length + secret
            auth_payload = AUTH_MAGIC + bytes([len(SHARED_SECRET)]) + SHARED_SECRET
            self._server_sock.sendall(auth_payload)

            # Wait for 1-byte response (blocking, with timeout)
            self._server_sock.settimeout(AUTH_TIMEOUT)
            try:
                resp = self._server_sock.recv(1)
            except socket.timeout:
                log.error("Auth timeout — server did not respond")
                self._server_sock.close()
                self._server_sock = None
                return False

            if not resp or resp[0] != AUTH_OK:
                log.error("Auth rejected by server")
                self._server_sock.close()
                self._server_sock = None
                return False

            log.info("Authenticated with server")
            self._server_sock.setblocking(False)
            return True
        except OSError as e:
            log.error("Failed to connect to server %s:%d: %s",
                      self.server_host, self.server_port, e)
            if self._server_sock:
                self._server_sock.close()
                self._server_sock = None
            return False

    def _disconnect_server(self) -> None:
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None

    @staticmethod
    def check_no_carrier(tail: bytes) -> bool:
        """
        Check if the modem tail buffer contains NO CARRIER.
        Handles the pattern split across multiple reads.
        """
        text = tail.decode("ascii", errors="replace")
        return "NO CARRIER" in text

    def relay_loop(self) -> str:
        """
        Bidirectional byte relay between modem and server.
        Returns a reason string: "no_carrier", "server_closed", "error".

        Uses non-blocking reads on the modem (cross-platform) and select()
        only on the server socket (works on all platforms including Windows).
        """
        self._modem_tail = b""
        server_fd = self._server_sock.fileno()
        idle_seconds = 0.0

        while True:
            had_data = False

            # --- Modem -> Server (non-blocking poll) ---
            try:
                data = self.modem.read(4096)
            except OSError:
                return "no_carrier"

            if data:
                had_data = True
                log.debug("MODEM raw %d bytes: %s", len(data), data.hex())

                if self.verbose:
                    self._modem_logger.feed(data)

                self._modem_tail = (self._modem_tail + data)[-NO_CARRIER_WINDOW:]
                if self.check_no_carrier(self._modem_tail):
                    log.info("NO CARRIER detected")
                    return "no_carrier"

                try:
                    self._server_sock.sendall(data)
                except OSError:
                    return "server_closed"

            # --- Server -> Modem (select on socket is cross-platform) ---
            try:
                readable, _, _ = select.select([server_fd], [], [], 0)
            except (ValueError, OSError):
                return "error"

            if readable:
                had_data = True
                try:
                    data = self._server_sock.recv(4096)
                except OSError:
                    return "server_closed"
                if not data:
                    return "server_closed"

                log.debug("SERVER raw %d bytes: %s", len(data), data.hex())

                if self.verbose:
                    self._server_logger.feed(data)

                try:
                    self.modem.write(data)
                except OSError:
                    return "no_carrier"

            if not had_data:
                time.sleep(0.01)
                idle_seconds += 0.01
                if idle_seconds >= 10.0:
                    log.debug("Relay idle %.0fs, no data from either side",
                              idle_seconds)
                    idle_seconds = 0.0
            else:
                idle_seconds = 0.0

    def run(self) -> None:
        """Main loop: init modem, wait for calls, relay, reconnect."""
        self._running = True
        try:
            self.modem.open()
            self.modem.init_modem()

            while self._running:
                log.info("<listening>")
                if not self.modem.wait_for_connect():
                    log.warning("wait_for_connect failed, re-initializing modem...")
                    time.sleep(2)
                    # Re-init modem: resets all registers, clears stale state,
                    # and re-enables auto-answer. This fixes the bug where the
                    # modem stops responding to RING after long idle periods.
                    try:
                        self.modem.init_modem()
                    except Exception as e:
                        log.error("Modem re-init failed: %s — reopening port", e)
                        self.modem.close()
                        time.sleep(2)
                        self.modem.open()
                        self.modem.init_modem()
                    continue

                if not self.connect_to_server():
                    log.error("Cannot reach server, dropping modem connection")
                    self.modem.hangup()
                    time.sleep(2)
                    self.modem.init_modem()
                    continue

                self._modem_logger.reset()
                self._server_logger.reset()
                log.info("Relay active")
                reason = self.relay_loop()
                log.info("Relay ended: %s", reason)

                self._disconnect_server()
                self._modem_tail = b""

                if reason == "no_carrier":
                    log.info("Saturn disconnected, re-initializing modem...")
                elif reason == "server_closed":
                    log.warning("Server closed connection, hanging up modem")
                    self.modem.hangup()
                else:
                    log.warning("Relay error, retrying...")
                    time.sleep(1)

                # Always re-initialize the modem between sessions.
                # This restores S-registers (ATS0=1 for auto-answer),
                # drains stale serial data, and keeps the USB port awake.
                try:
                    self.modem.init_modem()
                except Exception as e:
                    log.error("Modem re-init failed: %s — reopening port", e)
                    self.modem.close()
                    time.sleep(2)
                    self.modem.open()
                    self.modem.init_modem()

        except KeyboardInterrupt:
            log.info("Shutting down (keyboard interrupt)")
        finally:
            self._running = False
            self._disconnect_server()
            self.modem.close()
            log.info("Bridge shut down.")

    def stop(self) -> None:
        """Signal the bridge to stop."""
        self._running = False


# ---------------------------------------------------------------------------
# Serial port discovery
# ---------------------------------------------------------------------------

def list_serial_ports() -> list:
    """List available serial ports."""
    try:
        from serial.tools.list_ports import comports
        return list(comports())
    except ImportError:
        log.error("pyserial not installed. Run: pip install pyserial")
        return []


def auto_detect_modem() -> str:
    """Try to find a USB modem serial port."""
    ports = list_serial_ports()
    for p in ports:
        desc = (p.description or "").lower()
        if any(kw in desc for kw in ("modem", "usb", "serial", "uart")):
            return p.device
    # Fallback: return first port if any
    if ports:
        return ports[0].device
    return ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_server_arg(s: str) -> tuple:
    """Parse 'host:port' string."""
    if ":" in s:
        host, port_str = s.rsplit(":", 1)
        return host, int(port_str)
    return s, 4821


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Saturn NetLink Modem-to-TCP Bridge"
    )
    parser.add_argument(
        "--serial-port",
        help="Serial port for USB modem (auto-detects if omitted)",
    )
    parser.add_argument(
        "--baud", type=int, default=DEFAULT_BAUD,
        help="Serial baud rate (default: %d)" % DEFAULT_BAUD,
    )
    parser.add_argument(
        "--server", default=DEFAULT_SERVER,
        help="Chat server host:port (default: %s)" % DEFAULT_SERVER,
    )
    parser.add_argument(
        "--mock", nargs="?", const=DEFAULT_MOCK_PORT, type=int, metavar="PORT",
        help="Mock mode: accept TCP instead of serial (default port: %d)" % DEFAULT_MOCK_PORT,
    )
    parser.add_argument(
        "--direct", action="store_true",
        help="Direct connect: send ATA without waiting for RING "
             "(for cable connections without a line voltage inducer)",
    )
    parser.add_argument(
        "--manual-answer", action="store_true",
        help="Use RING+ATA instead of ATS0=1 auto-answer",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Log decoded SNCP frames for all relayed data",
    )
    parser.add_argument(
        "--list-ports", action="store_true",
        help="List available serial ports and exit",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.list_ports:
        ports = list_serial_ports()
        if not ports:
            print("No serial ports found.")
        else:
            print("Available serial ports:")
            for p in ports:
                print("  %s  —  %s" % (p.device, p.description))
        sys.exit(0)

    server_host, server_port = parse_server_arg(args.server)

    if args.mock is not None:
        modem = MockModemHandler(port=args.mock)
    else:
        port = args.serial_port or auto_detect_modem()
        if not port:
            log.error("No serial port found. Use --serial-port or --list-ports.")
            sys.exit(1)
        modem = ModemHandler(port=port, baud=args.baud,
                             manual_answer=args.manual_answer,
                             direct=args.direct)

    bridge = NetlinkBridge(
        modem=modem,
        server_host=server_host,
        server_port=server_port,
        verbose=args.verbose,
    )
    bridge.run()


if __name__ == "__main__":
    main()
