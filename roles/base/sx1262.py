"""Eigen SX1262-driver voor de LoRa-chip in de Hivemapper HDC.

Pure Python stdlib: SPI via /dev/spidev1.0-ioctl, GPIO via de gpiochip v1-ioctl.
Pinnen (gpiochip0): CS 18, BUSY 25, NRST 24, DIO1 7 -- uit de vendor lorawan.conf.
De vendor-daemon `lorawan-logger` moet gestopt zijn, anders vechten we om dezelfde chip.
"""
import array
import fcntl
import math
import os
import struct
import time

SPIDEV = "/dev/spidev1.0"
GPIOCHIP = "/dev/gpiochip0"
PIN_CS, PIN_BUSY, PIN_NRST, PIN_DIO1 = 18, 25, 24, 7

# --- ioctl-nummers -------------------------------------------------------
SPI_IOC_MESSAGE_1 = 0x40206B00
SPI_IOC_WR_MODE = 0x40016B01
SPI_IOC_WR_BITS = 0x40016B03
SPI_IOC_WR_SPEED = 0x40046B04
GPIO_GET_LINEHANDLE = 0xC16CB403          # _IOWR(0xB4, 3, struct gpiohandle_request) = 364 bytes
GPIOHANDLE_GET_VALUES = 0xC040B408
GPIOHANDLE_SET_VALUES = 0xC040B409
GPIOHANDLE_REQUEST_INPUT = 1 << 0
GPIOHANDLE_REQUEST_OUTPUT = 1 << 1

# --- LoRa-parameters ------------------------------------------------------
BW_CODE = {7.8: 0x00, 10.4: 0x08, 15.6: 0x01, 20.8: 0x09, 31.25: 0x02,
           41.7: 0x0A, 62.5: 0x03, 125: 0x04, 250: 0x05, 500: 0x06}
IRQ_TX_DONE, IRQ_RX_DONE, IRQ_PREAMBLE = 0x0001, 0x0002, 0x0004
IRQ_HEADER_VALID, IRQ_HEADER_ERR, IRQ_CRC_ERR, IRQ_TIMEOUT = 0x0010, 0x0020, 0x0040, 0x0200
IRQ_CAD_DONE, IRQ_CAD_DETECTED = 0x0080, 0x0100
IRQ_ALL = 0x03FF
DEVICE_ERRORS = {0: "RC64K_CALIB", 1: "RC13M_CALIB", 2: "PLL_CALIB", 3: "ADC_CALIB",
                 4: "IMG_CALIB", 5: "XOSC_START", 6: "PLL_LOCK", 8: "PA_RAMP"}
TCXO_VOLT = {1.6: 0x00, 1.7: 0x01, 1.8: 0x02, 2.2: 0x03, 2.4: 0x04, 2.7: 0x05, 3.0: 0x06, 3.3: 0x07}


def sync_word_reg(sw):
    """Eén-byte LoRa-syncword (0x12 privé, 0x34 LoRaWAN, 0x2B Meshtastic) -> registerwaarde."""
    return bytes([(sw & 0xF0) | 0x04, ((sw & 0x0F) << 4) | 0x04])


def airtime_ms(payload_len, sf, bw_khz, cr=1, preamble=8, crc=True, explicit=True, ldro=None):
    """Zendduur van één pakket volgens Semtech AN1200.13; nodig voor de duty-cycle-boekhouding."""
    if ldro is None:
        ldro = (2 ** sf) / bw_khz > 16
    t_sym = (2 ** sf) / bw_khz                       # ms
    de = 1 if ldro else 0
    ih = 0 if explicit else 1
    num = 8 * payload_len - 4 * sf + 28 + (16 if crc else 0) - 20 * ih
    n_payload = 8 + max(math.ceil(num / (4 * (sf - 2 * de))) * (cr + 4), 0)
    if sf <= 6:
        n_payload += 2
    return (preamble + 4.25) * t_sym + n_payload * t_sym


class _Gpio:
    def __init__(self, chip, lines, output, defaults=None, label=b"roamcam-lora"):
        n = len(lines)
        offsets = list(lines) + [0] * (64 - n)
        vals = list(defaults or [0] * n) + [0] * (64 - n)
        flags = GPIOHANDLE_REQUEST_OUTPUT if output else GPIOHANDLE_REQUEST_INPUT
        req = bytearray(struct.pack("64II64B32sIi", *offsets, flags, *vals, label, n, 0))
        fd = os.open(chip, os.O_RDONLY)
        try:
            fcntl.ioctl(fd, GPIO_GET_LINEHANDLE, req)
        finally:
            os.close(fd)
        self.fd = struct.unpack_from("i", req, 360)[0]
        self.n = n

    def get(self):
        buf = bytearray(64)
        fcntl.ioctl(self.fd, GPIOHANDLE_GET_VALUES, buf)
        return list(buf[:self.n])

    def set(self, values):
        buf = bytearray(64)
        buf[:len(values)] = bytes(values)
        fcntl.ioctl(self.fd, GPIOHANDLE_SET_VALUES, buf)

    def close(self):
        os.close(self.fd)


class SX1262:
    def __init__(self, spi_hz=2_000_000):
        self.spi_hz = spi_hz
        self.spi = os.open(SPIDEV, os.O_RDWR)
        fcntl.ioctl(self.spi, SPI_IOC_WR_MODE, struct.pack("B", 0))
        fcntl.ioctl(self.spi, SPI_IOC_WR_BITS, struct.pack("B", 8))
        fcntl.ioctl(self.spi, SPI_IOC_WR_SPEED, struct.pack("I", spi_hz))
        # De vendor-datalogger exporteert via sysfs een reeks GPIO's, ook onze LoRa-pinnen,
        # en die claim blijft staan nadat hij stopt. Eerst vrijgeven.
        for pin in (PIN_CS, PIN_NRST, PIN_BUSY, PIN_DIO1):
            if os.path.exists("/sys/class/gpio/gpio%d" % pin):
                try:
                    with open("/sys/class/gpio/unexport", "w") as f:
                        f.write(str(pin))
                except OSError:
                    pass
        self.out = _Gpio(GPIOCHIP, [PIN_CS, PIN_NRST], True, [1, 1])   # CS en NRST inactief hoog
        self.inp = _Gpio(GPIOCHIP, [PIN_BUSY, PIN_DIO1], False)
        self.cfg = {}
        self.tcxo = None

    def close(self):
        for h in (self.out, self.inp):
            h.close()
        os.close(self.spi)

    # --- laag niveau ---------------------------------------------------
    def busy(self):
        return self.inp.get()[0] == 1

    def dio1(self):
        return self.inp.get()[1] == 1

    def wait_busy(self, timeout=1.0):
        end = time.monotonic() + timeout
        while self.busy():
            if time.monotonic() > end:
                raise TimeoutError("SX1262 BUSY blijft hoog")
            time.sleep(0.0005)

    def xfer(self, tx):
        self.wait_busy()
        tx = bytes(tx)
        b_tx = array.array("B", tx)
        b_rx = array.array("B", bytes(len(tx)))
        req = struct.pack("QQIIHBBBBH", b_tx.buffer_info()[0], b_rx.buffer_info()[0],
                          len(tx), self.spi_hz, 0, 8, 0, 0, 0, 0)
        self.out.set([0, 1])                       # CS laag, NRST hoog
        try:
            fcntl.ioctl(self.spi, SPI_IOC_MESSAGE_1, req)
        finally:
            self.out.set([1, 1])
        return bytes(b_rx)

    def cmd(self, op, *args, read=0):
        r = self.xfer([op, *args] + [0] * read)
        return r[len(args) + 1:] if read else r[1:2]

    def write_reg(self, addr, data):
        self.xfer([0x0D, addr >> 8, addr & 0xFF, *data])

    def read_reg(self, addr, n=1):
        return self.xfer([0x1D, addr >> 8, addr & 0xFF, 0] + [0] * n)[4:]

    # --- toestand ------------------------------------------------------
    def reset(self):
        self.out.set([1, 0])
        time.sleep(0.002)
        self.out.set([1, 1])
        time.sleep(0.01)
        self.wait_busy(2.0)

    def status(self):
        s = self.xfer([0xC0, 0])[1]
        return {"raw": s, "chipmode": (s >> 4) & 7, "cmdstatus": (s >> 1) & 7}

    def errors(self):
        r = self.xfer([0x17, 0, 0, 0])
        v = (r[2] << 8) | r[3]
        return v, [name for bit, name in DEVICE_ERRORS.items() if v & (1 << bit)]

    def clear_errors(self):
        self.xfer([0x07, 0, 0])

    def standby(self, xosc=False):
        self.cmd(0x80, 1 if xosc else 0)

    def begin(self, tcxo=None):
        """Reset + basisinstelling. tcxo=None -> automatisch: eerst kristal, bij XOSC/PLL-fout TCXO 1.8 V."""
        for attempt in ([tcxo] if tcxo is not None else [0, 1.8, 3.3]):
            self.reset()
            self.standby()
            if attempt:
                self.cmd(0x97, TCXO_VOLT[attempt], 0x00, 0x01, 0x40)   # ~5 ms opstarttijd
            self.cmd(0x96, 0x01)                   # DC-DC regelaar
            self.cmd(0x9D, 0x01)                   # DIO2 schakelt de RF-switch
            self.clear_errors()
            self.cmd(0x89, 0x7F)                   # alles kalibreren
            time.sleep(0.01)
            self.wait_busy()
            self.cmd(0x8A, 0x01)                   # pakkettype LoRa
            self.cmd(0x8F, 0x00, 0x00)             # TX- en RX-buffer beide vanaf 0
            val, names = self.errors()
            if not ({"XOSC_START", "PLL_CALIB", "PLL_LOCK"} & set(names)):
                self.tcxo = attempt or None
                return {"tcxo": self.tcxo, "errors": names}
        raise RuntimeError("SX1262 kalibratie mislukt: %s" % names)

    # --- configuratie --------------------------------------------------
    def configure(self, freq_mhz=869.525, sf=9, bw=125, cr=1, power_dbm=14, sync=0x12,
                  preamble=16, crc=True, invert_iq=False, max_payload=255, rx_boost=True):
        f_hz = int(round(freq_mhz * 1e6))
        # beeldkalibratie voor de band waar we zitten
        if 863e6 <= f_hz <= 870e6:
            self.cmd(0x98, 0xD7, 0xDB)
        elif 902e6 <= f_hz <= 928e6:
            self.cmd(0x98, 0xE1, 0xE9)
        elif 430e6 <= f_hz <= 440e6:
            self.cmd(0x98, 0x6B, 0x6F)
        frf = int(f_hz * (1 << 25) / 32_000_000)
        self.cmd(0x86, (frf >> 24) & 0xFF, (frf >> 16) & 0xFF, (frf >> 8) & 0xFF, frf & 0xFF)
        power_dbm = max(-9, min(22, int(power_dbm)))
        # PA-tabel uit de datasheet (hoogste efficiëntie per vermogensklasse)
        if power_dbm > 20:
            pa = (0x04, 0x07)
        elif power_dbm > 17:
            pa = (0x03, 0x05)
        elif power_dbm > 14:
            pa = (0x02, 0x03)
        else:
            pa = (0x02, 0x02)
        self.cmd(0x95, pa[0], pa[1], 0x00, 0x01)
        # stroombegrenzing PA (eenheden 2,5 mA; 0x38 = 140 mA, 0x3F = 157,5 mA = maximum)
        self.write_reg(0x08E7, [int(os.environ.get("SX_OCP", "0x38"), 0)])
        self.cmd(0x8E, power_dbm & 0xFF, 0x04)    # 200 µs ramp
        bw_code = BW_CODE[bw]
        ldro = 1 if (2 ** sf) / bw > 16 else 0
        self.cmd(0x8B, sf, bw_code, cr, ldro)
        self._packet_params(preamble, max_payload, crc, invert_iq)
        # errata 15.4: IQ-polariteit
        reg = self.read_reg(0x0736)[0]
        self.write_reg(0x0736, [(reg & ~0x04) if invert_iq else (reg | 0x04)])
        # errata 15.1: 500 kHz-modulatie
        reg = self.read_reg(0x0889)[0]
        self.write_reg(0x0889, [(reg & ~0x04) if bw == 500 else (reg | 0x04)])
        self.write_reg(0x0740, sync_word_reg(sync))
        if rx_boost:
            self.write_reg(0x08AC, [0x96])
        self.cmd(0x08, IRQ_ALL >> 8, IRQ_ALL & 0xFF, IRQ_ALL >> 8, IRQ_ALL & 0xFF, 0, 0, 0, 0)
        self.cfg = dict(freq_mhz=freq_mhz, sf=sf, bw=bw, cr=cr, power_dbm=power_dbm, sync=sync,
                        preamble=preamble, crc=crc, invert_iq=invert_iq)
        return self.cfg

    def _packet_params(self, preamble, length, crc, invert_iq):
        self.cmd(0x8C, preamble >> 8, preamble & 0xFF, 0x00, length & 0xFF,
                 1 if crc else 0, 1 if invert_iq else 0)

    def irq(self):
        r = self.xfer([0x12, 0, 0, 0])
        return (r[2] << 8) | r[3]

    def clear_irq(self, mask=IRQ_ALL):
        self.cmd(0x02, mask >> 8, mask & 0xFF)

    # --- ontvangen -----------------------------------------------------
    def rx_continuous(self):
        self.clear_irq()
        self._packet_params(self.cfg.get("preamble", 16), 255, self.cfg.get("crc", True),
                            self.cfg.get("invert_iq", False))
        self.cmd(0x82, 0xFF, 0xFF, 0xFF)

    def read_packet(self):
        """Na RX_DONE: payload + signaalinfo. None als er (nog) niets is."""
        flags = self.irq()
        if not flags & (IRQ_RX_DONE | IRQ_HEADER_ERR | IRQ_CRC_ERR):
            return None
        self.clear_irq()
        if flags & IRQ_HEADER_ERR and not flags & IRQ_RX_DONE:
            return {"error": "header", "irq": flags}
        r = self.xfer([0x13, 0, 0, 0])
        length, start = r[2], r[3]
        data = self.xfer([0x1E, start, 0] + [0] * length)[3:] if length else b""
        ps = self.xfer([0x14, 0, 0, 0, 0])
        rssi = -ps[2] / 2
        snr = struct.unpack("b", bytes([ps[3]]))[0] / 4
        sig = -ps[4] / 2
        # frequentiefout t.o.v. de zender (register FreqError, 20 bit signed; Semtech-formule)
        fe = self.read_reg(0x076B, 3)
        raw = ((fe[0] << 16) | (fe[1] << 8) | fe[2]) & 0xFFFFF
        if raw & 0x80000:
            raw -= 0x100000
        bw = self.cfg.get("bw", 125)
        fe_hz = round(1.55 * raw / (1600.0 / bw))
        return {"data": bytes(data), "rssi": rssi, "snr": snr, "signal_rssi": sig,
                "freq_err_hz": fe_hz, "crc_ok": not flags & IRQ_CRC_ERR, "irq": flags}

    def rssi_now(self):
        return -self.xfer([0x15, 0, 0])[2] / 2

    # --- zenden --------------------------------------------------------
    def send(self, payload, timeout=10.0):
        payload = bytes(payload)
        if not 0 < len(payload) <= 255:
            raise ValueError("payload 1..255 bytes")
        self.standby()
        self._packet_params(self.cfg.get("preamble", 16), len(payload), self.cfg.get("crc", True),
                            self.cfg.get("invert_iq", False))
        for i in range(0, len(payload), 200):
            self.xfer([0x0E, i] + list(payload[i:i + 200]))
        self.clear_irq()
        t0 = time.monotonic()
        self.cmd(0x83, 0, 0, 0)                   # TX zonder chip-timeout
        end = t0 + timeout
        while time.monotonic() < end:
            if self.irq() & IRQ_TX_DONE:
                self.clear_irq()
                return (time.monotonic() - t0) * 1000
            time.sleep(0.002)
        self.standby()
        raise TimeoutError("TX_DONE bleef uit")

    # --- snel wisselen + channel activity detection ----------------------
    def retune(self, freq_mhz, sf, bw, sync, invert_iq=False, preamble=8):
        """Lichte herconfiguratie voor de scanner: geen beeldkalibratie, geen PA-instelling."""
        frf = int(freq_mhz * 1e6 * (1 << 25) / 32_000_000)
        self.cmd(0x86, (frf >> 24) & 0xFF, (frf >> 16) & 0xFF, (frf >> 8) & 0xFF, frf & 0xFF)
        ldro = 1 if (2 ** sf) / bw > 16 else 0
        self.cmd(0x8B, sf, BW_CODE[bw], 1, ldro)
        self._packet_params(preamble, 255, True, invert_iq)
        reg = self.read_reg(0x0736)[0]
        want = (reg & ~0x04) if invert_iq else (reg | 0x04)
        if want != reg:
            self.write_reg(0x0736, [want])
        self.write_reg(0x0740, sync_word_reg(sync))
        self.cfg.update(freq_mhz=freq_mhz, sf=sf, bw=bw, sync=sync, invert_iq=invert_iq, preamble=preamble)

    # Semtech AN1200.48: aanbevolen CAD-drempels per SF (symbolen, detPeak, detMin)
    CAD_PARAMS = {5: (2, 22, 10), 6: (2, 22, 10), 7: (2, 22, 10), 8: (2, 22, 10), 9: (4, 23, 10),
                  10: (4, 24, 10), 11: (4, 25, 10), 12: (4, 28, 10)}

    def cad(self, rx_timeout_ms=0):
        """Start CAD op het huidige kanaal. Met rx_timeout_ms > 0 gaat de chip bij een treffer
        direct in RX (CAD_RX), zodat het pakket zelf ook binnenkomt."""
        sym, peak, mn = self.CAD_PARAMS[self.cfg["sf"]]
        code = {1: 0, 2: 1, 4: 2, 8: 3, 16: 4}[sym]
        t = int(rx_timeout_ms * 64)               # eenheden van 15,625 µs
        self.cmd(0x88, code, peak, mn, 1 if rx_timeout_ms else 0, (t >> 16) & 0xFF, (t >> 8) & 0xFF, t & 0xFF)
        self.clear_irq()
        self.cmd(0xC5)
        return sym * (2 ** self.cfg["sf"]) / self.cfg["bw"]   # verwachte CAD-duur in ms

    def rssi_scan(self, freqs_mhz, dwell=0.02, samples=5):
        """Kanaalactiviteit: per frequentie korte RX + momentane RSSI (poor man's spectrum)."""
        out = []
        for f in freqs_mhz:
            frf = int(f * 1e6 * (1 << 25) / 32_000_000)
            self.standby()
            self.cmd(0x86, (frf >> 24) & 0xFF, (frf >> 16) & 0xFF, (frf >> 8) & 0xFF, frf & 0xFF)
            self.cmd(0x82, 0xFF, 0xFF, 0xFF)
            time.sleep(dwell)
            vals = []
            for _ in range(samples):
                vals.append(self.rssi_now())
                time.sleep(0.002)
            out.append((f, max(vals), sum(vals) / len(vals)))
        self.standby()
        if self.cfg:
            self.configure(**self.cfg)
        return out
