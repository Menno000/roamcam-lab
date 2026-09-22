"""GPS van de HDC: u-blox M9 op /dev/ttyAMA1. Alleen stdlib.

De chip heeft geen backupbatterij. Na echte stroomuitval (contact uit in de auto) start hij op zijn
fabrieksinstelling: 38400 baud en alleen NMEA. Pas de vendor-`datalogger` zet hem op 921600 baud met
UBX NAV-PVT -- en die datalogger schakelen wij uit. Daarom zoeken we hier zelf de baudrate, zetten
NAV-PVT zelf aan (alleen in RAM) en lezen als vangnet ook NMEA (RMC + GGA).
"""
import calendar
import os
import struct
import subprocess
import termios
import threading
import time

PORT = "/dev/ttyAMA1"
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


# UBX-CFG-VALSET (RAM): NAV-PVT elke navigatiecyclus op UART1
ENABLE_NAVPVT = ubx(0x06, 0x8A, struct.pack("<BBH", 0, 0x01, 0) + struct.pack("<IB", 0x20910007, 1))


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
                os.write(fd, ENABLE_NAVPVT)
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
            except Exception as e:  # noqa: BLE001
                self.error = "%s: %s" % (type(e).__name__, e)
                time.sleep(5)

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
                    nmea=self.nmea_frames, baud=self.baud, source=self.source)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()
        return self
