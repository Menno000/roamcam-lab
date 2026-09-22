"""GPS van de HDC: u-blox M9 op /dev/ttyAMA1. Alleen stdlib.

De chip heeft geen backupbatterij. Na echte stroomuitval (contact uit in de auto) start hij op zijn
fabrieksinstelling: 38400 baud en alleen NMEA. Pas de vendor-`datalogger` zet hem op 921600 baud met
UBX NAV-PVT -- en die datalogger schakelen wij uit. Daarom zoeken we hier zelf de baudrate, zetten
NAV-PVT zelf aan (alleen in RAM) en lezen als vangnet ook NMEA (RMC + GGA).
"""
import calendar
import json
import os
import struct
import subprocess
import termios
import threading
import time

PORT = "/dev/ttyAMA1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DBD_FILE = os.path.join(BASE_DIR, "gnss_dbd.bin")       # navigatiedatabase van de chip
DBD_META = os.path.join(BASE_DIR, "gnss_dbd.json")      # wanneer/waar opgeslagen
DBD_MAX_AGE_S = 3 * 24 * 3600
DBD_SAVE_EVERY_S = 300
BAUDS = (921600, 38400, 115200, 9600, 230400, 460800)
_B = {921600: termios.B921600, 460800: termios.B460800, 230400: termios.B230400,
      115200: termios.B115200, 38400: termios.B38400, 9600: termios.B9600}


def ubx(cls, mid, payload=b""):
    body = bytes([cls, mid]) + struct.pack("<H", len(payload)) + payload
    a = b = 0
    for x in body:
        a = (a + x) & 0xFF
        b = (b + a) & 0xFF
    return b"\xb5\x62" + body + bytes([a, b])


def valset(*keyvals):
    """UBX-CFG-VALSET in RAM. keyvals: (sleutel, waarde, formaat)."""
    body = struct.pack("<BBH", 0, 0x01, 0)
    for key, val, fmt in keyvals:
        body += struct.pack("<I", key) + struct.pack(fmt, val)
    return ubx(0x06, 0x8A, body)


# NAV-PVT elke navigatiecyclus op UART1
ENABLE_NAVPVT = valset((0x20910007, 1, "<B"))
# Sneller een fix na stroomuitval (de chip heeft geen backupbatterij):
#  - ACKAIDING: de chip bevestigt hulpgegevens, zodat we zien of ze aankomen
#  - CFG-ANA-USE_ANA: AssistNow Autonomous, de chip rekent zelf satellietbanen vooruit
#  - alle vier de satellietstelsels aan: meer satellieten in beeld = sneller
SPEED_UP = valset((0x10110025, 1, "<B"),          # CFG-NAVSPG-ACKAIDING
                  (0x10230001, 1, "<B"),          # CFG-ANA-USE_ANA
                  (0x1031001F, 1, "<B"),          # CFG-SIGNAL-GPS_ENA
                  (0x10310021, 1, "<B"),          # CFG-SIGNAL-GAL_ENA
                  (0x10310022, 1, "<B"),          # CFG-SIGNAL-BDS_ENA
                  (0x10310025, 1, "<B"))          # CFG-SIGNAL-GLO_ENA
POLL_DBD = ubx(0x13, 0x80)                        # UBX-MGA-DBD opvragen


def mga_ini_time(epoch, acc_s):
    """UBX-MGA-INI-TIME_UTC: ruwe tijd met eerlijke onzekerheid."""
    t = time.gmtime(epoch)
    # type, version, ref, leapSecs | jaar, maand, dag, uur, min, sec, reserved | ns | tAccS | res | tAccNs
    return ubx(0x13, 0x40, struct.pack("<BBBbHBBBBBBIHHI", 0x10, 0x00, 0x00, -128, t.tm_year, t.tm_mon,
                                       t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, 0, 0,
                                       int(min(65535, max(1, acc_s))), 0, 0))


def mga_ini_pos(lat, lon, alt_m, acc_m):
    """UBX-MGA-INI-POS_LLH: laatst bekende plek."""
    return ubx(0x13, 0x40, struct.pack("<BBBBiiii", 0x01, 0x00, 0x00, 0x00, int(round(lat * 1e7)),
                                       int(round(lon * 1e7)), int(round(alt_m * 100)),
                                       int(max(1, acc_m) * 100)))


def _nmea_ok(line):
    """'$GNRMC,...*hh' -> True als de checksum klopt."""
    if not line.startswith(b"$") or b"*" not in line:
        return False
    body, _, ck = line[1:].partition(b"*")
    c = 0
    for x in body:
        c ^= x
    try:
        return int(ck[:2], 16) == c
    except ValueError:
        return False


def _deg(v, hemi):
    if not v:
        return None
    dot = v.index(".")
    d = float(v[:dot - 2]) + float(v[dot - 2:]) / 60
    return -d if hemi in ("S", "W") else d


class GPS:
    def __init__(self, port=PORT, set_clock=True):
        self.port = port
        self.set_clock = set_clock
        self.fix = {}
        self.error = None
        self.frames = 0
        self.nmea_frames = 0
        self.baud = None
        self.source = None           # "ubx" of "nmea"
        self.clock_set = False
        self._t_last = 0
        self._t_ubx = 0
        self._gga = {}
        self._fd = None
        self._t_start = time.monotonic()
        self.ttff_s = None            # tijd tot de eerste fix na (her)start
        self.aided = None             # wat we de chip hebben meegegeven
        self._dbd_saved = 0
        self._jobs = []               # opdrachten die de GPS-thread zelf moet doen
        self._results = {}

    def age_s(self):
        return time.monotonic() - self._t_last if self._t_last else None

    def has_fix(self):
        f = self.fix
        return bool(f) and f.get("fix_type", 0) >= 2 and f.get("gnss_ok") and (self.age_s() or 99) < 3

    # --- seriële poort -------------------------------------------------
    def _open(self, baud):
        fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY)
        a = termios.tcgetattr(fd)
        a[0] = 0
        a[1] = 0
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[3] = 0
        a[4] = a[5] = _B[baud]
        a[6][termios.VMIN] = 0
        a[6][termios.VTIME] = 5
        termios.tcsetattr(fd, termios.TCSANOW, a)
        termios.tcflush(fd, termios.TCIFLUSH)
        return fd

    def _detect(self):
        """Probeert de baudrates; geeft (fd, baud) terug zodra er geldige UBX of NMEA langskomt."""
        for baud in BAUDS:
            fd = self._open(baud)
            buf, t0 = b"", time.monotonic()
            while time.monotonic() - t0 < 2.0:
                buf += os.read(fd, 4096)
                if self._valid_in(buf):
                    return fd, baud
            os.close(fd)
        return None, None

    def _valid_in(self, buf):
        if any(_nmea_ok(ln.strip()) for ln in buf.split(b"\n")[:-1]):
            return True
        i = buf.find(b"\xb5\x62")
        while 0 <= i and i + 8 <= len(buf):
            ln = struct.unpack_from("<H", buf, i + 4)[0]
            if ln < 1024 and i + 8 + ln <= len(buf) and ubx(buf[i + 2], buf[i + 3], buf[i + 6:i + 6 + ln]) == buf[i:i + 8 + ln]:
                return True
            i = buf.find(b"\xb5\x62", i + 2)
        return False

    def run(self):
        while True:
            try:
                fd, baud = self._detect()
                if fd is None:
                    self.error = "geen GPS-gegevens op %s (alle baudrates geprobeerd)" % self.port
                    time.sleep(5)
                    continue
                self.baud, self.error = baud, None
                self._fd = fd
                os.write(fd, ENABLE_NAVPVT)
                os.write(fd, SPEED_UP)
                self._aid(fd)
                buf, t_data = b"", time.monotonic()
                while True:
                    chunk = os.read(fd, 4096)
                    if chunk:
                        buf += chunk
                        n0 = self.frames + self.nmea_frames
                        buf = self._parse(buf)
                        if self.frames + self.nmea_frames != n0:
                            t_data = time.monotonic()
                    if time.monotonic() - t_data > 5:          # stil of onzin: opnieuw zoeken
                        os.close(fd)
                        break
                    # NAV-PVT nog niet gezien terwijl NMEA wel komt: nogmaals aanzetten
                    if self.nmea_frames and time.monotonic() - self._t_ubx > 10 and self.nmea_frames % 50 == 1:
                        os.write(fd, ENABLE_NAVPVT)
                    # met fix: de navigatiedatabase bewaren voor de volgende koude start
                    if self.has_fix() and time.monotonic() - self._dbd_saved > DBD_SAVE_EVERY_S:
                        self._dbd_saved = time.monotonic()
                        self._save_dbd()
                    self._run_jobs()
            except Exception as e:  # noqa: BLE001
                self.error = "%s: %s" % (type(e).__name__, e)
                time.sleep(5)

    # --- hulp bij het opstarten -------------------------------------------
    def _aid(self, fd):
        """Laatst bekende tijd, plaats en navigatiedatabase terugzetten: warme start."""
        out = {"dbd": 0, "time": False, "pos": False}
        try:
            meta = json.load(open(DBD_META))
            data = open(DBD_FILE, "rb").read()
        except (OSError, ValueError):
            self.aided = dict(out, note="niets bewaard")
            return
        age = time.time() - meta.get("time", 0)
        if not 0 < age < DBD_MAX_AGE_S or not data:
            self.aided = dict(out, note="bewaarde gegevens zijn %.1f uur oud" % (age / 3600))
            return
        # tijd: wij weten alleen hoe oud het bestand is; die onzekerheid geven we eerlijk mee
        os.write(fd, mga_ini_time(meta["time"] + age, acc_s=min(age, 3600)))
        out["time"] = True
        if meta.get("lat") is not None:
            os.write(fd, mga_ini_pos(meta["lat"], meta["lon"], meta.get("alt", 0), acc_m=20000))
            out["pos"] = True
        time.sleep(0.05)
        # Bericht voor bericht, en de bevestiging (UBX-MGA-ACK) meelezen: anders weet je niet of
        # de chip de hulp aanneemt. infoCode != 0 betekent geweigerd, met reden.
        ok = bad = 0
        codes = {}
        for msg in self._split_ubx(data):
            os.write(fd, msg)
            a, code = self._read_mga_ack(fd, 0.3)
            if a is True:
                ok += 1
            elif a is False:
                bad += 1
                codes[code] = codes.get(code, 0) + 1
        out.update(dbd=len(data), msgs=ok + bad, aangenomen=ok, geweigerd=bad, redenen=codes,
                   age_h=round(age / 3600, 2))
        self.aided = out

    @staticmethod
    def _split_ubx(data):
        out, i = [], 0
        while True:
            j = data.find(b"\xb5\x62", i)
            if j < 0 or len(data) < j + 8:
                return out
            ln = struct.unpack_from("<H", data, j + 4)[0]
            if len(data) < j + 8 + ln:
                return out
            out.append(data[j:j + 8 + ln])
            i = j + 8 + ln

    def _read_mga_ack(self, fd, timeout):
        """-> (True/False/None, infoCode). None = geen bevestiging gezien."""
        t0 = time.monotonic()
        buf = b""
        while time.monotonic() - t0 < timeout:
            try:
                buf += os.read(fd, 1024)
            except BlockingIOError:
                time.sleep(0.005)
                continue
            i = 0
            while True:
                j = buf.find(b"\xb5\x62", i)
                if j < 0 or len(buf) < j + 8:
                    break
                ln = struct.unpack_from("<H", buf, j + 4)[0]
                if len(buf) < j + 8 + ln:
                    break
                msg = buf[j:j + 8 + ln]
                if msg[2] == 0x13 and msg[3] == 0x60 and ln >= 8:      # UBX-MGA-ACK-DATA0
                    return msg[6] == 1, msg[8]
                self._parse(msg)
                i = j + 8 + ln
            buf = buf[i:]
        return None, None

    # Alleen de GPS-thread mag van de poort lezen; anders pikken twee lezers elkaars bytes weg
    # (dat maakte de eerste metingen onbetrouwbaar). Van buiten af gaat alles via deze wachtrij.
    def request(self, what, timeout=30, **kw):
        key = object()
        self._jobs.append((key, what, kw))
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if key in self._results:
                return self._results.pop(key)
            time.sleep(0.05)
        return {"error": "GPS-thread reageerde niet"}

    def _run_jobs(self):
        while self._jobs:
            key, what, kw = self._jobs.pop(0)
            try:
                if what == "save":
                    self._results[key] = {"saved": self._save_dbd()}
                elif what == "coldstart":
                    self._results[key] = {"reset": self._cold_reset(**kw)}
                else:
                    self._results[key] = {"error": "onbekend: %s" % what}
            except Exception as e:  # noqa: BLE001
                self._results[key] = {"error": "%s: %s" % (type(e).__name__, e)}

    def save_dbd(self):
        return self.request("save").get("saved", False)

    def cold_reset(self, aid=True):
        return self.request("coldstart", aid=aid).get("reset", False)

    def _save_dbd(self):
        """Navigatiedatabase van de chip opvragen en bewaren (UBX-MGA-DBD)."""
        fd = self._fd
        if fd is None:
            return False
        os.write(fd, POLL_DBD)
        chunks, t0, last = b"", time.monotonic(), time.monotonic()
        buf = b""
        while time.monotonic() - t0 < 5 and time.monotonic() - last < 1.0:
            try:
                buf += os.read(fd, 4096)
            except BlockingIOError:
                time.sleep(0.01)
                continue
            i = 0
            while True:
                j = buf.find(b"\xb5\x62", i)
                if j < 0 or len(buf) < j + 8:
                    break
                ln = struct.unpack_from("<H", buf, j + 4)[0]
                if len(buf) < j + 8 + ln:
                    break
                msg = buf[j:j + 8 + ln]
                if msg[2] == 0x13 and msg[3] == 0x80:
                    chunks += msg
                    last = time.monotonic()
                else:
                    self._parse(msg)                 # NAV-PVT ondertussen gewoon verwerken
                i = j + 8 + ln
            buf = buf[i:]
        if not chunks:
            return False
        f = self.fix if self.has_fix() else {}
        tmp = DBD_FILE + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(chunks)
        os.replace(tmp, DBD_FILE)
        with open(DBD_META + ".tmp", "w") as fh:
            json.dump({"time": time.time(), "lat": f.get("lat"), "lon": f.get("lon"),
                       "alt": f.get("alt_m", 0), "bytes": len(chunks)}, fh)
        os.replace(DBD_META + ".tmp", DBD_META)
        return True

    def _cold_reset(self, aid=True):
        """UBX-CFG-RST: alles wissen en herstarten, net als stroom eraf. Voor tests, en om te
        meten of de bewaarde navigatiedatabase de eerste fix echt versnelt."""
        if self._fd is None:
            return False
        os.write(self._fd, ubx(0x06, 0x04, struct.pack("<HBB", 0xFFFF, 0x00, 0)))
        self.fix, self.ttff_s, self.aided = {}, None, None
        time.sleep(1.5)
        os.write(self._fd, ENABLE_NAVPVT)
        os.write(self._fd, SPEED_UP)
        if aid:
            self._aid(self._fd)
        else:
            self.aided = {"dbd": 0, "time": False, "pos": False, "note": "bewust zonder hulp (meting)"}
        self._t_start = time.monotonic()
        return True

    # --- ontleden ---------------------------------------------------------
    def _parse(self, buf):
        while buf:
            i_u = buf.find(b"\xb5\x62")
            i_n = buf.find(b"$")
            if i_u < 0 and i_n < 0:
                return b""
            if i_n >= 0 and (i_u < 0 or i_n < i_u):
                end = buf.find(b"\n", i_n)
                if end < 0:
                    return buf[i_n:] if len(buf) - i_n < 200 else b""
                self._nmea(buf[i_n:end].strip())
                buf = buf[end + 1:]
                continue
            i = i_u
            if len(buf) < i + 6:
                return buf[i:]
            cls, mid, ln = buf[i + 2], buf[i + 3], struct.unpack_from("<H", buf, i + 4)[0]
            if ln > 1024:
                buf = buf[i + 2:]
                continue
            end = i + 8 + ln
            if len(buf) < end:
                return buf[i:]
            if ubx(cls, mid, buf[i + 6:i + 6 + ln]) == buf[i:end] and cls == 0x01 and mid == 0x07 and ln >= 92:
                self._navpvt(buf[i + 6:i + 6 + ln])
            buf = buf[end:]
        return buf

    def _nmea(self, line):
        if not _nmea_ok(line):
            return
        f = line[1:line.index(b"*")].decode(errors="replace").split(",")
        kind = f[0][2:]
        if kind == "GGA" and len(f) > 9:
            self._gga = {"quality": int(f[6] or 0), "sats": int(f[7] or 0),
                         "hdop": float(f[8] or 99), "alt": float(f[9] or 0)}
        elif kind == "RMC" and len(f) > 9:
            self.nmea_frames += 1
            if time.monotonic() - self._t_ubx < 3:
                return                                   # NAV-PVT is er: die is rijker
            ok = f[2] == "A"
            g = self._gga
            self._t_last = time.monotonic()
            self.source = "nmea"
            self.fix = {
                "fix_type": (3 if g.get("quality", 0) and g.get("sats", 0) >= 4 else 2) if ok else 0,
                "gnss_ok": ok, "sats": g.get("sats", 0), "lat": _deg(f[3], f[4]), "lon": _deg(f[5], f[6]),
                "alt_m": g.get("alt"), "hacc_m": round(g.get("hdop", 99) * 2.5, 1),
                "speed_kmh": round(float(f[7] or 0) * 1.852, 1), "heading": float(f[8] or 0),
                "pdop": None, "time_valid": ok and bool(f[1]) and bool(f[9]),
            }
            if self.fix["time_valid"] and len(f[9]) == 6 and len(f[1]) >= 6:
                d, t = f[9], f[1]
                self._maybe_set_clock(2000 + int(d[4:6]), int(d[2:4]), int(d[0:2]), int(t[0:2]), int(t[2:4]), int(t[4:6]))

    def _navpvt(self, p):
        (itow, year, month, day, hour, minute, sec, valid, tacc, nano, fix_type, flags, flags2, numsv,
         lon, lat, height, hmsl, hacc, vacc, veln, vele, veld, gspeed, headmot, sacc, headacc,
         pdop) = struct.unpack_from("<IHBBBBBBIiBBBBiiiiIIiiiiiIIH", p, 0)
        self.frames += 1
        self._t_last = self._t_ubx = time.monotonic()
        if self.ttff_s is None and fix_type >= 2 and flags & 0x01:
            self.ttff_s = round(time.monotonic() - self._t_start, 1)
        self.source = "ubx"
        self.fix = {
            "fix_type": fix_type, "gnss_ok": bool(flags & 0x01), "sats": numsv,
            "lat": lat / 1e7, "lon": lon / 1e7, "alt_m": round(hmsl / 1000, 1),
            "hacc_m": round(hacc / 1000, 1), "speed_kmh": round(gspeed * 0.0036, 1),
            "heading": round(headmot / 1e5, 1), "pdop": pdop / 100,
            "time_valid": (valid & 0x07) == 0x07,
        }
        if self.fix["time_valid"]:
            self._maybe_set_clock(year, month, day, hour, minute, sec)

    def _maybe_set_clock(self, year, month, day, hour, minute, sec):
        # Geen RTC en in de auto geen internet: klok van de GPS nemen.
        if not self.set_clock or self.clock_set:
            return
        t = calendar.timegm((year, month, day, hour, minute, sec, 0, 0, 0))
        if abs(t - time.time()) > 5:
            subprocess.run(["date", "-u", "-s", "@%d" % int(t)], capture_output=True)
        self.clock_set = True

    def status(self):
        return dict(self.fix, ok=self.has_fix(), age_s=self.age_s(), error=self.error, frames=self.frames,
                    nmea=self.nmea_frames, baud=self.baud, source=self.source, ttff_s=self.ttff_s,
                    aided=self.aided)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()
        return self
