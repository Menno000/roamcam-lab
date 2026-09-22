#!/usr/bin/env python3
"""roamcam-lab rol 'basis': LoRa-basisstation op de SX1262 van een Hivemapper HDC.

Eén programma voor beide kanten van de link (rol 'basis' thuis, rol 'mobiel' in de auto).
Webinterface + JSON-API op poort 8080. Alleen Python-stdlib.

Alles is zelf te beheren: frequentie, SF, bandbreedte, zendvermogen, syncword, en de
duty-cycle-limiet. De wettelijke EU-grenzen per subband worden getoond en standaard
gehandhaafd; handhaving is per instelling uit te zetten (eigen verantwoordelijkheid).
"""
import collections
import hashlib
import hmac
import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sx1262 import SX1262, airtime_ms, BW_CODE  # noqa: E402
from wifi_home import WifiHome  # noqa: E402
from gps import GPS  # noqa: E402
from leds import Leds, FUNCTIONS as LED_FUNCTIONS  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, "config.json")
DEVICES_FILE = os.path.join(BASE, "apparaten.json")
LOG_FILE = os.path.join(BASE, "pakketten.jsonl")
RANGE_FILE = os.path.join(BASE, "bereik.jsonl")
PORT = 8080

DEFAULTS = {
    "node_id": 1,                 # 1 = basis, 2.. = mobiele units
    "node_name": "basis",
    "freq_mhz": 869.525,          # g3-subband: 500 mW ERP en 10 % toegestaan
    "sf": 9, "bw": 125, "cr": 1, "preamble": 16,
    "power_dbm": 14,
    "sync": 0x12,                 # privé-syncword: LoRaWAN (0x34) en Meshtastic (0x2B) horen ons niet
    "crc": True, "invert_iq": False,
    "net_key": "",                # leeg = onversleuteld; anders gedeelde sleutel (tekst)
    "enforce_duty": True,         # duty-cycle-limiet handhaven
    "duty_pct": None,             # None = wettelijke waarde van de subband
    "enforce_power": False,       # vermogensgrens handhaven (standaard alleen waarschuwen)
    "beacon_s": 0,                # 0 = geen baken; anders elke N s een statusbaken
    "range_test": False,          # bereiktest: elke range_interval_s een ping met GPS-positie
    "range_interval_s": 10,
    "range_dst": 1,
    "led_fns": ["lora", "link", "systeem"],   # functie per LED (zie leds.FUNCTIONS)
    "led_brightness": 100,        # procent
    "led_night": True,            # 22-07 u Nederlandse tijd gedimd tot 20 %
    "echo_ping": True,            # automatisch antwoorden op ping
    "raw_log": True,              # ook vreemde pakketten (andere netwerken) loggen
}

# EU 863-870 MHz, ERC-aanbeveling 70-03 annex 1 / NL frequentieplan (h1.x-banden).
# (van, tot, max ERP dBm, duty %)
EU_BANDS = [
    (863.0, 865.0, 14, 0.1),
    (865.0, 868.0, 14, 1.0),
    (868.0, 868.6, 14, 1.0),
    (868.7, 869.2, 14, 0.1),
    (869.4, 869.65, 27, 10.0),
    (869.7, 870.0, 14, 1.0),
]

PRESETS = {
    "eigen-lang": dict(freq_mhz=869.525, sf=12, bw=125, sync=0x12, invert_iq=False, preamble=16),
    "eigen-standaard": dict(freq_mhz=869.525, sf=9, bw=125, sync=0x12, invert_iq=False, preamble=16),
    "eigen-snel": dict(freq_mhz=869.525, sf=7, bw=250, sync=0x12, invert_iq=False, preamble=16),
    "meshtastic-longfast": dict(freq_mhz=869.525, sf=11, bw=250, sync=0x2B, invert_iq=False, preamble=16),
    "meshtastic-mediumfast": dict(freq_mhz=869.525, sf=9, bw=250, sync=0x2B, invert_iq=False, preamble=16),
    "lorawan-868.1-sf7": dict(freq_mhz=868.1, sf=7, bw=125, sync=0x34, invert_iq=False, preamble=8),
    "lorawan-rx2-sf9": dict(freq_mhz=869.525, sf=9, bw=125, sync=0x34, invert_iq=True, preamble=8),
}
# Scanner (CAD-hoppen). "every" = om de hoeveel rondes dit doel aan de beurt is: snelle SF's (korte
# pakketten) vaak, trage SF's (lange pakketten, dure CAD) minder vaak.
LW_FREQS = (868.1, 868.3, 868.5, 867.1, 867.3, 867.5, 867.7, 867.9)
SCAN_PLAN = (
    [dict(net="LoRaWAN", f=f, sf=sf, bw=125, sync=0x34, inv=False, every=1) for f in LW_FREQS for sf in (7, 8, 9)]
    + [dict(net="LoRaWAN", f=f, sf=sf, bw=125, sync=0x34, inv=False, every=3) for f in LW_FREQS for sf in (10, 11, 12)]
    + [dict(net="LoRaWAN-downlink", f=869.525, sf=9, bw=125, sync=0x34, inv=True, every=1),
       dict(net="LoRaWAN-downlink", f=869.525, sf=12, bw=125, sync=0x34, inv=True, every=3),
       dict(net="Meshtastic", f=869.525, sf=11, bw=250, sync=0x2B, inv=False, every=1),
       dict(net="Meshtastic", f=869.525, sf=9, bw=250, sync=0x2B, inv=False, every=2),
       dict(net="Meshtastic", f=869.525, sf=7, bw=250, sync=0x2B, inv=False, every=2),
       dict(net="Meshtastic", f=869.525, sf=12, bw=125, sync=0x2B, inv=False, every=3)]
)


def lorawan_net(devaddr):
    """Operator uit het DevAddr-voorvoegsel (NetID). Alleen de zekere gevallen, rest als NwkID."""
    top = devaddr >> 25
    if devaddr >> 25 == 0x13:
        return "TTN (NetID 000013)"
    if devaddr >> 25 == 0x24:
        return "Helium (NetID 000024)"
    if devaddr >> 31 == 0:
        return "NwkID 0x%02X" % top
    ones = format(devaddr >> 24, "08b").find("0")      # type = aantal leidende enen
    return "NetID type %d" % (ones if ones >= 0 else 8)

# --- eigen protocol --------------------------------------------------------
MAGIC = b"RL"
T_BEACON, T_TEXT, T_PING, T_PONG = 1, 2, 3, 4
T_NAMES = {T_BEACON: "baken", T_TEXT: "tekst", T_PING: "ping", T_PONG: "pong"}
F_ENC = 0x80
BROADCAST = 0xFF


def _keystream(key, nonce, n):
    out = bytearray()
    ctr = 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + struct.pack(">I", ctr)).digest()
        ctr += 1
    return bytes(out[:n])


def frame_build(ptype, src, dst, seq, payload, key=b""):
    """[RL][type|enc][src][dst][seq16] payload [tag4]. Met sleutel: SHA256-CTR + HMAC-tag."""
    flags = ptype | (F_ENC if key else 0)
    hdr = MAGIC + struct.pack(">BBBH", flags, src, dst, seq & 0xFFFF)
    if key:
        k = hashlib.sha256(key).digest()
        body = bytes(a ^ b for a, b in zip(payload, _keystream(k, hdr, len(payload))))
        tag = hmac.new(k, hdr + body, hashlib.sha256).digest()[:4]
        return hdr + body + tag
    return hdr + payload


def frame_parse(data, key=b""):
    if len(data) < 7 or data[:2] != MAGIC:
        return None
    flags, src, dst, seq = struct.unpack(">BBBH", data[2:7])
    body = data[7:]
    enc = bool(flags & F_ENC)
    out = {"type": flags & 0x7F, "src": src, "dst": dst, "seq": seq, "enc": enc, "auth": None}
    if enc:
        if not key:
            out.update(payload=None, auth=False, note="versleuteld, geen sleutel ingesteld")
            return out
        k = hashlib.sha256(key).digest()
        body, tag = body[:-4], body[-4:]
        ok = hmac.compare_digest(tag, hmac.new(k, data[:7] + body, hashlib.sha256).digest()[:4])
        out["auth"] = ok
        if not ok:
            out.update(payload=None, note="tag klopt niet: andere sleutel of beschadigd")
            return out
        body = bytes(a ^ b for a, b in zip(body, _keystream(k, data[:7], len(body))))
    out["payload"] = body
    return out


def band_for(freq, bw_khz):
    lo, hi = freq - bw_khz / 2000, freq + bw_khz / 2000
    for b in EU_BANDS:
        if lo >= b[0] and hi <= b[1]:
            return {"from": b[0], "to": b[1], "max_dbm": b[2], "duty_pct": b[3]}
    return None


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return "ERR %s" % e


class Station:
    def __init__(self):
        self.cfg = dict(DEFAULTS)
        try:
            with open(CONFIG_FILE) as f:
                self.cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.lock = threading.RLock()
        self.cmds = queue.Queue()
        self.packets = collections.deque(maxlen=500)
        self.pkt_id = 0
        self.tx_hist = collections.deque()          # (monotonic, airtime_ms)
        self.stats = collections.Counter()
        self.peers = {}                             # node_id -> laatste info
        self.seq = 0
        self.radio = None
        self.radio_info = {}
        self.radio_error = None
        self.mode = self.cfg.get("mode", "link")    # link | sniff (sniff = CAD-scanner)
        self.sniff_idx = 0
        self.last_scan = None
        self.pending_pings = {}                     # seq -> monotonic
        self.started = time.monotonic()
        self.last_beacon = 0
        self.devices = {}
        try:
            with open(DEVICES_FILE) as f:
                self.devices = json.load(f)
        except (OSError, ValueError):
            pass
        self.devices_saved = time.monotonic()
        self.chan = {}                              # "f/SFsf/bw" -> {cad, hits, pkts}
        self.scan_pass = 0
        self.scan_pos = 0
        self.scan_stats = {"cad": 0, "hits": 0, "pass_ms": None}
        self._pass_t0 = time.monotonic()
        self.range_log = collections.deque(maxlen=20000)
        self.range_id = 0
        self.range_pending = {}                     # seq -> record (mobiele kant)
        self.last_range = 0
        try:
            with open(RANGE_FILE) as f:
                for ln in f:
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    self.range_id = max(self.range_id, r.get("id", 0))
                    self.range_log.append(r)
        except OSError:
            pass

    # --- instellingen ------------------------------------------------
    def save(self):
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.cfg, f, indent=1)
        os.replace(tmp, CONFIG_FILE)

    def key(self):
        return self.cfg.get("net_key", "").encode()

    def band(self):
        return band_for(self.cfg["freq_mhz"], self.cfg["bw"])

    def duty_limit_pct(self):
        if self.cfg.get("duty_pct") is not None:
            return float(self.cfg["duty_pct"])
        b = self.band()
        return b["duty_pct"] if b else 0.1

    def airtime_last_hour(self):
        now = time.monotonic()
        while self.tx_hist and now - self.tx_hist[0][0] > 3600:
            self.tx_hist.popleft()
        return sum(a for _, a in self.tx_hist)

    def radio_params(self):
        c = self.cfg
        return dict(freq_mhz=c["freq_mhz"], sf=c["sf"], bw=c["bw"], cr=c["cr"], power_dbm=c["power_dbm"],
                    sync=c["sync"], preamble=c["preamble"], crc=c["crc"], invert_iq=c["invert_iq"])

    # --- log -----------------------------------------------------------
    def add_packet(self, rec):
        with self.lock:
            self.pkt_id += 1
            rec["id"] = self.pkt_id
            rec["time"] = time.time()
            self.packets.append(rec)
        try:
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps(rec) + "\n")
            if os.path.getsize(LOG_FILE) > 20_000_000:
                os.replace(LOG_FILE, LOG_FILE + ".1")
        except OSError:
            pass

    # --- radio-thread ---------------------------------------------------
    def run_radio(self):
        while True:
            try:
                self.radio = SX1262()
                self.radio_info = self.radio.begin()
                self.radio.configure(**self.radio_params())
                self.radio.rx_continuous()
                self.radio_error = None
                self._loop()
            except Exception as e:  # noqa: BLE001
                self.radio_error = "%s: %s" % (type(e).__name__, e)
                try:
                    self.radio.close()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(5)

    def _loop(self):
        r = self.radio
        while True:
            try:
                fn, args, reply = self.cmds.get_nowait()
                try:
                    reply.put((True, fn(*args)))
                except Exception as e:  # noqa: BLE001
                    reply.put((False, str(e)))
                if self.mode == "link":
                    r.rx_continuous()
                continue
            except queue.Empty:
                pass
            now = time.monotonic()
            if now - self.devices_saved > 30:
                self._save_devices()
            if self.mode == "sniff":
                self._scan_step()
                continue
            p = r.read_packet()
            if p:
                self._on_packet(p)
                r.rx_continuous()
            if self.mode == "link":
                self._range_tick(now)
            if (self.mode == "link" and self.cfg.get("beacon_s") and
                    now - self.last_beacon > float(self.cfg["beacon_s"])):
                self.last_beacon = now
                try:
                    self._send(T_BEACON, BROADCAST, self._beacon_payload(), auto=True)
                except Exception as e:  # noqa: BLE001
                    self.stats["baken_overgeslagen"] += 1
                    self.last_tx_error = str(e)
                r.rx_continuous()
            time.sleep(0.005)

    # --- scanner ----------------------------------------------------------
    def _scan_targets(self):
        c = self.cfg
        own = dict(net="eigen", f=c["freq_mhz"], sf=c["sf"], bw=c["bw"], sync=c["sync"], inv=c["invert_iq"], every=1)
        return SCAN_PLAN + [own]

    def _scan_step(self):
        """Eén doel: afstemmen, CAD, en bij een treffer het pakket ontvangen."""
        r = self.radio
        targets = self._scan_targets()
        if self.scan_pos >= len(targets):
            self.scan_pos = 0
            self.scan_pass += 1
            now = time.monotonic()
            self.scan_stats["pass_ms"] = round((now - self._pass_t0) * 1000)
            self._pass_t0 = now
        t = targets[self.scan_pos]
        self.scan_pos += 1
        if self.scan_pass % t["every"]:
            return
        ckey = "%.3f/SF%d/%g" % (t["f"], t["sf"], t["bw"])
        ch = self.chan.setdefault(ckey, {"net": t["net"], "f": t["f"], "sf": t["sf"], "bw": t["bw"],
                                         "cad": 0, "hits": 0, "pkts": 0, "last": None})
        r.standby()
        r.retune(t["f"], t["sf"], t["bw"], t["sync"], t["inv"])
        rx_ms = min(4000, airtime_ms(64, t["sf"], t["bw"], preamble=8) + 100)
        ch["cad"] += 1
        self.scan_stats["cad"] += 1
        self.sniff_cur = "%s %.3f MHz SF%d BW%g" % (t["net"], t["f"], t["sf"], t["bw"])
        # Eerste CAD alleen kijken. Losse CAD-treffers zijn voor ~1 % ruis; een echte uitzending
        # duurt lang genoeg voor een tweede CAD, dus pas bij twee treffers op rij telt het.
        if not self._cad_once(0):
            return
        ch["suspect"] = ch.get("suspect", 0) + 1
        if not self._cad_once(rx_ms):
            self.scan_stats["vals_alarm"] = self.scan_stats.get("vals_alarm", 0) + 1
            return
        # iets gezien: de chip staat nu in RX
        rssi_cad = r.rssi_now()
        ch["hits"] += 1
        ch["last"] = time.time()
        self.scan_stats["hits"] += 1
        deadline = time.monotonic() + rx_ms / 1000 + 0.2
        while time.monotonic() < deadline:
            flags = r.irq()
            if flags & 0x0262:                       # RX_DONE | HEADER_ERR | CRC_ERR | TIMEOUT
                break
            time.sleep(0.002)
        p = r.read_packet() if flags & 0x0062 else None
        if p and "data" in p and p.get("crc_ok"):
            ch["pkts"] += 1
            self._on_packet(p, net=t["net"])
        else:
            why = ("CRC-fout" if p and p.get("crc_ok") is False else "headerfout" if p else
                   "geen geldig pakket (ander syncword, of alleen activiteit)")
            self._seen("signaal %.3f MHz SF%d" % (t["f"], t["sf"]), net=t["net"] + "?",
                       kind="LoRa-activiteit, niet gedecodeerd", rssi=rssi_cad, snr=None,
                       f=t["f"], sf=t["sf"], note=why)
            self.stats["scan_niet_gedecodeerd"] += 1
        r.clear_irq()

    def _cad_once(self, rx_ms):
        r = self.radio
        cad_ms = r.cad(rx_timeout_ms=rx_ms)
        deadline = time.monotonic() + cad_ms / 1000 * 2 + 0.02
        flags = 0
        while time.monotonic() < deadline:
            flags = r.irq()
            if flags & 0x0080:                       # CAD_DONE
                break
            time.sleep(0.0005)
        return bool(flags & 0x0100)                  # CAD_DETECTED

    def _seen(self, key, net, kind, rssi, snr, f, sf, note=None, extra=None):
        now = time.time()
        d = self.devices.get(key)
        if d is None:
            d = self.devices[key] = {"key": key, "net": net, "kind": kind, "first": now, "count": 0,
                                     "best_rssi": rssi, "freqs": [], "sfs": []}
        d.update(last=now, rssi=rssi, snr=snr, kind=kind, net=net)
        d["count"] += 1
        if rssi is not None and (d.get("best_rssi") is None or rssi > d["best_rssi"]):
            d["best_rssi"] = rssi
        if f not in d["freqs"]:
            d["freqs"].append(f)
        if sf not in d["sfs"]:
            d["sfs"].append(sf)
        if note:
            d["note"] = note
        if extra:
            d.update(extra)

    def _save_devices(self):
        self.devices_saved = time.monotonic()
        try:
            tmp = DEVICES_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.devices, f)
            os.replace(tmp, DEVICES_FILE)
        except OSError:
            pass

    def _register_foreign(self, data, cfg, p, net):
        LD.flash("vreemd")
        f, sf, rssi, snr = cfg.get("freq_mhz"), cfg.get("sf"), p.get("rssi"), p.get("snr")
        sw = cfg.get("sync")
        if sw == 0x34 and data:
            mtype = data[0] >> 5
            if mtype in (2, 4) and len(data) >= 8:
                addr = struct.unpack("<I", data[1:5])[0]
                self._seen("LoRaWAN %08X" % addr, lorawan_net(addr), "LoRaWAN-sensor (uplink)", rssi, snr, f, sf,
                           extra={"fcnt": struct.unpack("<H", data[6:8])[0],
                                  "fport": data[8 + (data[5] & 0x0F)] if len(data) > 9 + (data[5] & 0x0F) else None})
                return
            if mtype in (3, 5) and len(data) >= 5:
                addr = struct.unpack("<I", data[1:5])[0]
                self._seen("gateway → %08X" % addr, lorawan_net(addr), "LoRaWAN-gateway (downlink)", rssi, snr, f, sf)
                return
            if mtype == 0 and len(data) >= 17:
                deveui = data[9:17][::-1].hex().upper()
                self._seen("LoRaWAN DevEUI %s" % deveui, "LoRaWAN (aanmelden)", "LoRaWAN-sensor (join)", rssi, snr,
                           f, sf, extra={"join_eui": data[1:9][::-1].hex().upper()})
                return
            if mtype == 1:
                self._seen("gateway (join-accept) %.1f" % f, "LoRaWAN", "LoRaWAN-gateway (join-accept)", rssi, snr, f, sf)
                return
        if sw == 0x2B and len(data) >= 16:
            dest, sender = struct.unpack("<II", data[:8])
            self._seen("Meshtastic !%08x" % sender, "Meshtastic", "Meshtastic-node", rssi, snr, f, sf,
                       extra={"hop_limit": data[12] & 7, "channel_hash": "%02x" % data[13]})
            return
        self._seen("onbekend %.3f SF%d" % (f, sf), net or "?", "onbekend LoRa-pakket", rssi, snr, f, sf,
                   extra={"voorbeeld_hex": data[:24].hex()})

    # --- bereiktest -------------------------------------------------------
    @staticmethod
    def _pos_payload():
        f = GP.fix if GP.has_fix() else {}
        if not f:
            return b"G" + bytes(13)
        return b"G" + struct.pack("<iiHHB", int(round(f["lat"] * 1e7)), int(round(f["lon"] * 1e7)),
                                  min(65535, int(f["speed_kmh"] * 10)), min(65535, int(f["hacc_m"] * 10)),
                                  min(255, f["sats"]))

    @staticmethod
    def _pos_parse(pl):
        if not pl or len(pl) < 14 or pl[:1] != b"G":
            return None
        lat, lon, spd, hacc, sats = struct.unpack("<iiHHB", pl[1:14])
        if lat == 0 and lon == 0:
            return {"lat": None, "lon": None}
        return {"lat": lat / 1e7, "lon": lon / 1e7, "speed_kmh": spd / 10, "hacc_m": hacc / 10, "sats": sats}

    def _range_add(self, rec):
        self.range_id += 1
        rec["id"] = self.range_id
        self.range_log.append(rec)
        try:
            with open(RANGE_FILE, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    def _range_tick(self, now):
        c = self.cfg
        # verlopen pings: geen pong binnen de wachttijd = verloren
        for seq, rec in list(self.range_pending.items()):
            if now - rec["_t"] > max(3.0, 2.5 * rec["airtime_ms"] / 1000 + 1.5):
                del self.range_pending[seq]
                rec.pop("_t", None)
                rec["ok"] = False
                self._range_add(rec)
        if not c.get("range_test") or now - self.last_range < float(c.get("range_interval_s", 10)):
            return
        self.last_range = now
        pos = GP.fix if GP.has_fix() else {}
        rec = {"side": "mobiel", "time": time.time(), "lat": pos.get("lat"), "lon": pos.get("lon"),
               "speed_kmh": pos.get("speed_kmh"), "hacc_m": pos.get("hacc_m"), "sats": pos.get("sats"),
               "sf": c["sf"], "bw": c["bw"], "power_dbm": c["power_dbm"], "freq": c["freq_mhz"]}
        try:
            tx = self._send(T_PING, int(c.get("range_dst", 1)), self._pos_payload(), auto=True)
        except Exception as e:  # noqa: BLE001
            rec.update(ok=None, error=str(e))
            self._range_add(rec)
            return
        rec.update(seq=self.seq, airtime_ms=tx["airtime_ms"], _t=now)
        self.range_pending[self.seq] = rec
        self.radio.rx_continuous()

    def _beacon_payload(self):
        up = int(time.monotonic() - self.started)
        temp = sh("cat /sys/class/thermal/thermal_zone0/temp") or "0"
        return json.dumps({"n": self.cfg["node_name"], "up": up, "t": round(int(temp) / 1000, 1)},
                          separators=(",", ":")).encode()

    def _on_packet(self, p, net=None):
        LD.flash("rx")
        cfg = self.radio.cfg
        rec = {"dir": "rx", "rssi": p.get("rssi"), "snr": p.get("snr"), "crc_ok": p.get("crc_ok"),
               "freq_err_hz": p.get("freq_err_hz"),
               "freq": cfg.get("freq_mhz"), "sf": cfg.get("sf"), "bw": cfg.get("bw"),
               "sync": cfg.get("sync"), "mode": self.mode}
        if "error" in p:
            self.stats["rx_header_fout"] += 1
            rec["error"] = p["error"]
            if self.cfg.get("raw_log"):
                self.add_packet(rec)
            return
        data = p["data"]
        rec["len"] = len(data)
        rec["hex"] = data.hex()
        fr = frame_parse(data, self.key()) if p.get("crc_ok") else None
        if fr is None:
            self.stats["rx_vreemd" if p.get("crc_ok") else "rx_crc_fout"] += 1
            rec["kind"] = self._guess_foreign(data, cfg)
            if p.get("crc_ok"):
                self._register_foreign(data, cfg, p, net)
            if self.cfg.get("raw_log"):
                self.add_packet(rec)
            return
        self.stats["rx_eigen"] += 1
        self._seen("eigen node %d" % fr["src"], "eigen netwerk", "roamcam-node", p["rssi"], p["snr"],
                   cfg.get("freq_mhz"), cfg.get("sf"), extra={"auth": fr.get("auth")})
        pl = fr.pop("payload", None)
        rec.update(kind="eigen", frame=fr, type=T_NAMES.get(fr["type"], fr["type"]))
        if pl is not None:
            try:
                rec["text"] = pl.decode()
            except UnicodeDecodeError:
                rec["payload_hex"] = pl.hex()
        src = fr["src"]
        if src != self.cfg["node_id"]:
            peer = self.peers.setdefault(src, {"node_id": src, "rx": 0})
            peer.update(last=time.time(), rssi=p["rssi"], snr=p["snr"], auth=fr.get("auth"))
            peer["rx"] += 1
        for_me = fr["dst"] in (BROADCAST, self.cfg["node_id"])
        if fr["type"] == T_BEACON and pl:
            try:
                self.peers[src]["beacon"] = json.loads(pl)
            except (ValueError, KeyError):
                pass
        if fr["type"] == T_PING and for_me and pl and pl[:1] == b"G":
            pos = self._pos_parse(pl) or {}
            self._range_add({"side": "basis", "time": time.time(), "src": src, "seq": fr["seq"], "ok": True,
                             "rssi": p["rssi"], "snr": p["snr"], "freq": cfg.get("freq_mhz"), "sf": cfg.get("sf"),
                             "bw": cfg.get("bw"), **pos})
        if fr["type"] == T_PING and for_me and self.cfg.get("echo_ping") and fr.get("auth") is not False:
            info_out = {"rssi": p["rssi"], "snr": p["snr"], "seq": fr["seq"], "fe": p.get("freq_err_hz"),
                        "len": len(data)}
            if pl and pl[:1] == b"G" and GP.has_fix():       # eigen positie mee, voor de afstand
                info_out["b"] = [round(GP.fix["lat"], 5), round(GP.fix["lon"], 5)]
            reply = json.dumps(info_out, separators=(",", ":")).encode()
            try:
                time.sleep(0.05)
                self._send(T_PONG, src, reply, auto=True)
            except Exception as e:  # noqa: BLE001
                rec["note"] = "pong niet verzonden: %s" % e
        if fr["type"] == T_PONG and pl:
            try:
                info = json.loads(pl)
                t0 = self.pending_pings.pop(info.get("seq"), None)
                if t0:
                    rec["rtt_ms"] = round((time.monotonic() - t0) * 1000)
                rec["remote_rssi"] = info.get("rssi")
                rec["remote_snr"] = info.get("snr")
                rec["remote_fe"] = info.get("fe")
                rec["remote_len"] = info.get("len")
                self.peers.get(src, {}).update(their_rssi=info.get("rssi"), their_snr=info.get("snr"),
                                               rtt_ms=rec.get("rtt_ms"))
                rr = self.range_pending.pop(info.get("seq"), None)
                if rr:
                    rr.pop("_t", None)
                    rr.update(ok=True, rssi_there=info.get("rssi"), snr_there=info.get("snr"),
                              rssi_here=p["rssi"], snr_here=p["snr"], rtt_ms=rec.get("rtt_ms"))
                    if info.get("b"):
                        rr["base_lat"], rr["base_lon"] = info["b"]
                    self._range_add(rr)
            except ValueError:
                pass
        self.add_packet(rec)

    @staticmethod
    def _guess_foreign(data, cfg):
        sw = cfg.get("sync")
        if sw == 0x34 and data:
            mtype = data[0] >> 5
            names = {0: "LoRaWAN join-request", 1: "LoRaWAN join-accept", 2: "LoRaWAN uplink",
                     3: "LoRaWAN downlink", 4: "LoRaWAN uplink (confirmed)", 5: "LoRaWAN downlink (confirmed)",
                     6: "LoRaWAN rejoin", 7: "LoRaWAN proprietary"}
            kind = names.get(mtype, "LoRaWAN?")
            if mtype in (2, 3, 4, 5) and len(data) >= 5:
                kind += " DevAddr %08X" % struct.unpack("<I", data[1:5])[0]
            if mtype == 0 and len(data) >= 17:
                kind += " JoinEUI %s DevEUI %s" % (data[1:9][::-1].hex(), data[9:17][::-1].hex())
            return kind
        if sw == 0x2B and len(data) >= 16:
            dest, sender, pid = struct.unpack("<III", data[:12])
            return "Meshtastic van !%08x naar %s, hop %d, kanaalhash %02x" % (
                sender, "iedereen" if dest == 0xFFFFFFFF else "!%08x" % dest, data[12] & 7, data[13])
        return "onbekend"

    # --- zenden -------------------------------------------------------
    def check_tx(self, n_bytes):
        c = self.cfg
        at = airtime_ms(n_bytes, c["sf"], c["bw"], c["cr"], c["preamble"], c["crc"])
        limit_ms = self.duty_limit_pct() / 100 * 3600_000
        used = self.airtime_last_hour()
        b = self.band()
        if c.get("enforce_duty", True) and used + at > limit_ms:
            wait = 0
            acc = used + at - limit_ms
            for t, a in self.tx_hist:
                acc -= a
                if acc <= 0:
                    wait = 3600 - (time.monotonic() - t)
                    break
            raise RuntimeError("duty-cycle-limiet %.2f %% bereikt (%.1f s van %.1f s gebruikt); nog %d s wachten"
                               % (self.duty_limit_pct(), used / 1000, limit_ms / 1000, max(wait, 1)))
        if c.get("enforce_power") and b and c["power_dbm"] > b["max_dbm"]:
            raise RuntimeError("vermogen %d dBm boven de %d dBm van deze subband" % (c["power_dbm"], b["max_dbm"]))
        if c.get("enforce_power") and not b:
            raise RuntimeError("frequentie ligt niet (helemaal) in een vergunningsvrije subband")
        return at

    def _send(self, ptype, dst, payload, auto=False):
        self.seq = (self.seq + 1) & 0xFFFF
        data = frame_build(ptype, self.cfg["node_id"], dst, self.seq, payload, self.key())
        return self._send_raw(data, ptype=ptype, dst=dst, auto=auto, seq=self.seq,
                              text=payload.decode(errors="replace"))

    def _send_raw(self, data, **info):
        if self.mode == "sniff":
            raise RuntimeError("zenden kan niet in sniffermodus")
        at = self.check_tx(len(data))
        if ptype_is_ping(info):
            self.pending_pings[info["seq"]] = time.monotonic()
        LD.flash("tx")
        real = self.radio.send(data)
        self.tx_hist.append((time.monotonic(), at))
        self.stats["tx"] += 1
        c = self.cfg
        rec = {"dir": "tx", "len": len(data), "hex": data.hex(), "airtime_ms": round(at, 1),
               "measured_ms": round(real, 1), "freq": c["freq_mhz"], "sf": c["sf"], "bw": c["bw"],
               "power_dbm": c["power_dbm"], "type": T_NAMES.get(info.get("ptype"), "rauw"),
               "dst": info.get("dst"), "auto": info.get("auto", False), "text": info.get("text")}
        self.add_packet(rec)
        return rec

    # --- vanuit de webserver (thread-veilig via de radio-thread) ----------
    def call(self, fn, *args, timeout=30):
        reply = queue.Queue()
        self.cmds.put((fn, args, reply))
        ok, val = reply.get(timeout=timeout)
        if not ok:
            raise RuntimeError(val)
        return val

    def apply_config(self, new):
        allowed = set(DEFAULTS) | {"sniff_dwell_s"}
        clean = {k: v for k, v in new.items() if k in allowed}
        # De sleutel gaat nooit terug naar de browser, dus een leeg veld betekent "ongewijzigd".
        if clean.get("net_key") in ("", None):
            clean.pop("net_key", None)
        if new.get("net_key_clear"):
            clean["net_key"] = ""
        if "bw" in clean and float(clean["bw"]) not in BW_CODE:
            raise ValueError("bandbreedte moet een van %s zijn" % sorted(BW_CODE))
        for k in ("sf", "cr", "preamble", "power_dbm", "node_id", "sync"):
            if k in clean:
                clean[k] = int(clean[k], 0) if isinstance(clean[k], str) else int(clean[k])
        if "bw" in clean:
            bw = float(clean["bw"])
            clean["bw"] = int(bw) if bw.is_integer() else bw
        if "freq_mhz" in clean:
            clean["freq_mhz"] = float(clean["freq_mhz"])
        if not 5 <= clean.get("sf", self.cfg["sf"]) <= 12:
            raise ValueError("SF moet 5..12 zijn")
        if not -9 <= clean.get("power_dbm", self.cfg["power_dbm"]) <= 22:
            raise ValueError("vermogen moet -9..22 dBm zijn (grens van de SX1262)")
        if not 150 <= clean.get("freq_mhz", self.cfg["freq_mhz"]) <= 960:
            raise ValueError("frequentie buiten het bereik van de SX1262")
        if "led_fns" in clean:
            fns = list(clean["led_fns"])[:3]
            if len(fns) != 3 or any(f not in LED_FUNCTIONS for f in fns):
                raise ValueError("led_fns: drie functies uit %s" % sorted(LED_FUNCTIONS))
            clean["led_fns"] = fns
        if "led_brightness" in clean:
            clean["led_brightness"] = max(0, min(100, int(clean["led_brightness"])))
        if clean.get("duty_pct") in ("", None) and "duty_pct" in clean:
            clean["duty_pct"] = None
        elif "duty_pct" in clean:
            clean["duty_pct"] = float(clean["duty_pct"])

        def do():
            self.cfg.update(clean)
            self.save()
            if self.mode == "link":
                self.radio.standby()
                self.radio.configure(**self.radio_params())
            return public_cfg(self.cfg)
        return self.call(do)

    def set_mode(self, mode):
        def do():
            self.mode = mode
            self.cfg["mode"] = mode
            self.save()
            self.radio.standby()
            if mode == "link":
                self.radio.configure(**self.radio_params())
                self.radio.rx_continuous()
            return mode
        return self.call(do)

    def scan(self, start, stop, step):
        freqs = []
        f = start
        while f <= stop + 1e-9 and len(freqs) < 400:
            freqs.append(round(f, 4))
            f += step

        def do():
            res = self.radio.rssi_scan(freqs)
            self.last_scan = {"time": time.time(), "points": res}
            return self.last_scan
        return self.call(do, timeout=120)

    def ping(self, dst, pad=0):
        pad = max(0, min(int(pad), 240))
        return self.call(lambda: self._send(T_PING, dst, b"p" + os.urandom(pad)))

    def send_text(self, text, dst):
        return self.call(lambda: self._send(T_TEXT, dst, text.encode()[:230]))

    def send_raw_hex(self, hexstr):
        data = bytes.fromhex(hexstr)
        return self.call(lambda: self._send_raw(data))

    def state(self, since=0):
        c = self.cfg
        used = self.airtime_last_hour()
        limit = self.duty_limit_pct()
        with self.lock:
            pk = [p for p in self.packets if p["id"] > since]
        at1 = airtime_ms(20, c["sf"], c["bw"], c["cr"], c["preamble"], c["crc"])
        return {
            "cfg": public_cfg(c), "band": self.band(), "duty_limit_pct": limit,
            "airtime_hour_ms": round(used, 1), "airtime_limit_ms": limit / 100 * 3600_000,
            "airtime_20b_ms": round(at1, 1), "mode": self.mode,
            "sniff_cur": getattr(self, "sniff_cur", None),
            "radio": {"info": self.radio_info, "error": self.radio_error,
                      "rssi_now": self._rssi_safe()},
            "stats": dict(self.stats), "peers": list(self.peers.values()),
            "packets": pk, "presets": PRESETS, "uptime_s": int(time.monotonic() - self.started),
            "hostname": sh("hostname"), "time": time.time(), "last_scan": self.last_scan,
            "wifi": WH.public(),
            "led_functions": LED_FUNCTIONS,
            "gps": dict(GP.fix, ok=GP.has_fix(), age_s=GP.age_s(), error=GP.error, frames=GP.frames),
            "devices": sorted(self.devices.values(), key=lambda d: -d["last"]),
            "channels": sorted(self.chan.values(), key=lambda c: (c["net"], c["f"], c["sf"])),
            "scan_stats": dict(self.scan_stats, targets=len(self._scan_targets()), pass_nr=self.scan_pass),
        }

    def _rssi_safe(self):
        try:
            return self.radio.rssi_now() if self.radio and self.mode == "link" else None
        except Exception:  # noqa: BLE001
            return None


def public_cfg(cfg):
    """Instellingen voor buiten de unit: zonder de netwerksleutel zelf."""
    out = dict(cfg)
    out["has_key"] = bool(out.pop("net_key", ""))
    return out


def ptype_is_ping(info):
    return info.get("ptype") == T_PING


GP = GPS()
ST = Station()
WH = WifiHome(os.path.join(BASE, "wifi.json"))
LD = Leds(ST, WH, GP)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(BASE, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/state"):
            since = 0
            if "since=" in self.path:
                since = int(self.path.split("since=")[1].split("&")[0] or 0)
            self._json(ST.state(since))
        elif self.path.startswith("/api/range"):
            since = int(self.path.split("since=")[1].split("&")[0] or 0) if "since=" in self.path else 0
            self._json([r for r in ST.range_log if r["id"] > since])
        elif self.path == "/api/log":
            try:
                with open(LOG_FILE, "rb") as f:
                    body = f.read()
            except OSError:
                body = b""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Disposition", "attachment; filename=pakketten.jsonl")
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json({"error": "niet gevonden"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
            p = self.path
            if p == "/api/config":
                res = ST.apply_config(req)
            elif p == "/api/preset":
                res = ST.apply_config(PRESETS[req["name"]])
            elif p == "/api/mode":
                res = ST.set_mode("sniff" if req.get("mode") == "sniff" else "link")
            elif p == "/api/send":
                dst = int(req.get("dst", BROADCAST))
                res = ST.send_raw_hex(req["hex"]) if req.get("hex") else ST.send_text(req.get("text", ""), dst)
            elif p == "/api/ping":
                res = ST.ping(int(req.get("dst", BROADCAST)), req.get("pad", 0))
            elif p == "/api/scan":
                res = ST.scan(float(req.get("start", 863)), float(req.get("stop", 870)),
                              float(req.get("step", 0.05)))
            elif p == "/api/time":
                # Geen RTC en thuis vaak geen NTP: de browser geeft de klok door.
                ms = int(req["epoch_ms"])
                if abs(ms / 1000 - time.time()) > 30:
                    sh("date -u -s @%d" % (ms // 1000))
                res = {"time": time.time()}
            elif p == "/api/wifi":
                res = WH.set_network(req.get("ssid", ""), req.get("password", ""), req.get("enabled", True))
            elif p == "/api/ap_password":
                pw = req.get("password", "")
                # boot.sh zet dit met sed in hostapd.conf: / \ & en regeleinden zouden dat breken
                if not 8 <= len(pw) <= 63 or any(c in pw for c in "\n\r/\\&"):
                    raise ValueError("8 tot 63 tekens, zonder / \\ & of regeleinde")
                path = os.path.join(BASE, "ap_pass")
                with open(path, "w") as f:
                    f.write(pw)
                os.chmod(path, 0o600)
                res = {"ok": True, "applies": "bij de volgende herstart"}
            elif p == "/api/wifi/enabled":
                res = WH.set_enabled(bool(req.get("on")))
            elif p == "/api/wifi/scan":
                res = WH.scan()
            elif p == "/api/wifi/connect":
                if not (WH.cfg.get("ssid") and WH.cfg.get("psk_hex")):
                    raise ValueError("eerst een thuisnetwerk instellen")
                WH.hold_ap_until = 0
                WH.force_home = True
                res = WH.public()
            elif p == "/api/wifi/hold":
                WH.hold_ap_until = time.monotonic() + 60 * float(req.get("minutes", 30))
                res = WH.public()
            elif p == "/api/wifi/ap":
                threading.Thread(target=WH.to_ap, args=("handmatig teruggezet",), daemon=True).start()
                res = {"ok": True}
            elif p == "/api/leds/test":
                LD.color_test()
                res = {"ok": True}
            elif p == "/api/range/clear":
                ST.range_log.clear()
                ST.range_pending.clear()
                try:
                    os.replace(RANGE_FILE, RANGE_FILE + ".%d" % int(time.time()))
                except OSError:
                    pass
                res = {"ok": True}
            elif p == "/api/devices/clear":
                ST.devices.clear()
                ST.chan.clear()
                ST._save_devices()
                res = {"ok": True}
            elif p == "/api/clear":
                with ST.lock:
                    ST.packets.clear()
                    ST.stats.clear()
                    ST.peers.clear()
                res = {"ok": True}
            else:
                return self._json({"error": "onbekend"}, 404)
            self._json({"ok": True, "result": res})
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "error": str(e)}, 400)


def main():
    threading.Thread(target=ST.run_radio, daemon=True).start()
    threading.Thread(target=WH.run, daemon=True).start()
    GP.start()
    LD.start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.serve_forever()


if __name__ == "__main__":
    main()
