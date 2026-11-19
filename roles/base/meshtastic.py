"""Meshtastic op de SX1262: openbaar LongFast-kanaal (EU_868).

De AES-128 stond hier als eigen kopie in (69 regels, identiek aan die in Roamcam).
Die staat nu in de gedeelde kern, getoetst aan de officiele FIPS-197-vectoren.
"""

import os
import struct
import sys

# De kern staat op het toestel in /mnt/data en in de repo naast deze map.
for _p in ("/mnt/data",
           os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"),
           os.path.dirname(os.path.abspath(__file__))):
    if os.path.exists(os.path.join(_p, "roamcam_core.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
import roamcam_core as core  # noqa: E402

aes128_encrypt_block = core.aes128_encrypt_block

# ---------------------------------------------------------------------------------------------
# Meshtastic over de lucht (EU_868, LongFast), alleen stdlib.
# Pakket: 16 bytes header (little endian) + AES-128-CTR versleutelde protobuf `Data`.
#   dest u32 | sender u32 | id u32 | flags u8 (hop_limit 0-2, want_ack 3, via_mqtt 4, hop_start 5-7)
#   | channel-hash u8 | next_hop u8 | relay_node u8
# Nonce: packet-id (u64 LE) + sender (u32 LE) + 4 nullen. Openbaar standaardkanaal "LongFast"
# gebruikt de bekende standaardsleutel (PSK "AQ==").
# ---------------------------------------------------------------------------------------------

DEFAULT_KEY = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")
CHANNEL_NAME = "LongFast"
BROADCAST = 0xFFFFFFFF
RADIO = dict(freq_mhz=869.525, sf=11, bw=250, cr=1, sync=0x2B, preamble=16, invert_iq=False, crc=True)
PORTS = {1: "tekst", 3: "positie", 4: "node-info", 5: "routing", 6: "admin", 32: "antwoord", 34: "paxcounter",
         64: "serieel", 65: "store&forward", 66: "range-test", 67: "telemetrie", 70: "traceroute",
         71: "buren", 73: "kaartrapport"}


def channel_hash(name=CHANNEL_NAME, key=DEFAULT_KEY):
    h = 0
    for b in name.encode() + key:
        h ^= b
    return h


aes_ctr = core.aes_ctr
nonce = core.meshtastic_nonce


# --- minimale protobuf ---
def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb_varint(field, n):
    return _varint(field << 3) + _varint(n)


def pb_bytes(field, b):
    b = b.encode() if isinstance(b, str) else b
    return _varint((field << 3) | 2) + _varint(len(b)) + b


def pb_parse(buf):
    """-> {veld: [waarden]}; gooit ValueError bij onzin (zo herkennen we een verkeerde sleutel)."""
    out, i = {}, 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        field, wt = key >> 3, key & 7
        if field == 0:
            raise ValueError("veld 0")
        if wt == 0:
            v, i = _read_varint(buf, i)
        elif wt == 1:
            v, i = buf[i:i + 8], i + 8
        elif wt == 2:
            ln, i = _read_varint(buf, i)
            v, i = buf[i:i + ln], i + ln
        elif wt == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            raise ValueError("wiretype %d" % wt)
        if i > len(buf):
            raise ValueError("te kort")
        out.setdefault(field, []).append(v)
    return out


def _read_varint(buf, i):
    n = shift = 0
    while True:
        if i >= len(buf) or shift > 63:
            raise ValueError("varint")
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, i


def _s(v):
    try:
        return v.decode()
    except (UnicodeDecodeError, AttributeError):
        return None


# --- bouwen ---
def node_num_from_mac(mac):
    """Zoals Meshtastic: laatste 4 bytes van het MAC-adres."""
    b = bytes.fromhex(mac.replace(":", ""))
    n = int.from_bytes(b[-4:], "big")
    return n if n not in (0, BROADCAST) else 0x0A0B0C0D


def build_nodeinfo(node_num, long_name, short_name, want_response=True, hop_limit=3):
    user = (pb_bytes(1, "!%08x" % node_num) + pb_bytes(2, long_name[:39]) + pb_bytes(3, short_name[:4])
            + pb_varint(5, 255))                                   # hw_model PRIVATE_HW
    data = pb_varint(1, 4) + pb_bytes(2, user) + (pb_varint(3, 1) if want_response else b"")
    pid = struct.unpack("<I", os.urandom(4))[0] | 1
    flags = (hop_limit & 7) | ((hop_limit & 7) << 5)            # hop_limit 0: alleen directe buren
    hdr = struct.pack("<IIIBBBB", BROADCAST, node_num, pid, flags, channel_hash(), 0, node_num & 0xFF)
    return hdr + aes_ctr(DEFAULT_KEY, nonce(pid, node_num), data), pid


# --- ontleden ---
def decode(pkt):
    """Header altijd; inhoud alleen als het het openbare LongFast-kanaal is."""
    if len(pkt) < 16:
        return None
    dest, sender, pid, flags, ch, next_hop, relay = struct.unpack("<IIIBBBB", pkt[:16])
    out = {"from": "!%08x" % sender, "to": "iedereen" if dest == BROADCAST else "!%08x" % dest,
           "id": pid, "hop_limit": flags & 7, "hop_start": flags >> 5, "want_ack": bool(flags & 8),
           "channel_hash": ch, "relay": relay, "public": ch == channel_hash()}
    out["hops"] = max(0, out["hop_start"] - out["hop_limit"]) if out["hop_start"] else None
    if not out["public"]:
        out["note"] = "ander kanaal of eigen sleutel: niet te lezen"
        return out
    try:
        d = pb_parse(aes_ctr(DEFAULT_KEY, nonce(pid, sender), pkt[16:]))
        port = d.get(1, [None])[0]
        out["port"] = PORTS.get(port, port)
        payload = d.get(2, [b""])[0]
        if port == 1:
            out["text"] = _s(payload)
        elif port == 4:
            u = pb_parse(payload)
            out["user"] = {"id": _s(u.get(1, [b""])[0]), "long": _s(u.get(2, [b""])[0]),
                           "short": _s(u.get(3, [b""])[0]), "hw": u.get(5, [None])[0]}
        elif port == 3:
            pp = pb_parse(payload)
            if 1 in pp and 2 in pp:
                out["pos"] = [struct.unpack("<i", pp[1][0])[0] / 1e7, struct.unpack("<i", pp[2][0])[0] / 1e7]
        out["want_response"] = bool(d.get(3, [0])[0])
    except (ValueError, IndexError, struct.error):
        out["note"] = "ontsleutelen gaf geen geldige inhoud"
    return out
