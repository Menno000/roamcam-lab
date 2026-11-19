# Roamcam Lab

[![License](https://img.shields.io/github/license/Menno000/roamcam-lab)](LICENSE)

**Other things a Hivemapper HDC can be.**

[Roamcam](https://github.com/Menno000/Roamcam) turns one of these cameras into a private offline
dashcam. This repo is the other half of the idea: the same hardware doing something else entirely —
a trip recorder, a LoRa base station, a Home Assistant camera — with the role picked in the
interface rather than by flashing something different.

> Made in the Netherlands 🇳🇱, for anyone with one of these cameras in a drawer.

> **⚠️ Hardware:** built and tested on the **HDC** (Raspberry Pi CM4 based). Should also work on the
> **HDC-S**. The newer **Bee** is different hardware and is not supported.

---

## The idea: ride along, don't take over

Roamcam pushes the stock firmware aside and drives the camera itself. That works, but it means
fighting for the hardware, and you end up re-implementing things the vendor already does well.

Roamcam Lab does the opposite. The stock firmware keeps running, and we read along with it.

It turns out that's the better deal:

- `libcamera-bridge` already produces **10 fps MJPEG at 2028×1024** into a RAM buffer, with about
  **100 seconds of rolling history** always available. Reading the newest frame costs **zero CPU** —
  the frames are already encoded.
- The GPS is already parsed into a JSON file **twice per second**: fix, position, speed, heading,
  accuracy, satellite counts. Same for the accelerometer.
- Because we ask the camera for nothing, the stock recording keeps running untouched. Live view,
  recording and timelapse all at once, with no contention.

The full write-up of how the stock firmware works — and the traps in it — is in
**[docs/VENDOR-FIRMWARE.md](docs/VENDOR-FIRMWARE.md)**. If you're building your own thing on one of
these cameras, start there.

And **[docs/IDEAS.md](docs/IDEAS.md)** is the pile of things this hardware turns out to be capable
of that nobody has built yet — including a hundred seconds of pre-impact footage that the stock
firmware is already keeping in RAM for you. Nothing on that page is implemented; it's marked
per-item what we actually tested and what is still a guess.

---

## Roles

### 🚗 Trip — *working*

A road-trip recorder. It photographs **every N metres** rather than every N seconds, so a traffic
light doesn't eat half your film and the result plays at an even pace whether you were doing 30 or
130. Without a GPS fix it falls back to a time interval on its own.

- Big live view, distance / photo count / elapsed time, one start button
- Trip archive with an offline map, a thumbnail grid, GPX export and one-click film assembly
  (hardware H.264, 1920×970 mp4)
- Optionally starts a trip by itself when you drive off and closes it after five minutes parked
- Clock sets itself from GPS, time zone selectable
- About **7 MB per kilometre** at a 50 m interval — roughly 3000 km in the free space on a stock
  device

### 📡 Base station — *the radio works; the range does not*

A fixed unit at home that listens for the car over LoRa. Two SX1262 radios talking to each other
directly — no LoRaWAN provider, no subscription, no coverage map, nothing to sign up for.

**What is built and measured** (on two HDCs, September 2026):

- Own SX1262 driver, no vendor LoRaWAN stack in the way. 100% reception across SF7–SF12 at
  BW125/250, both directions, with measured airtime matching the formula to within 1–2 ms
- Encrypted link with authentication; a wrong key is reported as `auth=False` rather than silently
  accepted
- EU duty-cycle limits shown and enforced by default, and switchable off — your transmitter, your
  responsibility. Enforcement demonstrably works: a pong was refused when the 1% budget was spent
- Transmit power is linear from −9 to **21 dBm**. 22 dBm is reproducibly ~3 dB *weaker* than 21 —
  the PA saturates on this board, so 21 dBm is the practical maximum
- Combi mode: guards its own channel every 40–50 ms while scanning other channels in between
  (measured: 18/18 pings answered, 0.08% of guard checks late)
- Range test with GPS positions, an RSSI/SNR log and a map; LED feedback per unit
- The two units' crystals differ by about 5.35 kHz (≈6 ppm), stable — fine for BW ≥ 62.5 kHz

**What does not work yet, honestly:** range in a car. Over 643 position pings on a real drive, the
furthest answered ping was **21 metres**, and the base heard the car up to 29 m. At 21 m the signal
was −46 to −51 dBm — roughly 70 dB of margin, in theory good for kilometres — and then nothing. That
pattern does not fit shielding, which would give a weak signal everywhere rather than a strong one
with a cliff behind it. **The cause is unknown and under investigation.**

Also still unproven: the scanner has never decoded a packet from a foreign LoRa device, and 446
Meshtastic greetings produced no answer from a real Meshtastic node. Unit-to-unit works; interop
does not.

Code lives in [`roles/base/`](roles/base). Home Assistant forwarding is still to do.

### 🏠 Home Assistant camera — *planned*

Driveway or garden camera: motion detection on the existing image stream, snapshots into Home
Assistant. Licence-plate recognition is realistic *here* — a static scene at a known distance —
in a way it never is from a moving car. The device already ships with object-detection models.

### 🔙 Rear camera — *planned*

A second unit that joins the car unit's access point, so one dashboard shows both views and one
trip list.

See [ROADMAP.md](ROADMAP.md) for what each of those still needs.

---

## Install

On the camera, as root:

```sh
scp -r roles install.sh root@192.168.0.10:/tmp/
ssh root@192.168.0.10 'cd /tmp && sh install.sh'
```

The installer asks which role you want, copies it to `/mnt/data`, and registers it in the
firmware's own `cron_config` so it starts at boot and restarts itself if it ever dies. No systemd
unit — those don't survive a reboot on this device.

Then open **`http://<device-ip>:8080`**. On many of these cameras `http://<hostname>.local:8080`
also works, because `avahi-daemon` is running — but don't count on it: `avahi` appears only in the
*dev* defconfigs of the public firmware, so a camera with a different build may not have it. The IP
always works.

To remove: `sh uninstall.sh`.

---

## Status

**Trip** is finished and verified on hardware: the capture loop, film assembly, GPX export, the
offline map, GPS clock sync, and surviving a reboot.

**Base station** is built and its radio is proven on two units — but the range in a car is not
solved, and that is written up honestly above. The Home Assistant camera and the rear camera are
designed and not built.

The two roles share a core (`roles/core/roamcam_core.py`) with **46 tests that run without a
camera**. The AES-128 in it is checked against the official vectors from FIPS-197 and NIST
SP 800-38A rather than against our own earlier output, because that code encrypts real packets on
two devices.

**Known limitations, honestly:**

- The interfaces are **Dutch only** for now. English is on the roadmap.
- Range over LoRa collapses after about 20 metres in a car, for reasons we do not yet understand.
  The radio itself measures fine.
- The scanner has never decoded a packet from someone else's LoRa device, and no real Meshtastic
  node has ever answered. Unit-to-unit works; interop is unproven.
- Cold-start GPS timing has not been measured with real power cuts. See the GNSS section of
  [docs/VENDOR-FIRMWARE.md](docs/VENDOR-FIRMWARE.md) for what we know and what we don't.
- mDNS (`<hostname>.local`) works on our cameras but is not guaranteed on yours — see Install.

---

## Related

- **[Roamcam](https://github.com/Menno000/Roamcam)** — the same hardware as a straightforward
  private dashcam. If you want a dashcam, you want that one.

## Licence

MIT — see [LICENSE](LICENSE).
