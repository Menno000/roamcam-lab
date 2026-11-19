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

### 📡 Base station — *planned*

A fixed unit at home that listens for the car over LoRa and pushes what it hears into Home
Assistant. Two SX1262 radios talking to each other directly — no LoRaWAN provider, no subscription,
no coverage map. "Car is two kilometres away" is enough to open a garage door.

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

The Trip role is finished and verified on hardware: the capture loop, film assembly, GPX export,
the offline map, GPS clock sync, and surviving a reboot. The other three roles are designed but not
built.

**Known limitations, honestly:**

- The Trip interface is **Dutch only** for now. English is on the roadmap.
- The shared core hasn't been factored out yet. With one role implemented, guessing at the right
  abstraction would be premature — it gets extracted when the second role lands.
- Cold-start GPS timing has not been measured with real power cuts. See the GNSS section of
  [docs/VENDOR-FIRMWARE.md](docs/VENDOR-FIRMWARE.md) for what we know and what we don't.

---

## Related

- **[Roamcam](https://github.com/Menno000/Roamcam)** — the same hardware as a straightforward
  private dashcam. If you want a dashcam, you want that one.

## Licence

MIT — see [LICENSE](LICENSE).
