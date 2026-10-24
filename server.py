#!/usr/bin/env python3
"""
Relais 10EaZy -> tableau de bord plein écran.

  python server.py --demo                # données simulées (accélérées x10) pour voir l'interface
  python server.py --ip 192.168.40.221   # PC qui fait tourner 10EaZy (ou saisir l'IP dans l'interface)
  python server.py --test capture.png    # vérifier la lecture d'une capture du panneau

Puis ouvrir http://localhost:8080 (ou http://<ip-de-ce-pc>:8080 depuis une autre machine).

10EaZy ne publie pas de données : son WebViewer affiche une image du panneau LabVIEW.
Ce serveur télécharge cette image et lit les chiffres dessus.
Prérequis : Python 3.9+ et Pillow (pip install pillow).
"""
import argparse
import io
import json
import math
import random
import re
import socket
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --- Seuils ------------------------------------------------------------------
ORANGE_DB = 96.0          # LAeq15 au-dessus -> orange
RED_DB = 100.0            # LAeq15 au-dessus -> rouge
ALARM_AFTER_S = 5 * 60    # rouge depuis 5 min -> clignotement
WINDOW_S = 15 * 60        # fenêtre glissante du LAeq

# --- Source 10EaZy ------------------------------------------------------------
# Image du panneau, celle qu'affiche le WebViewer de 10EaZy
SOURCE_URL = "http://{host}/.snap?10EaZy%20Development.lvproj/My%20Computer/Remote_Panel.vi&{rand}"
POLL_S = 1.0


# --- Lecture des chiffres sur l'image du panneau 10EaZy ---------------------
REF_W = 847                      # largeur de référence du panneau
BAND = (86, 133)                 # bande verticale des grands chiffres
DIGIT_Y = (91, 123)              # hauteur d'un chiffre
GW = 24                          # largeur normalisée d'un chiffre
FIELDS = ("laeq_1s", "laeq_15", "laeq_60", "lceq_1s", "lceq_15", "lceq_60")


def _glyph_groups(data):
    """Découpe les 6 grandes valeurs en glyphes : entier (bitmap) ou '.' (virgule)."""
    from PIL import Image  # pip install pillow
    im = Image.open(io.BytesIO(data)).convert("RGB")
    if im.width != REF_W:
        im = im.resize((REF_W, round(im.height * REF_W / im.width)))
    h = BAND[1] - BAND[0]
    px = im.crop((0, BAND[0], REF_W, BAND[1])).tobytes()  # RGB brut

    def green(i):
        r, g, b = px[i], px[i + 1], px[i + 2]
        return g > 140 and g - b > 60 and g - r > 20

    mask = [[green(3 * (y * REF_W + x)) for x in range(REF_W)] for y in range(h)]
    cols = [any(mask[y][x] for y in range(h)) for x in range(REF_W)] + [False]

    segs, s = [], None
    for x, c in enumerate(cols):
        if c and s is None:
            s = x
        elif not c and s is not None:
            segs.append((s, x))
            s = None

    groups, cur, last = [], [], None
    for s, e in segs:
        if last is not None and s - last > 25:
            groups.append(cur)
            cur = []
        cur.append((s, e))
        last = e
    if cur:
        groups.append(cur)

    y0, y1 = DIGIT_Y[0] - BAND[0], DIGIT_Y[1] - BAND[0]
    out = []
    for grp in groups:
        glyphs = []
        for s, e in grp:
            top = next((y for y in range(h) if any(mask[y][s:e])), 0)
            if e - s <= 7 and top > 110 - BAND[0]:
                glyphs.append(".")
                continue
            w = min(GW, e - s)
            off = (GW - w) // 2
            bits = 0
            for y in range(y0, y1):
                row = mask[y]
                for i in range(GW):
                    j = i - off
                    bits = (bits << 1) | (1 if 0 <= j < w and row[s + j] else 0)
            glyphs.append(bits)
        out.append(glyphs)
    return out


def read_panel(data):
    """Image (PNG/JPEG, bytes) du panneau -> dict des 6 valeurs. ValueError si illisible."""
    groups = _glyph_groups(data)
    if len(groups) != 6:
        raise ValueError(f"panneau non reconnu ({len(groups)} valeurs trouvées au lieu de 6)")
    values = {}
    for name, glyphs in zip(FIELDS, groups):
        txt = "".join(g if g == "." else
                      min(DIGITS, key=lambda d: bin(g ^ DIGITS[d]).count("1")) for g in glyphs)
        if not re.fullmatch(r"\d{2,3}\.\d", txt) or not 20 <= float(txt) <= 150:
            raise ValueError(f"lecture douteuse pour {name} : {txt!r}")
        values[name] = float(txt)
    return values


DIGITS = {  # gabarits 32x24 extraits du panneau 10EaZy
    "0": 0x007e0001ff8003ffc007ffe00fc3f00f00f01f00f81e00781e00783e003c3c003c3c003c3c003c3c003c3c003c3c003c3c003c3c003c3c003c3c003c3c003c3c003c3c003c1e00781e00781f00f80f00f00fc3f007ffe003ffc001ff80007e00,
    "1": 0x000380000380000780000f80001f80003f8000ff8001ff8003f78003c780038780020780000780000780000780000780000780000780000780000780000780000780000780000780000780000780000780000780000780000780000780000780,
    "2": 0x00ff0003ffc00fffe00ffff01f83f83f00f83e00783c007c3c007c1c003c00007c0000780000780000f80001f00003e00007c0000fc0001f80003f00007c0001f80003f00007e0000fc0000f80001f00003e00003ffffc3ffffc7ffffc7ffffc,
    "3": 0x00fe0003ff8007ffc00fffe01f83f01f01f01e00f03e00f80c00f80000f00000f00001f00007e0003fc0003f80003fe0003ff00000f800007800007c00003c00003c0c003c3c003c3e003c3e007c1f00f81fc3f80ffff007ffe003ffc000fe00,
    "4": 0x0001e00001e00003e00007e00007e0000fe0001fe0003de00039e00079e000f1e000e1e001e1e003c1e00781e00781e00f01e01e01e03c01e03c01e07ffffe7ffffe7ffffe7ffffe0001e00001e00001e00001e00001e00001e00001e00001e0,
    "5": 0x07fff807fff807fff807fff80780000f00000f00000f00000f00000e00001e3f001effc01fffe01ffff01f83f83e00f80c007c00007c00003c00003c00003c00003c00003c3c003c3c007c3e00781f00f81f83f00fffe007ffe003ff8000fe00,
    "6": 0x007f0001ffc003ffe007fff00fc1f80f80f81f00781e007c3e00003c00003c00003c3f003cffc03dffe03ffff03fc1f83f00f83e007c3e003c3c003c3c003c3c003c3c003c3c003c3e003c1e00781f00f80fc1f807fff003ffe001ffc0007f00,
    "7": 0x3ffffc3ffffc3ffffc3ffffc0000780000f00001f00003e00003c00007c0000780000f80000f00001f00001e00003e00003c00007c0000780000780000f80000f00000f00000f00001e00001e00001e00001e00001e00003e00003c00003c000,
    "8": 0x00ff0003ffc007ffe00ffff00fc3f01f00f81e00781e00781e00781e00780f00f00fc3f007ffe001ff8003ffc007ffe00f83f01f00f83e00783c003c3c003c3c003c3c003c3c003c3c003c3e007c1f00f81fc1f80ffff007ffe003ffc000ff00,
    "9": 0x00fe0003ff8007ffc00fffe01fc3f01f00f83e00783e00783c007c3c003c3c003c3c003c3c003c3c007c3e007c1f00fc1f83fc0ffffc07ffbc03ff3c00fc3c00003c00007c0000783e00781e00f81f01f01f83f00fffe007ffc003ff8000fe00,
}


# --- Horloge (accélérable pour la démo) ---------------------------------------
_T0 = time.time()
SPEED = 1.0


def clock():
    return _T0 + (time.time() - _T0) * SPEED


# --- Mesure ----------------------------------------------------------------
class Meter:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with getattr(self, "lock", threading.Lock()):
            self.samples = deque()      # (t, LAS)
            self.las = None
            self.src_leq = None
            self.over_since = None
            self.last_ok = 0.0
            self.error = None
            self.extra = {}

    def push(self, las, src_leq, extra=None):
        t = clock()
        with self.lock:
            if las is not None:
                self.las = las
                self.samples.append((t, las))
            self.src_leq = src_leq
            self.extra = extra or {}
            while self.samples and self.samples[0][0] < t - WINDOW_S:
                self.samples.popleft()
            self.last_ok, self.error = t, None
            leq = self._leq15()
            if leq is not None and leq > RED_DB:
                self.over_since = self.over_since or t
            else:
                self.over_since = None

    def fail(self, msg):
        with self.lock:
            self.error = msg

    def _leq15(self):
        if self.src_leq is not None:
            return self.src_leq
        if not self.samples:
            return None
        energy = sum(10 ** (l / 10) for _, l in self.samples) / len(self.samples)
        return 10 * math.log10(energy)

    def snapshot(self):
        now = clock()
        with self.lock:
            leq = self._leq15()
            level = None if leq is None else "bad" if leq > RED_DB else "warn" if leq > ORANGE_DB else "ok"
            over = now - self.over_since if self.over_since else 0
            window = WINDOW_S if self.src_leq is not None else (
                min(WINDOW_S, now - self.samples[0][0]) if self.samples else 0)
            return {
                "t": now, "las": self.las, "laeq15": leq, "level": level,
                "over_s": over, "alarm": over >= ALARM_AFTER_S, "window_s": window,
                "connected": now - self.last_ok < 5 * SPEED,
                "error": self.error, "host": CONFIG["host"], "demo": CONFIG["demo"],
                "thresholds": [ORANGE_DB, RED_DB], "extra": self.extra,
            }

    def history(self):
        with self.lock:
            return [[t, l] for t, l in self.samples]


CONFIG = {"host": None, "demo": False}
METER = Meter()


def poller():
    lvl = 92.0
    while True:
        if CONFIG["demo"]:
            target = 98 + 7 * math.sin((clock() - _T0) / 700)
            lvl += (target - lvl) * 0.2 + random.gauss(0, 1.3)
            METER.push(round(lvl, 1), None, {"laeq_60": round(target - 1.5, 1),
                                             "lceq_15": round(target + 13, 1)})
            time.sleep(POLL_S / SPEED)
            continue
        if CONFIG["host"]:
            try:
                url = SOURCE_URL.format(host=CONFIG["host"], rand=random.randint(0, 10**9))
                with urllib.request.urlopen(url, timeout=4) as r:
                    data = r.read()
                v = read_panel(data)
                METER.push(v["laeq_1s"], v["laeq_15"], v)
            except ImportError:
                METER.fail("Pillow manquant : pip install pillow")
            except Exception as e:  # noqa: BLE001
                METER.fail(str(e))
        time.sleep(POLL_S)


# --- Serveur HTTP -------------------------------------------------------------
HERE = Path(__file__).resolve().parent


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send((HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json(METER.snapshot())
        elif path == "/api/history":
            self._json(METER.history())
        elif path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(f"data: {json.dumps(METER.snapshot())}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        else:
            self._send(b"not found", "text/plain", 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        if self.path == "/api/config":
            host = str(body.get("host", "")).strip().removeprefix("http://").strip("/")
            CONFIG["host"] = host or None
            if host:
                CONFIG["demo"] = False
            METER.reset()
            self._json({"ok": True, "host": CONFIG["host"]})
        elif self.path == "/api/reset":
            METER.reset()
            self._json({"ok": True})
        else:
            self._send(b"not found", "text/plain", 404)


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main():
    global SPEED
    ap = argparse.ArgumentParser(description="Relais d'affichage 10EaZy")
    ap.add_argument("--ip", help="IP (ou ip:port) du PC 10EaZy")
    ap.add_argument("--port", type=int, default=8080, help="port du tableau de bord (8080)")
    ap.add_argument("--demo", action="store_true", help="données simulées")
    ap.add_argument("--test", metavar="IMAGE", help="lire une capture du panneau et afficher les valeurs")
    ap.add_argument("--speed", type=float, default=10, help="accélération du temps en démo (10)")
    a = ap.parse_args()

    if a.test:
        with open(a.test, "rb") as f:
            for k, v in read_panel(f.read()).items():
                print(f"{k:8} {v:6.1f} dB")
        return

    CONFIG["host"], CONFIG["demo"] = a.ip, a.demo
    if a.demo:
        SPEED = a.speed
    threading.Thread(target=poller, daemon=True).start()

    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    srv.daemon_threads = True
    print(f"Tableau de bord : http://localhost:{a.port}  |  réseau : http://{local_ip()}:{a.port}")
    if a.demo:
        print(f"Mode démo, temps accéléré x{SPEED:g}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
