"""Thuiswifi voor de HDC, met de eigen AP als vangnet.

Overgenomen uit Roamcam (beta, dashboard_server.py), waar dit twee keer misging. De regels:
  1. De eigen AP is altijd de begintoestand en de terugval. /mnt/data/wifi.cfg blijft op "AP",
     dus na een herstart komt het toestel altijd schoon op zijn eigen AP.
  2. Eerst SCANNEN of het thuisnetwerk er echt is (kan tijdens AP-modus), pas dan schakelen.
  3. Nooit automatisch wegschakelen zolang er iemand op de AP zit (/proc/net/arp).
  4. dhcpcd doet het DHCP-werk, met een config die wlan0 wél beheert. De AP-config verbiedt dat
     (denyinterfaces wlan0) -- vorige keer kreeg het toestel wel een lease maar paste hem niet toe.
  5. AP en client tegelijk kan deze brcmfmac-chip niet ("Operation not supported"): het is schakelen.
Extra voor de basis: thuis weg of de router reageert 2 minuten niet -> terug naar de eigen AP.
"""
import hashlib
import json
import os
import re
import subprocess
import time

STA_CONF = "/etc/wpa_supplicant-sta.conf"
AP_DHCPCD = "/opt/dashcam/cfg/AP_dhcpcd.conf"
AP_IP = "192.168.0.10"
SETTLE_S = 60          # na opstarten eerst met rust laten: tijd om zelf op de AP te komen
SCAN_EVERY_S = 30
LOST_GRACE_S = 120     # zo lang mag thuis weg zijn voor we terugvallen
MIN_SIGNAL_DBM = -80


def sh(cmd, timeout=30):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout
    except Exception as e:  # noqa: BLE001
        return "ERR %s" % e


class WifiHome:
    def __init__(self, path):
        self.path = path
        self.cfg = {"ssid": "", "psk_hex": "", "enabled": False}
        try:
            with open(path) as f:
                self.cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.state = {"mode": "ap", "ip": "", "gateway": "", "last_error": "", "last_scan": 0,
                      "seen": None, "signal": None, "clients": 0, "since": time.time(), "log": []}
        self.boot = time.monotonic()
        self.force_home = False
        self.hold_ap_until = 0
        # Bij een herstart van dit programma kan het toestel al op het thuisnetwerk zitten. Dan
        # die toestand overnemen, anders "schakelt" hij na een minuut opnieuw en valt hij weg.
        if "wpa_state=COMPLETED" in sh("wpa_cli -i wlan0 status 2>/dev/null"):
            ip = self.ip()
            if ip and ip != AP_IP:
                self.state.update(mode="home", ip=ip, gateway=self.gateway())
                self._log("programma herstart, zat al op het thuisnetwerk (%s)" % ip)
                sh("iwconfig wlan0 power off 2>/dev/null")

    # --- instellingen ------------------------------------------------------
    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.cfg, f)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def set_network(self, ssid, password, enabled=True):
        """Wachtwoord wordt meteen omgezet naar de WPA-sleutel (PBKDF2), het leesbare wachtwoord
        wordt niet bewaard en er zijn geen aanhalingsteken-problemen in de wpa_supplicant-config."""
        ssid = (ssid or "").strip()
        if not ssid:
            self.cfg.update(ssid="", psk_hex="", enabled=False)
            self._save()
            self._log("thuisnetwerk gewist")
            return self.public()
        if password:
            if not 8 <= len(password) <= 63:
                raise ValueError("wifi-wachtwoord moet 8 tot 63 tekens zijn")
            psk = hashlib.pbkdf2_hmac("sha1", password.encode(), ssid.encode(), 4096, 32).hex()
        elif ssid == self.cfg.get("ssid") and self.cfg.get("psk_hex"):
            psk = self.cfg["psk_hex"]           # zelfde netwerk, wachtwoord ongewijzigd laten
        else:
            raise ValueError("wachtwoord ontbreekt")
        self.cfg.update(ssid=ssid, psk_hex=psk, enabled=bool(enabled))
        self._save()
        self._log("thuisnetwerk ingesteld: %s (%s)" % (ssid, "aan" if enabled else "uit"))
        return self.public()

    def set_enabled(self, on):
        self.cfg["enabled"] = bool(on)
        self._save()
        if not on and self.state["mode"] == "home":
            self.to_ap("thuisnetwerk uitgezet")
        return self.public()

    def public(self):
        return {"ssid": self.cfg["ssid"], "has_password": bool(self.cfg["psk_hex"]),
                "enabled": self.cfg["enabled"], **{k: v for k, v in self.state.items()},
                "hold_ap_s": max(0, int(self.hold_ap_until - time.monotonic()))}

    def _log(self, msg):
        self.state["log"] = (self.state["log"] + [[time.time(), msg]])[-30:]

    # --- metingen ----------------------------------------------------------
    @staticmethod
    def ap_clients():
        n = 0
        try:
            with open("/proc/net/arp") as f:
                for ln in f.read().splitlines()[1:]:
                    p = ln.split()
                    if len(p) >= 6 and p[-1] == "wlan0" and p[2] == "0x2":
                        n += 1
        except OSError:
            pass
        return n

    @staticmethod
    def scan():
        nets, ssid, sig, enc = {}, None, None, False
        for ln in sh("iwlist wlan0 scan 2>/dev/null").splitlines():
            ln = ln.strip()
            if ln.startswith("Cell "):
                ssid, sig, enc = None, None, False
            elif "Signal level" in ln:
                m = re.search(r"Signal level[=:]\s*(-?\d+)", ln)
                if m:
                    sig = int(m.group(1))
            elif ln.startswith("Encryption key:"):
                enc = ln.endswith("on")
            elif ln.startswith("ESSID:"):
                ssid = ln.split(":", 1)[1].strip().strip('"')
                if ssid and ssid != "\\x00":
                    cur = nets.get(ssid)
                    s = sig if sig is not None else -99
                    if cur is None or s > cur["signal"]:
                        nets[ssid] = {"ssid": ssid, "signal": s, "secure": enc}
        return sorted(nets.values(), key=lambda n: -n["signal"])

    @staticmethod
    def ip():
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", sh("ip -4 -o addr show wlan0 2>/dev/null"))
        return m.group(1) if m else ""

    @staticmethod
    def gateway():
        m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", sh("ip route 2>/dev/null"))
        return m.group(1) if m else ""

    # --- schakelen ---------------------------------------------------------
    def to_ap(self, reason=""):
        sh("systemctl stop roamcam-wpa wpa_supplicant 2>/dev/null; killall wpa_supplicant 2>/dev/null")
        sh("systemctl stop dhcpcd 2>/dev/null")
        sh("ip addr flush dev wlan0 2>/dev/null")
        sh("cp %s /etc/dhcpcd.conf 2>/dev/null" % AP_DHCPCD)
        sh("systemctl restart hostapd 2>/dev/null")
        sh("systemctl restart dnsmasq 2>/dev/null")
        sh("systemctl restart dhcpcd 2>/dev/null")
        self.state.update(mode="ap", ip=AP_IP, gateway="", since=time.time())
        if reason:
            self.state["last_error"] = reason
            self._log("terug naar eigen AP: %s" % reason)

    def to_home(self):
        ssid, psk = self.cfg.get("ssid"), self.cfg.get("psk_hex")
        if not (ssid and psk):
            return False
        self._log("schakelt naar thuisnetwerk %s" % ssid)
        try:
            with open(STA_CONF, "w") as f:
                f.write("ctrl_interface=/var/run/wpa_supplicant\ncountry=NL\n"
                        "network={\n  ssid=%s\n  psk=%s\n  scan_ssid=1\n}\n" % (ssid.encode().hex(), psk))
            os.chmod(STA_CONF, 0o600)
            sh("systemctl stop hostapd 2>/dev/null; systemctl stop dnsmasq 2>/dev/null; systemctl stop dhcpcd 2>/dev/null")
            sh("ip addr flush dev wlan0 2>/dev/null")
            with open("/etc/dhcpcd.conf", "w") as f:
                f.write("hostname\nclientid\npersistent\noption rapid_commit\n"
                        "option domain_name_servers, domain_name, domain_search, host_name\n"
                        "option classless_static_routes\noption interface_mtu\n"
                        "require dhcp_server_identifier\nslaac private\nnoipv6rs\nnoipv6\n")
            # Als eigen systemd-unit, NIET als kind van dit programma: anders zit hij in onze cgroup
            # en schiet systemd hem af bij elke herstart van het LoRa-programma (twee keer gebeurd).
            sh("systemctl stop roamcam-wpa 2>/dev/null; killall wpa_supplicant 2>/dev/null")
            sh("systemd-run --unit=roamcam-wpa --collect /sbin/wpa_supplicant -i wlan0 -c %s -Dnl80211 "
               "-f /mnt/data/basis/wifi_sta.log" % STA_CONF)
            t0 = time.monotonic()
            joined = False
            while time.monotonic() - t0 < 25:
                if "wpa_state=COMPLETED" in sh("wpa_cli -i wlan0 status 2>/dev/null"):
                    joined = True
                    break
                time.sleep(1)
            if not joined:
                self.to_ap("kon niet koppelen aan %s (wachtwoord fout of te zwak signaal)" % ssid)
                return False
            sh("systemctl restart dhcpcd 2>/dev/null")
            t0 = time.monotonic()
            while time.monotonic() - t0 < 25:
                ip = self.ip()
                if ip and ip != AP_IP:
                    gw = self.gateway()
                    self.state.update(mode="home", ip=ip, gateway=gw, last_error="", since=time.time())
                    self._log("verbonden met %s, adres %s" % (ssid, ip))
                    sh("iwconfig wlan0 power off 2>/dev/null")           # energiebesparing geeft haperingen
                    sh("systemctl restart avahi-daemon 2>/dev/null")      # naam.local op het thuisnet
                    sh("systemctl restart systemd-timesyncd 2>/dev/null")  # klok via internet
                    return True
                time.sleep(1)
            self.to_ap("wel gekoppeld maar geen adres van de router")
            return False
        except Exception as e:  # noqa: BLE001
            self.to_ap("fout bij omschakelen: %s" % e)
            return False

    def _home_ok(self):
        if "wpa_state=COMPLETED" not in sh("wpa_cli -i wlan0 status 2>/dev/null"):
            return False
        gw = self.state.get("gateway") or self.gateway()
        if not gw:
            return True        # zonder gateway-info niet op pingen afrekenen
        return bool(re.search(r"\b[1-9]\d* packets received", sh("ping -c 2 -W 3 %s 2>&1" % gw)))

    # --- lus -----------------------------------------------------------------
    def run(self):
        lost_since = 0
        while True:
            try:
                if self.state["mode"] == "home":
                    if self._home_ok():
                        lost_since = 0
                        self.state["ip"] = self.ip() or self.state["ip"]
                    else:
                        lost_since = lost_since or time.monotonic()
                        if time.monotonic() - lost_since > LOST_GRACE_S:
                            lost_since = 0
                            self.to_ap("thuisnetwerk of router %d s niet bereikbaar" % LOST_GRACE_S)
                    time.sleep(15)
                    continue
                self.state["clients"] = self.ap_clients()
                want = self.cfg.get("enabled") and self.cfg.get("ssid") and self.cfg.get("psk_hex")
                settled = time.monotonic() - self.boot > SETTLE_S
                held = time.monotonic() < self.hold_ap_until
                if want and (self.force_home or (settled and not held and self.state["clients"] == 0)):
                    self.force_home = False
                    nets = {n["ssid"]: n["signal"] for n in self.scan()}
                    sig = nets.get(self.cfg["ssid"])
                    self.state.update(last_scan=time.time(), seen=sig is not None, signal=sig)
                    if sig is not None and sig >= MIN_SIGNAL_DBM:
                        self.to_home()
                    elif sig is not None:
                        self.state["last_error"] = "thuisnetwerk te zwak (%d dBm)" % sig
            except Exception as e:  # noqa: BLE001
                self.state["last_error"] = str(e)
            for _ in range(SCAN_EVERY_S):      # onderbreekbaar wachten, voor de knop "nu verbinden"
                if self.force_home:
                    break
                time.sleep(1)
