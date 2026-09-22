"""De drie RGB-LED's van de HDC.

De vendor-dienst `led-controller` (blijft draaien) leest elke 100 ms /tmp/led.json:
{"leds": [{"index": 0..2, "red": 0..25, "green": 0..25, "blue": 0..25, "on": bool}]}.
Wij schrijven dat bestand. /tmp is RAM, dus 10x per seconde schrijven slijt niets.
"""
import calendar
import json
import os
import threading
import time

LED_FILE = "/tmp/led.json"
FUNCTIONS = {
    "uit": "uit",
    "lora": "LoRa-verkeer (blauw ontvangen, wit zenden, paars vreemd apparaat)",
    "link": "verbindingskwaliteit (groen/oranje/rood, knippert rood bij geen antwoord)",
    "systeem": "systeem (blauw thuiswifi, paars eigen wifi, oranje knipper GPS zoekt, rood fout)",
    "gps": "GPS (groen 3D, oranje 2D, rood knipper zoekt)",
    "rood": "vast rood", "groen": "vast groen", "blauw": "vast blauw", "wit": "vast wit",
}
C = {
    "uit": (0, 0, 0), "rood": (25, 0, 0), "groen": (0, 25, 0), "blauw": (0, 0, 25), "wit": (18, 18, 18),
    "oranje": (25, 8, 0), "paars": (14, 0, 25), "cyaan": (0, 18, 18),
}


def nl_hour(t=None):
    """Uur in Nederlandse tijd zonder tz-database: CET/CEST met de EU-zomertijdregel."""
    t = time.time() if t is None else t
    y = time.gmtime(t).tm_year

    def last_sunday_utc(month):
        # laatste zondag van de maand, 01:00 UTC
        for day in range(31, 24, -1):
            ts = calendar.timegm((y, month, day, 1, 0, 0, 0, 0, 0))
            if time.gmtime(ts).tm_mon == month and time.gmtime(ts).tm_wday == 6:
                return ts
        return 0
    offset = 2 if last_sunday_utc(3) <= t < last_sunday_utc(10) else 1
    return time.gmtime(t + offset * 3600).tm_hour


class Leds:
    def __init__(self, station, wifi, gps):
        self.st, self.wh, self.gp = station, wifi, gps
        self.flash_until = 0
        self.flash_color = "uit"
        self.test_until = 0
        self.last_written = None

    def flash(self, kind):
        col = {"rx": "blauw", "tx": "wit", "vreemd": "paars"}.get(kind, "wit")
        # zenden en vreemde apparaten niet laten overschrijven door een gewone ontvangst
        if time.monotonic() < self.flash_until and kind == "rx" and self.flash_color != "blauw":
            return
        self.flash_color = col
        self.flash_until = time.monotonic() + (0.25 if kind == "tx" else 0.15)

    def color_test(self, seconds=9):
        self.test_until = time.monotonic() + seconds

    # --- functies ------------------------------------------------------
    def _blink(self, period=1.0):
        return (time.monotonic() % period) < period / 2

    def _fn(self, name):
        now = time.monotonic()
        if name in C:
            return C[name]
        if name == "lora":
            return C[self.flash_color] if now < self.flash_until else C["uit"]
        if name == "gps":
            f = self.gp.fix
            if not self.gp.has_fix():
                return C["rood"] if self._blink() else C["uit"]
            return C["groen"] if f.get("fix_type", 0) >= 3 else C["oranje"]
        if name == "systeem":
            if self.st.radio_error:
                return C["rood"]
            base = C["blauw"] if self.wh.state.get("mode") == "home" else C["paars"]
            if not self.gp.has_fix() and not self._blink(1.4):
                return C["oranje"]
            return base
        if name == "link":
            return self._link()
        return C["uit"]

    def _link(self):
        st = self.st
        c = st.cfg
        rl = [r for r in list(st.range_log)[-5:] if r.get("side") == "mobiel" and r.get("ok") is not None]
        if c.get("range_test") and rl and time.time() - rl[-1]["time"] < 3 * float(c.get("range_interval_s", 10)) + 5:
            last = rl[-1]
            if not last["ok"]:
                return C["rood"] if self._blink(0.6) else C["uit"]
            return self._rssi_color(last.get("rssi_here"), last.get("rssi_there"))
        peers = [p for p in st.peers.values() if p.get("last")]
        if not peers:
            return C["uit"]
        p = max(peers, key=lambda x: x["last"])
        if time.time() - p["last"] > 60:
            return C["rood"] if self._blink(2.0) else C["uit"]
        return self._rssi_color(p.get("rssi"), p.get("their_rssi"))

    @staticmethod
    def _rssi_color(*vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return C["groen"]
        rssi = min(vals)                          # de zwakste richting bepaalt de kleur
        return C["groen"] if rssi >= -90 else (C["oranje"] if rssi >= -110 else C["rood"])

    # --- lus -----------------------------------------------------------
    def frame(self):
        c = self.st.cfg
        now = time.monotonic()
        if now < self.test_until:
            # kleurtest: alle drie tegelijk rood, dan groen, dan blauw (3 s per kleur)
            step = int((self.test_until - now) // 3)
            col = [C["blauw"], C["groen"], C["rood"]][min(step, 2)]
            cols = [col, col, col]
        else:
            fns = (c.get("led_fns") or ["lora", "link", "systeem"])[:3]
            cols = [self._fn(f) for f in fns] + [C["uit"]] * (3 - len(fns))
        br = max(0, min(100, int(c.get("led_brightness", 100)))) / 100
        if c.get("led_night", True):
            h = nl_hour()
            if h >= 22 or h < 7:
                br *= 0.2
        leds = []
        for i, (r, g, b) in enumerate(cols):
            r, g, b = (int(round(v * br)) for v in (r, g, b))
            if (r or g or b) and br > 0:
                r, g, b = (max(1, v) if v else 0 for v in (r, g, b))   # gedimd niet helemaal uitdoven
            leds.append({"index": i, "red": r, "green": g, "blue": b, "on": bool(r or g or b)})
        return {"leds": leds}

    def run(self):
        while True:
            try:
                out = json.dumps(self.frame())
                if out != self.last_written:
                    # Op zijn plek overschrijven, zoals de vendor (fs.writeFile). led-controller volgt het
                    # bestand met inotify; een tmp+rename geeft een nieuw inode en dan ziet hij niets meer.
                    with open(LED_FILE, "w") as f:
                        f.write(out)
                    self.last_written = out
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.05)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()
        return self
