"""
سرفر أسعار لحظي — يحافظ على اتصال WebSocket دائم مع Kraken ويخدم آخر سعر
لحظة بلحظة عبر HTTP محلي (127.0.0.1) للبوت. بدون أي مكتبة خارجية
(WebSocket client مكتوب بالمكتبة القياسية فقط).

التشغيل:
    python price_server.py

الإعدادات (متغيرات بيئة):
    PRICE_SERVER_SYMBOLS   رموز البوت، افتراضياً "XBTUSD"
    PRICE_SERVER_PORT      افتراضياً 8765
    PRICE_SERVER_HOST      افتراضياً 127.0.0.1
    PRICE_WS_URL           افتراضياً wss://ws.kraken.com/v2

نقاط HTTP:
    GET /price?symbol=XBTUSD  ->  {"symbol","price","bid","ask","age"}   (age بالثواني)
    GET /health               ->  حالة كل رمز

لو انقطع الاتصال يعيد المحاولة تلقائياً (backoff حتى 30 ثانية)، والبوت يرجع
لـ REST لو كان السعر قديم (age كبير) أو السرفر واقف.
"""

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

WS_URL = os.environ.get("PRICE_WS_URL", "wss://ws.kraken.com/v2")
HOST = os.environ.get("PRICE_SERVER_HOST", "127.0.0.1")
PORT = int(os.environ.get("PRICE_SERVER_PORT", "8765"))
SYMBOLS = [s.strip().upper() for s in os.environ.get("PRICE_SERVER_SYMBOLS", "XBTUSD").split(",") if s.strip()]

# رمز البوت (Kraken REST) -> رمز Kraken WebSocket v2
WS_SYMBOL = {"XBTUSD": "BTC/USD", "PAXGUSD": "PAXG/USD", "ETHUSD": "ETH/USD"}

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def log(msg):
    print(f"[price_server {time.strftime('%H:%M:%S', time.gmtime())} UTC] {msg}", flush=True)


# ------------------------------------------------------------------
# WebSocket client (RFC 6455) — بالمكتبة القياسية فقط
# ------------------------------------------------------------------
class WSClient:
    def __init__(self, url, timeout=30.0):
        u = urlparse(url)
        self.tls = u.scheme == "wss"
        self.host = u.hostname
        self.port = u.port or (443 if self.tls else 80)
        self.path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.timeout = timeout
        self.sock = None
        self._buf = bytearray()

    def connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=10)
        if self.tls:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {self.path} HTTP/1.1\r\nHost: {self.host}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("الاتصال أُغلق أثناء المصافحة")
            buf += chunk
            if len(buf) > 65536:
                raise ConnectionError("رد المصافحة كبير بشكل غير طبيعي")
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise ConnectionError(f"فشلت المصافحة: {lines[0]}")
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        expected = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise ConnectionError("Sec-WebSocket-Accept غير صحيح")
        self._buf = bytearray(rest)
        sock.settimeout(self.timeout)
        self.sock = sock

    def close(self):
        if self.sock:
            try:
                self._send_frame(0x8, b"")
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _read_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("الاتصال أُغلق")
            self._buf.extend(chunk)
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def _send_frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_message(self):
        """يرجع نص الرسالة التالية (يتعامل مع ping/pong والتجزئة)."""
        parts = []
        msg_opcode = None
        while True:
            b1, b2 = self._read_exact(2)
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("الخادم أغلق الاتصال")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                msg_opcode = opcode
                parts = [payload]
            elif opcode == 0x0:
                parts.append(payload)
            if fin:
                data = b"".join(parts)
                if msg_opcode == 0x1:
                    return data.decode("utf-8", errors="replace")
                parts = []
                msg_opcode = None


# ------------------------------------------------------------------
# تغذية الأسعار
# ------------------------------------------------------------------
class PriceFeed(threading.Thread):
    def __init__(self, url, symbols):
        super().__init__(daemon=True)
        self.url = url
        self.symbols = [s for s in symbols if s in WS_SYMBOL]
        self.ws_to_bot = {WS_SYMBOL[s]: s for s in self.symbols}
        self.prices = {}  # رمز البوت -> {"price","bid","ask","ts"}
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.connected = False
        self.reconnects = 0

    def handle(self, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if msg.get("channel") != "ticker":
            return
        now = time.time()
        for d in msg.get("data") or []:
            sym = self.ws_to_bot.get(d.get("symbol"))
            last = d.get("last")
            if not sym or last is None:
                continue
            with self.lock:
                self.prices[sym] = {"price": float(last), "bid": d.get("bid"), "ask": d.get("ask"), "ts": now}

    def run(self):
        backoff = 1.0
        while not self.stopped.is_set():
            ws = WSClient(self.url)
            try:
                ws.connect()
                ws.send_text(json.dumps({
                    "method": "subscribe",
                    "params": {"channel": "ticker", "symbol": list(self.ws_to_bot.keys())},
                }))
                self.connected = True
                log(f"متصل بـ {self.url} — {list(self.ws_to_bot.keys())}")
                backoff = 1.0
                while not self.stopped.is_set():
                    self.handle(ws.recv_message())
            except Exception as e:
                if not self.stopped.is_set():
                    log(f"انقطع الاتصال: {type(e).__name__}: {e} — إعادة المحاولة بعد {backoff:.0f}ث")
            finally:
                self.connected = False
                ws.close()
            if self.stopped.wait(backoff):
                break
            self.reconnects += 1
            backoff = min(backoff * 2, 30.0)

    def get(self, symbol):
        with self.lock:
            p = self.prices.get(symbol.upper())
            if not p:
                return None
            return {"symbol": symbol.upper(), "price": p["price"], "bid": p["bid"], "ask": p["ask"],
                    "age": round(time.time() - p["ts"], 3)}

    def health(self):
        with self.lock:
            return {
                "connected": self.connected, "reconnects": self.reconnects,
                "symbols": {s: round(time.time() - p["ts"], 3) for s, p in self.prices.items()},
            }


# ------------------------------------------------------------------
# HTTP محلي
# ------------------------------------------------------------------
def make_handler(feed):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/price":
                sym = (parse_qs(u.query).get("symbol") or [""])[0]
                p = feed.get(sym) if sym else None
                if p:
                    self._send(200, p)
                else:
                    self._send(404, {"error": "no price", "symbol": sym})
            elif u.path == "/health":
                self._send(200, feed.health())
            else:
                self._send(404, {"error": "not found"})

        def log_message(self, *args):
            pass

    return Handler


def main():
    feed = PriceFeed(WS_URL, SYMBOLS)
    if not feed.symbols:
        log(f"لا توجد رموز مدعومة في {SYMBOLS} (المدعوم: {list(WS_SYMBOL)})")
        sys.exit(1)
    feed.start()
    server = ThreadingHTTPServer((HOST, PORT), make_handler(feed))
    log(f"السرفر يخدم http://{HOST}:{PORT}  الرموز: {feed.symbols}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        feed.stopped.set()
        server.server_close()


if __name__ == "__main__":
    main()
