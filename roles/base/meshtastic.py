"""Meshtastic op de SX1262: openbaar LongFast-kanaal (EU_868). AES-128 overgenomen uit Roamcam (stdlib)."""
_SBOX = [
0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16]
_RCON = [0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1b,0x36]


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11b
    return a & 0xff


def _key_expansion(key):
    Nk = 4
    w = [list(key[4*i:4*i+4]) for i in range(Nk)]
    for i in range(Nk, 44):
        temp = list(w[i - 1])
        if i % Nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // Nk - 1]
        w.append([w[i - Nk][j] ^ temp[j] for j in range(4)])
    return w


def aes128_encrypt_block(key, block16):
    w = _key_expansion(key)
    state = [[block16[r + 4 * c] for c in range(4)] for r in range(4)]

    def add_round_key(state, rk):
        for c in range(4):
            for r in range(4):
                state[r][c] ^= rk[c][r]

    def sub_bytes(state):
        for r in range(4):
            for c in range(4):
                state[r][c] = _SBOX[state[r][c]]

    def shift_rows(state):
        for r in range(1, 4):
            state[r] = state[r][r:] + state[r][:r]

    def mix_columns(state):
        for c in range(4):
            a = [state[r][c] for r in range(4)]
            state[0][c] = _xtime(a[0]) ^ (_xtime(a[1]) ^ a[1]) ^ a[2] ^ a[3]
            state[1][c] = a[0] ^ _xtime(a[1]) ^ (_xtime(a[2]) ^ a[2]) ^ a[3]
            state[2][c] = a[0] ^ a[1] ^ _xtime(a[2]) ^ (_xtime(a[3]) ^ a[3])
            state[3][c] = (_xtime(a[0]) ^ a[0]) ^ a[1] ^ a[2] ^ _xtime(a[3])

    add_round_key(state, w[0:4])
    for rnd in range(1, 10):
        sub_bytes(state); shift_rows(state); mix_columns(state)
        add_round_key(state, w[4*rnd:4*rnd+4])
    sub_bytes(state); shift_rows(state)
    add_round_key(state, w[40:44])

    out = bytearray(16)
    for c in range(4):
        for r in range(4):
            out[r + 4*c] = state[r][c]
    return bytes(out)




# ---------------------------------------------------------------------------------------------
# Meshtastic over de lucht (EU_868, LongFast), alleen stdlib.
# Pakket: 16 bytes header (little endian) + AES-128-CTR versleutelde protobuf `Data`.
#   dest u32 | sender u32 | id u32 | flags u8 (hop_limit 0-2, want_ack 3, via_mqtt 4, hop_start 5-7)
#   | channel-hash u8 | next_hop u8 | relay_node u8
# Nonce: packet-id (u64 LE) + sender (u32 LE) + 4 nullen. Openbaar standaardkanaal "LongFast"
# gebruikt de bekende standaardsleutel (PSK "AQ==").
# ---------------------------------------------------------------------------------------------
import os
import struct

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


def aes_ctr(key, nonce16, data):
    out = bytearray()
    ctr = int.from_bytes(nonce16, "big")
    for i in range(0, len(data), 16):
        ks = aes128_encrypt_block(key, ctr.to_bytes(16, "big"))
        out += bytes(a ^ b for a, b in zip(data[i:i + 16], ks))
        ctr = (ctr + 1) & ((1 << 128) - 1)
    return bytes(out)


def nonce(packet_id, sender):
    return struct.pack("<QI", packet_id, sender) + bytes(4)


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
    flags = (hop_limit & 7) | ((hop_limit & 7) << 5)
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
