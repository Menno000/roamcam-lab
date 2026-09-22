"""GPS van de HDC: u-blox M9 op /dev/ttyAMA1, 921600 baud (zo laat de vendor-firmware hem staan).

Leest UBX NAV-PVT (positie, snelheid, nauwkeurigheid en tijd in één bericht). De vendor-`datalogger`
moet gestopt zijn, die houdt de poort anders vast. Alleen stdlib.
"""
import calendar
import os
import struct
import subprocess
import termios
import threading
import time

PORT = "/dev/ttyAMA1"


class GPS:
    def __init__(self, port=PORT, set_clock=True):
        self.port = port
        self.set_clock = set_clock
        self.fix = {}              # laatste NAV-PVT
        self.error = None
        self.frames = 0
        self.clock_set = False
        self._t_last = 0

    def age_s(self):
        return time.monotonic() - self._t_last if self._t_last else None

    def has_fix(self):
        f = self.fix
        return bool(f) and f.get("fix_type", 0) >= 2 and f.get("gnss_ok") and (self.age_s() or 99) < 3

    def _open(self):
        fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY)
        a = termios.tcgetattr(fd)
        a[0] = 0
        a[1] = 0
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[3] = 0
        a[4] = a[5] = termios.B921600
        a[6][termios.VMIN] = 1
        a[6][termios.VTIME] = 5
        termios.tcsetattr(fd, termios.TCSANOW, a)
        termios.tcflush(fd, termios.TCIFLUSH)
        return fd

    def run(self):
        while True:
            try:
                fd = self._open()
                self.error = None
                buf = b""
                while True:
                    buf += os.read(fd, 4096)
                    buf = self._parse(buf)
            except Exception as e:  # noqa: BLE001
                self.error = "%s: %s" % (type(e).__name__, e)
                time.sleep(5)

    def _parse(self, buf):
        while True:
            i = buf.find(b"\xb5\x62")
            if i < 0:
                return buf[-1:]
            if len(buf) < i + 6:
                return buf[i:]
            cls, mid, ln = buf[i + 2], buf[i + 3], struct.unpack_from("<H", buf, i + 4)[0]
            if ln > 1024:
                buf = buf[i + 2:]
                continue
            end = i + 6 + ln + 2
            if len(buf) < end:
                return buf[i:]
            frame = buf[i + 2:end - 2]
            ck_a = ck_b = 0
            for b in frame:
                ck_a = (ck_a + b) & 0xFF
                ck_b = (ck_b + ck_a) & 0xFF
            if (ck_a, ck_b) == (buf[end - 2], buf[end - 1]) and cls == 0x01 and mid == 0x07 and ln >= 92:
                self._navpvt(buf[i + 6:i + 6 + ln])
            buf = buf[end:]

    def _navpvt(self, p):
        (itow, year, month, day, hour, minute, sec, valid, tacc, nano, fix_type, flags, flags2, numsv,
         lon, lat, height, hmsl, hacc, vacc, veln, vele, veld, gspeed, headmot, sacc, headacc,
         pdop) = struct.unpack_from("<IHBBBBBBIiBBBBiiiiIIiiiiiIIH", p, 0)
        self.frames += 1
        self._t_last = time.monotonic()
        self.fix = {
            "fix_type": fix_type, "gnss_ok": bool(flags & 0x01), "sats": numsv,
            "lat": lat / 1e7, "lon": lon / 1e7, "alt_m": round(hmsl / 1000, 1),
            "hacc_m": round(hacc / 1000, 1), "speed_kmh": round(gspeed * 0.0036, 1),
            "heading": round(headmot / 1e5, 1), "pdop": pdop / 100,
            "utc": "%04d-%02d-%02dT%02d:%02d:%02dZ" % (year, month, day, hour, minute, sec),
            "time_valid": (valid & 0x07) == 0x07,
        }
        # Geen RTC en in de auto geen internet: klok van de GPS nemen als die geldig is.
        if self.set_clock and self.fix["time_valid"] and not self.clock_set:
            t = calendar.timegm((year, month, day, hour, minute, sec, 0, 0, 0))
            if abs(t - time.time()) > 5:
                subprocess.run(["date", "-u", "-s", "@%d" % int(t)], capture_output=True)
            self.clock_set = True

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()
        return self
