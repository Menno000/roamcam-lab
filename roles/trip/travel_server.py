#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Roamcam Reis -- timelapse/roadtrip-unit voor de Hivemapper HDC.

Andere gedachtengang dan dashboard_server.py: die duwt de vendor-firmware opzij en neemt de
camera zelf over. Dit programma doet het omgekeerde en lift mee:

  * libcamera-bridge draait gewoon door en zet 10 fps MJPEG 2028x1024 in /tmp/recording/pic
    (RAM). De vendor-purger houdt die map op 400 MB -- dat is ~100 s rollende geschiedenis.
    Wij lezen alleen het nieuwste bestand. Kost nul CPU en nul camera-onderhandeling.
  * De datalogger schrijft 2x per seconde een complete GPS-regel naar /mnt/data/gps/latest.log
    en de IMU naar /mnt/data/imu/latest.log. We hoeven de u-blox dus niet zelf aan te spreken.

Gevolg: opnemen, live kijken en timelapse kunnen tegelijk, want we vragen de camera niets.

Foto's worden op AFSTAND getriggerd, niet op tijd -- elke N meter een frame. Zo staat er geen
half uur stoplicht in je film en loopt de film gelijkmatig, of je nu 30 of 130 reed. Zonder
GPS-fix valt hij automatisch terug op een tijdinterval.

Alleen /mnt/data overleeft een herstart; / is een RAM-overlay. Alle staat staat dus onder BASE.
"""

import calendar
import http.server
import json
import math
import os
import re
import shutil
import socketserver
import sys
import threading
import time

# De kern staat op het toestel in /mnt/data en in de repo naast deze map.
for _p in ("/mnt/data",
           os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"),
           os.path.dirname(os.path.abspath(__file__))):
    if os.path.exists(os.path.join(_p, "roamcam_core.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
import roamcam_core as core  # noqa: E402
from urllib.parse import urlparse, parse_qs, unquote

PORT = 8080

PIC_DIR = "/tmp/recording/pic"          # rollende JPEG-buffer van de vendor-bridge
GPS_LOG = "/mnt/data/gps/latest.log"
IMU_LOG = "/mnt/data/imu/latest.log"

BASE = "/mnt/data/travel"
TRIPS = BASE + "/trips"
CFG_PATH = BASE + "/config.json"
LOG_PATH = BASE + "/travel.log"
CLOCK_PATH = BASE + "/lastclock"

DEFAULT_CFG = {
    "name": "Roamcam Reis",
    "mode": "auto",          # auto | distance | time
    "dist_m": 50,            # afstandsinterval
    "time_s": 5,             # tijdinterval (bij mode=time of als er geen fix is)
    "still_time_s": 60,      # bij stilstand mét fix toch af en toe een frame; 0 = uit
    "tz": "Europe/Amsterdam",
    "keep_free_mb": 1000,    # stop met opslaan als er minder vrij is
    "film_fps": 12,
    "live_fps": 5,
    "auto_trip": True,       # zelf een reis beginnen zodra je gaat rijden
}

cfg = dict(DEFAULT_CFG)

# Alles wat een verstreken tijd meet gebruikt time.monotonic() en niet de wandklok. De HDC
# heeft geen RTC-batterij: hij start in 2022 en springt jaren vooruit zodra de GPS de klok
# gelijkzet. Met de wandklok dacht de automatische stop daardoor dat de auto al jaren stilstond
# en sloot hij een reis na één seconde weer af.
state = {
    "trip": None,            # id van de lopende reis, of None
    "frames": 0,
    "dist_m": 0.0,
    "started": 0,            # wandklok, alleen om te tonen
    "started_mono": 0,
    "last_cap": 0,           # monotonic
    "since_last_m": 0.0,
    "last_fix": None,        # (lat, lon) van het vorige GPS-punt met fix
    "moving_since": 0,
    "still_since": 0,
    "last_error": "",
    "film": {},              # trip_id -> "bezig" | "klaar" | foutmelding
}

_lock = threading.Lock()


# ---------------------------------------------------------------- basis


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
        if os.path.getsize(LOG_PATH) > 512 * 1024:
            keep = open(LOG_PATH).read()[-256 * 1024:]
            open(LOG_PATH, "w").write(keep)
    except Exception:
        pass
    print(line, flush=True)


def sh(cmd, timeout=10):
    """Gaat via de gedeelde kern, die bij een timeout de hele procesgroep afbreekt.

    De oude versie hier gebruikte `subprocess.run(timeout=)` en die houdt zich niet aan zijn
    eigen timeout zodra de opdracht kleinkinderen heeft -- zoals onze ffmpeg-ketens.
    """
    return core.sh(cmd, timeout)


def load_cfg():
    global cfg
    c = dict(DEFAULT_CFG)
    try:
        c.update(json.load(open(CFG_PATH)))
    except Exception:
        pass
    cfg = c


def save_cfg():
    try:
        os.makedirs(BASE, exist_ok=True)
        tmp = CFG_PATH + ".tmp"
        json.dump(cfg, open(tmp, "w"))
        os.replace(tmp, CFG_PATH)
        return True
    except Exception as e:
        state["last_error"] = "instellingen opslaan mislukt: %s" % e
        return False


def apply_tz():
    """Zonder dit staat alles in UTC en kloppen de tijden in de rittenlijst niet."""
    core.apply_tz(cfg.get("tz"))


def free_mb(path=BASE):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize // (1024 * 1024)
    except Exception:
        return 0


def dir_size(path):
    tot = 0
    try:
        for n in os.listdir(path):
            try:
                tot += os.path.getsize(os.path.join(path, n))
            except Exception:
                pass
    except Exception:
        pass
    return tot


# ---------------------------------------------------------------- klok

def clock_save():
    core.clock_save(CLOCK_PATH)


def clock_restore():
    """De HDC heeft geen RTC-batterij: na stroomloos starten staat hij in 2022. Eerst de
    laatst bekende tijd terugzetten zodat logs en mapnamen niet in het verleden belanden;
    zodra de GPS een fix heeft nemen we die tijd over (die is exact)."""
    if core.clock_restore(CLOCK_PATH):
        log("klok teruggezet naar bewaarde tijd")


def gps_is_live():
    """latest.log blijft na een herstart gewoon staan met de gegevens van vóór het uitzetten.
    Vlak na het opstarten lees je dus oude tijd en oude positie. De kern leest twee keer en
    eist dat het tijdstempel verandert; onze eigen lezer gaat mee, want die verrijkt de regel
    met fix_ok, kmh en het aantal satellieten."""
    return core.gps_is_live(GPS_LOG, reader=lambda _p: gps_read())


def clock_from_gps(gp):
    """GPS-tijd is UTC en absoluut betrouwbaar zodra time_resolved gezet is."""
    try:
        ts = gp.get("timestamp") or ""
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", ts)
        if not m:
            return False
        # timegm en niet mktime: de GPS-tijd is UTC, mktime zou hem als lokale tijd lezen
        want = calendar.timegm(time.strptime(" ".join(m.groups()), "%Y %m %d %H %M %S"))
        if abs(want - time.time()) > 5:
            core.clock_set_utc(want)
            log("klok gelijkgezet met GPS")
            clock_save()
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------- sensoren

def gps_read():
    """Geeft het laatste GPS-punt terug, of None. fix_ok betekent: bruikbaar om afstand mee
    te meten. Zonder die controle telt drift van een unit op tafel gewoon door als kilometers,
    dat hebben we op unit 1 al een keer als spookrit teruggezien."""
    try:
        d = json.load(open(GPS_LOG))
    except Exception:
        return None
    fix = str(d.get("fix") or "")
    acc = d.get("horizontal_accuracy")
    d["fix_ok"] = fix in ("2D", "3D") and isinstance(acc, (int, float)) and acc < 25
    d["kmh"] = round((d.get("speed") or 0) * 3.6, 1)
    sats = d.get("satellites") or {}
    d["sats_used"] = sats.get("used", 0)
    d["sats_seen"] = sats.get("seen", 0)
    return d


def imu_read():
    try:
        return json.load(open(IMU_LOG))
    except Exception:
        return None


# Afstand in meters, argumenten lat, lon, lat, lon -- uit de gedeelde kern.
haversine = core.haversine


# ---------------------------------------------------------------- beeld

def newest_frame():
    """Nieuwste JPEG uit de RAM-buffer. De bridge schrijft eerst .jpg.tmp en hernoemt daarna,
    dus op .jpg filteren geeft altijd een compleet bestand. latest.txt is er soms en scheelt
    een listdir van duizend namen."""
    try:
        lt = os.path.join(PIC_DIR, "latest.txt")
        if os.path.exists(lt):
            n = open(lt).read().strip().split("/")[-1]
            p = os.path.join(PIC_DIR, n)
            if n.endswith(".jpg") and os.path.exists(p):
                return p
    except Exception:
        pass
    try:
        best = None
        for n in os.listdir(PIC_DIR):
            # naam = <10 cijfers>_<6 cijfers>.jpg, dus alfabetisch == chronologisch
            if n.endswith(".jpg") and (best is None or n > best):
                best = n
        return os.path.join(PIC_DIR, best) if best else None
    except Exception:
        return None


def camera_available():
    return os.path.isdir(PIC_DIR) and newest_frame() is not None


# ---------------------------------------------------------------- reizen

def trip_dir(tid):
    return os.path.join(TRIPS, tid)


def trip_start(manual=True):
    with _lock:
        if state["trip"]:
            return state["trip"]
        tid = time.strftime("%Y%m%d_%H%M%S")
        d = trip_dir(tid)
        os.makedirs(d, exist_ok=True)
        state.update(trip=tid, frames=0, dist_m=0.0, started=time.time(),
                     started_mono=time.monotonic(), last_cap=0, since_last_m=0.0, last_fix=None)
        meta = {"id": tid, "start": time.time(), "end": 0, "frames": 0, "dist_m": 0,
                "title": "", "auto": not manual}
        json.dump(meta, open(os.path.join(d, "meta.json"), "w"))
    log("reis gestart: %s (%s)" % (tid, "handmatig" if manual else "automatisch"))
    return tid


def trip_stop():
    with _lock:
        tid = state["trip"]
        if not tid:
            return None
        d = trip_dir(tid)
        try:
            meta = json.load(open(os.path.join(d, "meta.json")))
        except Exception:
            meta = {"id": tid, "start": state["started"]}
        meta.update(end=time.time(), frames=state["frames"], dist_m=round(state["dist_m"]))
        json.dump(meta, open(os.path.join(d, "meta.json"), "w"))
        state.update(trip=None)
    log("reis gestopt: %s -- %d foto's, %.1f km" % (tid, meta.get("frames", 0),
                                                    meta.get("dist_m", 0) / 1000.0))
    return tid


def trip_capture(gp):
    """Kopieert het nieuwste frame uit RAM naar de reismap en legt het GPS-punt vast."""
    tid = state["trip"]
    src = newest_frame()
    if not tid or not src:
        return False
    if free_mb() < cfg["keep_free_mb"]:
        if state["last_error"] != "opslag vol":
            state["last_error"] = "opslag vol"
            log("opslag onder de grens -- stop met vastleggen")
        return False
    d = trip_dir(tid)
    i = state["frames"] + 1
    dst = os.path.join(d, "f%06d.jpg" % i)
    try:
        shutil.copyfile(src, dst)
    except Exception as e:
        state["last_error"] = "frame opslaan mislukt: %s" % e
        return False
    pt = {"i": i, "t": round(time.time(), 1)}
    if gp:
        pt.update(fix=bool(gp.get("fix_ok")), lat=gp.get("latitude"), lon=gp.get("longitude"),
                  alt=gp.get("height"), kmh=gp.get("kmh"), hdg=gp.get("heading"))
    try:
        with open(os.path.join(d, "points.jsonl"), "a") as f:
            f.write(json.dumps(pt) + "\n")
    except Exception:
        pass
    state["frames"] = i
    state["last_cap"] = time.monotonic()
    state["since_last_m"] = 0.0
    if state["last_error"] == "opslag vol":
        state["last_error"] = ""
    return True


def trip_list():
    out = []
    try:
        for tid in sorted(os.listdir(TRIPS), reverse=True):
            d = trip_dir(tid)
            if not os.path.isdir(d):
                continue
            try:
                meta = json.load(open(os.path.join(d, "meta.json")))
            except Exception:
                meta = {"id": tid, "start": 0}
            meta["bytes"] = dir_size(d)
            meta["film"] = os.path.exists(os.path.join(d, "film.mp4"))
            meta["running"] = (tid == state["trip"])
            if meta["running"]:
                meta["frames"] = state["frames"]
                meta["dist_m"] = round(state["dist_m"])
            out.append(meta)
    except Exception:
        pass
    return out


def trip_points(tid):
    pts = []
    try:
        for ln in open(os.path.join(trip_dir(tid), "points.jsonl")):
            try:
                pts.append(json.loads(ln))
            except Exception:
                pass
    except Exception:
        pass
    return pts


def trip_delete(tid):
    if tid == state["trip"]:
        trip_stop()
    try:
        shutil.rmtree(trip_dir(tid))
        log("reis verwijderd: %s" % tid)
        return True
    except Exception as e:
        state["last_error"] = str(e)
        return False


# ---------------------------------------------------------------- film

def film_make(tid):
    """Bouwt van de losse frames een mp4. De hardware-encoder van de Pi wil niet breder dan
    1920, dus we schalen 2028 -> 1920 terug."""
    d = trip_dir(tid)
    n = len([x for x in os.listdir(d) if x.endswith(".jpg")])
    if n < 2:
        state["film"][tid] = "te weinig foto's"
        return
    state["film"][tid] = "bezig"
    out = os.path.join(d, "film.mp4")
    cmd = ("ffmpeg -y -hide_banner -loglevel error -framerate %d -start_number 1 "
           "-i %s/f%%06d.jpg -vf scale=1920:-2 -c:v h264_v4l2m2m -b:v 8M -pix_fmt yuv420p %s"
           % (int(cfg["film_fps"]), d, out))
    log("film maken voor %s (%d frames)" % (tid, n))
    r = sh(cmd, timeout=1800)
    if os.path.exists(out) and os.path.getsize(out) > 10000:
        state["film"][tid] = "klaar"
        log("film klaar: %s (%.1f MB)" % (tid, os.path.getsize(out) / 1048576.0))
    else:
        # sommige builds struikelen over de hardware-encoder; val terug op mpeg4
        r2 = sh(cmd.replace("h264_v4l2m2m", "mpeg4").replace(".mp4", ".mp4"), timeout=1800)
        if os.path.exists(out) and os.path.getsize(out) > 10000:
            state["film"][tid] = "klaar"
            log("film klaar via mpeg4: %s" % tid)
        else:
            state["film"][tid] = "mislukt"
            log("film mislukt: %s / %s" % (r[-200:], r2[-200:]))


def gpx_for(tid):
    pts = [p for p in trip_points(tid) if p.get("fix") and p.get("lat")]
    head = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="Roamcam Reis" xmlns="http://www.topografix.com/GPX/1/1">\n'
            '<trk><name>%s</name><trkseg>\n' % tid)
    body = "".join(
        '<trkpt lat="%.7f" lon="%.7f"><ele>%.1f</ele><time>%s</time></trkpt>\n'
        % (p["lat"], p["lon"], p.get("alt") or 0,
           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(p.get("t", 0))))
        for p in pts)
    return head + body + "</trkseg></trk></gpx>\n"


def map_svg(tid, w=760, h=420):
    """Kaartje zonder internet: alleen de route zelf, met de foto's als punten erop."""
    pts = [p for p in trip_points(tid) if p.get("fix") and p.get("lat")]
    if len(pts) < 2:
        return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d">'
                '<text x="16" y="30" fill="#888" font-family="sans-serif" font-size="14">'
                'geen GPS-punten in deze reis</text></svg>' % (w, h))
    lats = [p["lat"] for p in pts]
    lons = [p["lon"] for p in pts]
    la0, la1 = min(lats), max(lats)
    lo0, lo1 = min(lons), max(lons)
    k = math.cos(math.radians((la0 + la1) / 2))  # lengtegraden lopen samen naar de polen
    dw = max((lo1 - lo0) * k, 1e-6)
    dh = max(la1 - la0, 1e-6)
    pad = 24
    sc = min((w - 2 * pad) / dw, (h - 2 * pad) / dh)
    ox = (w - dw * sc) / 2
    oy = (h - dh * sc) / 2

    def xy(p):
        return (ox + (p["lon"] - lo0) * k * sc, h - (oy + (p["lat"] - la0) * sc))

    path = " ".join(("M" if i == 0 else "L") + "%.1f %.1f" % xy(p) for i, p in enumerate(pts))
    dots = "".join('<circle cx="%.1f" cy="%.1f" r="2.6" fill="#ffb020" opacity=".85"/>' % xy(p)
                   for p in pts[::max(1, len(pts) // 200)])
    x0, y0 = xy(pts[0])
    x1, y1 = xy(pts[-1])
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">'
            '<path d="%s" fill="none" stroke="#ffb020" stroke-width="2.4" stroke-linejoin="round"'
            ' stroke-linecap="round" opacity=".55"/>%s'
            '<circle cx="%.1f" cy="%.1f" r="6" fill="#2ecc71"/>'
            '<circle cx="%.1f" cy="%.1f" r="6" fill="#e74c3c"/></svg>'
            % (w, h, w, h, path, dots, x0, y0, x1, y1))


# ---------------------------------------------------------------- de lus

def should_capture(mono, gp):
    mode = cfg.get("mode", "auto")
    has = bool(gp and gp.get("fix_ok"))
    if not state["last_cap"]:
        return True                       # eerste frame van een reis meteen
    dt = mono - state["last_cap"]

    if mode == "time" or (mode == "auto" and not has):
        return dt >= max(1, int(cfg["time_s"]))
    if not has:
        return False                      # zuiver op afstand, maar geen fix -> niets doen
    if state["since_last_m"] >= max(5, int(cfg["dist_m"])):
        return True
    still = int(cfg.get("still_time_s") or 0)
    if still and (gp.get("speed") or 0) < 1.0 and dt >= still:
        return True
    return False


def travel_loop():
    # niet op 0 beginnen: dan zou de eerste ronde meteen opnieuw de klok zetten, mogelijk nog
    # met het oude latest.log. Het opstarten heeft de klok al gedaan.
    last_clock_sync = time.monotonic()
    while True:
        try:
            mono = time.monotonic()
            gp = gps_read()

            if gp and gp.get("time_resolved") and mono - last_clock_sync > 3600:
                clock_from_gps(gp)
                last_clock_sync = mono

            # afstand bijhouden, ook als er niet wordt vastgelegd
            if gp and gp.get("fix_ok") and (gp.get("speed") or 0) > 0.8:
                cur = (gp.get("latitude"), gp.get("longitude"))
                if state["last_fix"] and None not in cur:
                    d = haversine(state["last_fix"][0], state["last_fix"][1], cur[0], cur[1])
                    if 0 < d < 400:       # sprongen groter dan dit zijn geen rijden maar ruis
                        state["since_last_m"] += d
                        if state["trip"]:
                            state["dist_m"] += d
                if None not in cur:
                    state["last_fix"] = cur
            elif gp and not gp.get("fix_ok"):
                state["last_fix"] = None

            # zelf een reis beginnen/afsluiten
            if cfg.get("auto_trip") and gp and gp.get("fix_ok"):
                spd = gp.get("speed") or 0
                if spd > 3.0:
                    state["still_since"] = 0
                    state["moving_since"] = state["moving_since"] or mono
                    if not state["trip"] and mono - state["moving_since"] > 8:
                        trip_start(manual=False)
                else:
                    state["moving_since"] = 0
                    state["still_since"] = state["still_since"] or mono
                    if state["trip"] and mono - state["still_since"] > 300:
                        trip_stop()

            if state["trip"] and should_capture(mono, gp):
                trip_capture(gp)

            time.sleep(0.4)
        except Exception as e:
            state["last_error"] = "lus: %s" % e
            time.sleep(2)


# ---------------------------------------------------------------- webserver

def status():
    gp = gps_read()
    im = imu_read()
    d = {
        "name": cfg.get("name"),
        "trip": state["trip"],
        "frames": state["frames"],
        "dist_km": round(state["dist_m"] / 1000.0, 2),
        "started": state["started"],
        "elapsed": int(time.monotonic() - state["started_mono"]) if state["trip"] else 0,
        "since_last_m": round(state["since_last_m"]),
        "camera": camera_available(),
        "free_mb": free_mb(),
        "last_error": state["last_error"],
        "now": time.strftime("%H:%M:%S"),
        "date": time.strftime("%a %d %b"),
        "cfg": cfg,
    }
    if gp:
        d["gps"] = {"fix": gp.get("fix"), "ok": gp.get("fix_ok"), "kmh": gp.get("kmh"),
                    "lat": gp.get("latitude"), "lon": gp.get("longitude"),
                    "alt": round(gp.get("height") or 0), "hdg": round(gp.get("heading") or 0),
                    "used": gp.get("sats_used"), "seen": gp.get("sats_seen"),
                    "acc": gp.get("horizontal_accuracy")}
    else:
        d["gps"] = None
    if im:
        d["temp"] = round(im.get("temp") or 0, 1)
    return d


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj))

    def _file(self, path, ctype, download=None):
        try:
            data = open(path, "rb").read()
        except Exception:
            return self._send(404, "text/plain", "niet gevonden")
        extra = {}
        if download:
            extra["Content-Disposition"] = 'attachment; filename="%s"' % download
        self._send(200, ctype, data, extra)

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query, keep_blank_values=True)

        def arg(k, d=""):
            return q.get(k, [d])[0]

        if p == "/":
            return self._send(200, "text/html; charset=utf-8", PAGE)
        if p == "/status":
            return self._json(status())

        # --- live beeld
        if p == "/live.jpg":
            f = newest_frame()
            if not f:
                return self._send(503, "text/plain", "geen beeld")
            return self._file(f, "image/jpeg")
        if p == "/live.mjpg":
            return self.stream_mjpeg()

        # --- reis besturen
        if p == "/trip/start":
            tid = trip_start()
            return self._json({"ok": True, "trip": tid, "msg": "Reis gestart"})
        if p == "/trip/stop":
            tid = trip_stop()
            return self._json({"ok": bool(tid), "msg": "Reis opgeslagen" if tid else "Er liep geen reis"})
        if p == "/trip/shot":
            ok = trip_capture(gps_read()) if state["trip"] else False
            return self._json({"ok": ok, "msg": "Foto vastgelegd" if ok else "Start eerst een reis"})

        # --- archief
        if p == "/trips":
            return self._json({"trips": trip_list()})
        if p == "/trip/points":
            return self._json({"points": trip_points(arg("id"))})
        if p == "/trip/map.svg":
            return self._send(200, "image/svg+xml", map_svg(arg("id")))
        if p == "/trip/gpx":
            tid = arg("id")
            return self._send(200, "application/gpx+xml", gpx_for(tid),
                              {"Content-Disposition": 'attachment; filename="%s.gpx"' % tid})
        if p == "/trip/frame":
            tid, i = arg("id"), arg("i", "1")
            return self._file(os.path.join(trip_dir(tid), "f%06d.jpg" % int(i)), "image/jpeg")
        if p == "/trip/film":
            tid = arg("id")
            if state["film"].get(tid) == "bezig":
                return self._json({"ok": True, "msg": "Film wordt al gemaakt"})
            threading.Thread(target=film_make, args=(tid,), daemon=True).start()
            return self._json({"ok": True, "msg": "Film maken gestart"})
        if p == "/trip/film_status":
            tid = arg("id")
            return self._json({"state": state["film"].get(tid, ""),
                               "ready": os.path.exists(os.path.join(trip_dir(tid), "film.mp4"))})
        if p == "/trip/film.mp4":
            tid = arg("id")
            return self._file(os.path.join(trip_dir(tid), "film.mp4"), "video/mp4",
                              download="%s.mp4" % tid)
        if p == "/trip/delete":
            ok = trip_delete(arg("id"))
            return self._json({"ok": ok, "msg": "Reis verwijderd" if ok else "Verwijderen mislukt"})

        # --- instellingen
        if p == "/set":
            changed = []
            for k in ("mode", "tz", "name"):
                if k in q:
                    cfg[k] = q[k][0][:64]
                    changed.append(k)
            for k in ("dist_m", "time_s", "still_time_s", "keep_free_mb", "film_fps", "live_fps"):
                if k in q and q[k][0] != "":
                    try:
                        cfg[k] = int(float(q[k][0]))
                        changed.append(k)
                    except Exception:
                        pass
            for k in ("auto_trip",):
                if k in q:
                    cfg[k] = q[k][0] in ("1", "true", "on")
                    changed.append(k)
            ok = save_cfg()
            apply_tz()
            return self._json({"ok": ok, "msg": "Instellingen opgeslagen" if ok
                               else state["last_error"], "changed": changed})
        if p == "/log":
            try:
                return self._send(200, "text/plain; charset=utf-8", open(LOG_PATH).read()[-20000:])
            except Exception:
                return self._send(200, "text/plain", "(leeg)")

        return self._send(404, "text/plain", "niet gevonden")

    def stream_mjpeg(self):
        """Simpelweg het nieuwste bestand doorgeven. De frames zijn al geëncodeerd door de
        vendor-bridge, dus dit kost geen rekenkracht -- alleen netwerk."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=rc")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        except Exception:
            return
        last = None
        delay = 1.0 / max(1, int(cfg.get("live_fps") or 5))
        while True:
            f = newest_frame()
            if f and f != last:
                try:
                    data = open(f, "rb").read()
                except Exception:
                    data = b""
                if len(data) > 1000:
                    try:
                        self.wfile.write(b"--rc\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                    except Exception:
                        return
                    last = f
            time.sleep(delay)


class Srv(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------- pagina

PAGE = r"""<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Roamcam Reis</title>
<style>
:root{--bg:#101014;--card:#1a1a20;--fg:#f2f2f4;--mut:#8b8b96;--acc:#ffb020;--ok:#2ecc71;--bad:#e74c3c;--bd:#2a2a33}
:root[data-theme=light]{--bg:#f5f5f7;--card:#fff;--fg:#16161a;--mut:#6b6b76;--bd:#e2e2e8}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 -apple-system,system-ui,"Segoe UI",sans-serif;padding-bottom:74px}
header{display:flex;align-items:center;gap:10px;padding:14px 16px 10px}
header h1{font-size:19px;margin:0;font-weight:650;letter-spacing:-.2px;flex:1}
.clock{font-variant-numeric:tabular-nums;color:var(--mut);font-size:13px;text-align:right;line-height:1.2}
.iconbtn{background:none;border:1px solid var(--bd);color:var(--mut);border-radius:9px;padding:5px 9px;font-size:14px;cursor:pointer}
main{padding:0 16px}
.view{display:none}.view.on{display:block}
.live{position:relative;border-radius:16px;overflow:hidden;background:#000;aspect-ratio:2028/1024;display:flex;align-items:center;justify-content:center}
.live img{width:100%;height:100%;object-fit:cover;display:block}
.live .off{color:var(--mut);font-size:14px}
.badge{position:absolute;left:10px;top:10px;display:flex;gap:6px;flex-wrap:wrap}
.badge span{background:rgba(0,0,0,.62);color:#fff;border-radius:99px;padding:3px 10px;font-size:12px;font-variant-numeric:tabular-nums;backdrop-filter:blur(4px)}
.rec{background:var(--bad)!important;animation:pulse 1.6s infinite}
@keyframes pulse{50%{opacity:.45}}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0}
.stat{background:var(--card);border:1px solid var(--bd);border-radius:13px;padding:10px 12px}
.stat b{display:block;font-size:21px;font-weight:640;font-variant-numeric:tabular-nums;letter-spacing:-.4px}
.stat i{font-style:normal;color:var(--mut);font-size:11.5px;text-transform:uppercase;letter-spacing:.5px}
.big{width:100%;border:0;border-radius:15px;padding:17px;font-size:17px;font-weight:640;cursor:pointer;color:#16161a;background:var(--acc);margin-top:4px}
.big.stop{background:var(--bad);color:#fff}
.row{display:flex;gap:8px;margin-top:8px}
.row button{flex:1;background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:12px;padding:12px;font-size:14px;cursor:pointer}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:13px 14px;margin:10px 0}
.card h2{font-size:13px;margin:0 0 10px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.6px}
.trip{display:flex;gap:12px;align-items:center;padding:11px 0;border-bottom:1px solid var(--bd);cursor:pointer}
.trip:last-child{border-bottom:0}
.trip .th{width:76px;height:42px;border-radius:8px;object-fit:cover;background:#000;flex:none}
.trip .tx{flex:1;min-width:0}
.trip .tx b{display:block;font-size:15px;font-weight:600}
.trip .tx span{color:var(--mut);font-size:12.5px}
.pill{background:var(--acc);color:#16161a;border-radius:99px;padding:2px 8px;font-size:11px;font-weight:600}
label{display:block;margin:11px 0 5px;font-size:13px;color:var(--mut)}
select,input{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:10px;padding:10px;font-size:15px;font-family:inherit}
.seg{display:flex;gap:6px}
.seg button{flex:1;background:var(--bg);color:var(--mut);border:1px solid var(--bd);border-radius:10px;padding:10px 6px;font-size:13.5px;cursor:pointer}
.seg button.on{background:var(--acc);color:#16161a;border-color:var(--acc);font-weight:600}
nav{position:fixed;left:0;right:0;bottom:0;display:flex;background:var(--card);border-top:1px solid var(--bd);padding-bottom:env(safe-area-inset-bottom)}
nav button{flex:1;background:none;border:0;color:var(--mut);padding:11px 4px 13px;font-size:11.5px;cursor:pointer;font-family:inherit}
nav button em{display:block;font-size:19px;font-style:normal;line-height:1.25}
nav button.on{color:var(--acc)}
#toast{position:fixed;left:50%;transform:translateX(-50%);bottom:88px;background:#2ecc71;color:#08210f;padding:11px 18px;border-radius:12px;font-size:14.5px;font-weight:600;opacity:0;transition:.25s;pointer-events:none;z-index:9;max-width:88vw;text-align:center}
#toast.bad{background:var(--bad);color:#fff}
#toast.on{opacity:1}
.muted{color:var(--mut);font-size:12.5px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:6px;margin-top:10px}
.grid img{width:100%;aspect-ratio:2/1;object-fit:cover;border-radius:7px;background:#000;cursor:pointer}
dialog{border:0;border-radius:16px;background:var(--card);color:var(--fg);padding:0;max-width:96vw;width:820px}
dialog::backdrop{background:rgba(0,0,0,.7)}
.dh{display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid var(--bd)}
.dh b{flex:1;font-size:16px}
.db{padding:14px 16px;max-height:74vh;overflow:auto}
.map{width:100%;border-radius:12px;background:var(--bg);border:1px solid var(--bd)}
</style></head><body>

<header>
  <h1 id="title">Roamcam Reis</h1>
  <div class="clock"><div id="clk">--:--:--</div><div id="dat" style="font-size:11px"></div></div>
  <button class="iconbtn" id="theme">◐</button>
</header>

<main>
  <section class="view on" id="v-reis">
    <div class="live" id="livebox"><span class="off" id="liveoff">beeld laden…</span>
      <div class="badge">
        <span id="bFix">GPS</span><span id="bSpd">0 km/u</span><span id="bRec" style="display:none" class="rec">● REIS</span>
      </div>
    </div>
    <div class="stats">
      <div class="stat"><b id="sKm">0,0</b><i>km</i></div>
      <div class="stat"><b id="sFoto">0</b><i>foto's</i></div>
      <div class="stat"><b id="sTijd">0:00</b><i>onderweg</i></div>
    </div>
    <button class="big" id="btnTrip">Reis starten</button>
    <div class="row">
      <button id="btnShot">Nu een foto</button>
      <button id="btnLive">Live aan/uit</button>
    </div>
    <p class="muted" id="hint" style="margin-top:12px"></p>
  </section>

  <section class="view" id="v-archief">
    <div class="card"><h2>Reizen</h2><div id="triplist"><p class="muted">laden…</p></div></div>
  </section>

  <section class="view" id="v-inst">
    <div class="card"><h2>Wanneer een foto</h2>
      <label>Manier</label>
      <div class="seg" id="segMode">
        <button data-v="auto">Automatisch</button>
        <button data-v="distance">Afstand</button>
        <button data-v="time">Tijd</button>
      </div>
      <p class="muted" id="modeHint" style="margin:8px 0 0"></p>
      <label>Om de hoeveel meter</label>
      <select id="cDist"><option>25</option><option>50</option><option>100</option><option>250</option><option>500</option></select>
      <label>Tijdinterval (seconden, gebruikt zonder GPS-fix)</label>
      <select id="cTime"><option>2</option><option>5</option><option>10</option><option>30</option><option>60</option></select>
      <label>Bij stilstand toch elke … seconden (0 = uit)</label>
      <select id="cStill"><option>0</option><option>30</option><option>60</option><option>300</option></select>
      <label><input type="checkbox" id="cAuto" style="width:auto;margin-right:7px">Zelf een reis beginnen zodra ik rijd</label>
      <button class="big" id="saveTrig" style="margin-top:14px">Opslaan</button>
    </div>
    <div class="card"><h2>Algemeen</h2>
      <label>Naam van deze camera</label><input id="cName">
      <label>Tijdzone</label>
      <select id="cTz"><option>Europe/Amsterdam</option><option>Europe/Rome</option><option>Europe/Paris</option><option>Europe/Madrid</option><option>Europe/Berlin</option><option>Europe/London</option><option>UTC</option></select>
      <label>Beeldjes per seconde in de film</label>
      <select id="cFilm"><option>6</option><option>8</option><option>12</option><option>15</option><option>24</option><option>30</option></select>
      <label>Live beeld (fps) — lager = minder wifi</label>
      <select id="cLive"><option>2</option><option>5</option><option>10</option></select>
      <label>Stop met opslaan onder … MB vrij</label>
      <select id="cFree"><option>500</option><option>1000</option><option>2000</option><option>5000</option></select>
      <button class="big" id="saveGen" style="margin-top:14px">Opslaan</button>
    </div>
    <div class="card"><h2>Systeem</h2>
      <div class="muted" id="sys">…</div>
      <div class="row"><button onclick="location.href='/log'">Logboek</button></div>
    </div>
  </section>
</main>

<nav>
  <button class="on" data-v="reis"><em>◉</em>Reis</button>
  <button data-v="archief"><em>▤</em>Archief</button>
  <button data-v="inst"><em>⚙</em>Instellingen</button>
</nav>

<div id="toast"></div>

<dialog id="dlg"><div class="dh"><b id="dTitle">Reis</b><button class="iconbtn" onclick="dlg.close()">✕</button></div>
<div class="db" id="dBody"></div></dialog>

<script>
const $=i=>document.getElementById(i);
let LIVE=true, ST={}, CUR=null;

function toast(m,ok=true){const t=$('toast');t.textContent=m;t.className='on'+(ok?'':' bad');
  clearTimeout(t._h);t._h=setTimeout(()=>t.className='',2600);}
async function post(u,msg){try{const r=await(await fetch(u)).json();
  toast(r.msg||msg||'Klaar',r.ok!==false);return r;}catch(e){toast('Mislukt: '+e,false);return{};}}

// thema
if(localStorage.rcTheme==='light')document.documentElement.dataset.theme='light';
$('theme').onclick=()=>{const l=document.documentElement.dataset.theme==='light';
  if(l){delete document.documentElement.dataset.theme;localStorage.rcTheme='dark';}
  else{document.documentElement.dataset.theme='light';localStorage.rcTheme='light';}};

// tabs
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('on'));
  $('v-'+b.dataset.v).classList.add('on');
  if(b.dataset.v==='archief')loadTrips();
});

// live beeld
function liveOn(){const b=$('livebox');
  if(b.querySelector('img'))return;
  const im=new Image();im.src='/live.mjpg?'+Date.now();
  im.onload=()=>{$('liveoff').style.display='none'};
  im.onerror=()=>{$('liveoff').textContent='geen beeld van de camera';im.remove();};
  b.insertBefore(im,b.firstChild);}
function liveOff(){const i=$('livebox').querySelector('img');if(i)i.remove();
  $('liveoff').style.display='';$('liveoff').textContent='live staat uit';}
$('btnLive').onclick=()=>{LIVE=!LIVE;LIVE?liveOn():liveOff();};

function hhmm(s){s=Math.max(0,s|0);const h=s/3600|0,m=(s%3600)/60|0;
  return h?h+':'+String(m).padStart(2,'0'):m+':'+String(s%60).padStart(2,'0');}

async function tick(){
 try{
  const d=await(await fetch('/status')).json();ST=d;
  $('clk').textContent=d.now;$('dat').textContent=d.date;$('title').textContent=d.name||'Roamcam Reis';
  const g=d.gps||{};
  $('bFix').textContent=g.ok?('fix · '+g.used+'/'+g.seen+' sat'):(g.fix?('zoeken · '+(g.seen||0)+' sat'):'geen GPS');
  $('bFix').style.background=g.ok?'rgba(46,204,113,.85)':'rgba(0,0,0,.62)';
  $('bFix').style.color=g.ok?'#08210f':'#fff';
  $('bSpd').textContent=(g.kmh!=null?g.kmh:0)+' km/u';
  $('bRec').style.display=d.trip?'':'none';
  $('sKm').textContent=String(d.dist_km).replace('.',',');
  $('sFoto').textContent=d.frames;
  $('sTijd').textContent=hhmm(d.elapsed);
  const b=$('btnTrip');
  b.textContent=d.trip?'Reis stoppen':'Reis starten';
  b.className='big'+(d.trip?' stop':'');
  let h=[];
  if(d.trip){const m=d.cfg.mode;
    if(m==='time'||(m==='auto'&&!g.ok))h.push('legt nu elke '+d.cfg.time_s+' s vast');
    else h.push('nog '+Math.max(0,d.cfg.dist_m-d.since_last_m)+' m tot de volgende foto');}
  else if(d.cfg.auto_trip)h.push('begint vanzelf zodra je rijdt');
  if(!d.camera)h.push('⚠ geen beeld uit de camera-buffer');
  if(d.last_error)h.push('⚠ '+d.last_error);
  h.push((d.free_mb/1024).toFixed(1)+' GB vrij');
  $('hint').innerHTML=h.join(' · ');
  $('sys').innerHTML='Vrij: '+(d.free_mb/1024).toFixed(1)+' GB · temp '+(d.temp||'?')+' °C'+
    (g.lat?('<br>Positie: '+g.lat.toFixed(5)+', '+g.lon.toFixed(5)+' · '+g.alt+' m · ±'+g.acc+' m'):'');
 }catch(e){}
}

$('btnTrip').onclick=async()=>{
  if(ST.trip){if(!confirm('Reis stoppen en opslaan?'))return;await post('/trip/stop');}
  else await post('/trip/start');
  tick();};
$('btnShot').onclick=()=>post('/trip/shot').then(tick);

// ---- archief
async function loadTrips(){
  const d=await(await fetch('/trips')).json();
  const el=$('triplist');
  if(!d.trips.length){el.innerHTML='<p class="muted">Nog geen reizen. Druk op <b>Reis starten</b>, of laat hem vanzelf beginnen zodra je rijdt.</p>';return;}
  el.innerHTML=d.trips.map(t=>{
    const dt=new Date((t.start||0)*1000);
    const dur=t.end?hhmm(t.end-t.start):'loopt';
    return '<div class="trip" onclick="openTrip(\''+t.id+'\')">'+
      '<img class="th" src="/trip/frame?id='+t.id+'&i=1" onerror="this.style.visibility=\'hidden\'">'+
      '<div class="tx"><b>'+dt.toLocaleDateString('nl-NL',{weekday:'short',day:'numeric',month:'short'})+
      ' '+dt.toLocaleTimeString('nl-NL',{hour:'2-digit',minute:'2-digit'})+
      (t.running?' <span class="pill">loopt</span>':'')+'</b>'+
      '<span>'+((t.dist_m||0)/1000).toFixed(1).replace('.',',')+' km · '+(t.frames||0)+' foto\'s · '+
      dur+' · '+((t.bytes||0)/1048576).toFixed(0)+' MB'+(t.film?' · 🎬':'')+'</span></div></div>';
  }).join('');
}

async function openTrip(id){
  CUR=id;const d=await(await fetch('/trips')).json();
  const t=d.trips.find(x=>x.id===id)||{};
  const dt=new Date((t.start||0)*1000);
  $('dTitle').textContent=dt.toLocaleDateString('nl-NL',{weekday:'long',day:'numeric',month:'long'})+
    ' '+dt.toLocaleTimeString('nl-NL',{hour:'2-digit',minute:'2-digit'});
  const n=t.frames||0;
  let g='';const step=Math.max(1,Math.ceil(n/48));
  for(let i=1;i<=n;i+=step)g+='<img src="/trip/frame?id='+id+'&i='+i+'" onclick="window.open(this.src)">';
  $('dBody').innerHTML=
    '<img class="map" src="/trip/map.svg?id='+id+'">'+
    '<p class="muted">'+((t.dist_m||0)/1000).toFixed(1).replace('.',',')+' km · '+n+' foto\'s · '+
      ((t.bytes||0)/1048576).toFixed(0)+' MB</p>'+
    '<div class="row"><button onclick="mkFilm()">🎬 Film maken</button>'+
    (t.film?'<button onclick="location.href=\'/trip/film.mp4?id='+id+'\'">Film downloaden</button>':'')+
    '<button onclick="location.href=\'/trip/gpx?id='+id+'\'">GPX</button></div>'+
    '<div class="row"><button onclick="delTrip()" style="color:var(--bad)">Verwijderen</button></div>'+
    '<div id="filmst" class="muted" style="margin-top:8px"></div>'+
    '<div class="grid">'+g+'</div>';
  dlg.showModal();
}
async function mkFilm(){await post('/trip/film?id='+CUR);pollFilm();}
async function pollFilm(){
  const r=await(await fetch('/trip/film_status?id='+CUR)).json();
  const e=$('filmst');if(!e)return;
  if(r.state==='bezig'){e.textContent='film wordt gemaakt… (dit duurt even)';setTimeout(pollFilm,4000);}
  else if(r.state){e.textContent='film: '+r.state;if(r.ready)setTimeout(()=>openTrip(CUR),600);}
}
async function delTrip(){if(!confirm('Deze reis met alle foto\'s verwijderen?'))return;
  await post('/trip/delete?id='+CUR);dlg.close();loadTrips();}

// ---- instellingen
function fillCfg(c){
  document.querySelectorAll('#segMode button').forEach(b=>b.classList.toggle('on',b.dataset.v===c.mode));
  $('modeHint').textContent = c.mode==='auto'
    ? 'Op afstand zolang er GPS is, en automatisch op tijd zodra de fix wegvalt.'
    : c.mode==='distance' ? 'Alleen op afstand. Zonder GPS-fix wordt er niets vastgelegd.'
    : 'Altijd op tijd, ook als je stilstaat.';
  $('cDist').value=c.dist_m;$('cTime').value=c.time_s;$('cStill').value=c.still_time_s;
  $('cAuto').checked=!!c.auto_trip;$('cName').value=c.name||'';$('cTz').value=c.tz;
  $('cFilm').value=c.film_fps;$('cLive').value=c.live_fps;$('cFree').value=c.keep_free_mb;
}
document.querySelectorAll('#segMode button').forEach(b=>b.onclick=()=>{
  ST.cfg.mode=b.dataset.v;fillCfg(ST.cfg);});
$('saveTrig').onclick=async()=>{
  const p=new URLSearchParams({mode:ST.cfg.mode,dist_m:$('cDist').value,time_s:$('cTime').value,
    still_time_s:$('cStill').value,auto_trip:$('cAuto').checked?'1':'0'});
  await post('/set?'+p,'Opgeslagen');tick();};
$('saveGen').onclick=async()=>{
  const p=new URLSearchParams({name:$('cName').value,tz:$('cTz').value,film_fps:$('cFilm').value,
    live_fps:$('cLive').value,keep_free_mb:$('cFree').value});
  await post('/set?'+p,'Opgeslagen');tick();};

let cfgDone=false;
setInterval(async()=>{await tick();if(!cfgDone&&ST.cfg){fillCfg(ST.cfg);cfgDone=true;}},1000);
tick().then(()=>{if(ST.cfg)fillCfg(ST.cfg);cfgDone=true;});
liveOn();
</script></body></html>
"""


# ---------------------------------------------------------------- start

if __name__ == "__main__":
    os.makedirs(TRIPS, exist_ok=True)
    clock_restore()
    load_cfg()
    apply_tz()
    # Klok eerst gelijkzetten en pas daarna de lus starten: zo maakt de sprong van 2022 naar nu
    # geen rommel in de reisnamen en in de logregels. Wachten tot de datalogger echt schrijft,
    # anders zetten we de klok met het tijdstempel van vóór de vorige keer uitzetten.
    for _ in range(15):
        g = gps_is_live()
        if g:
            if g.get("time_resolved"):
                clock_from_gps(g)
            break
        time.sleep(2)
    else:
        log("datalogger schrijft nog geen verse GPS -- klok blijft op de bewaarde tijd")
    apply_tz()
    try:
        up = float(open("/proc/uptime").read().split()[0])
    except Exception:
        up = -1
    log("Roamcam Reis start -- %.0f s na het aanzetten, camera %s, %d MB vrij" %
        (up, "ok" if camera_available() else "GEEN BEELD", free_mb()))
    threading.Thread(target=travel_loop, daemon=True).start()
    srv = Srv(("0.0.0.0", PORT), H)
    log("webserver op poort %d" % PORT)
    srv.serve_forever()
