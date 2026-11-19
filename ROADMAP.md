# Roadmap

Where each role stands and what it still needs. Nothing here is a promise with a date on it.

> Looking for the wilder stuff? [docs/IDEAS.md](docs/IDEAS.md) collects what this hardware turns
> out to be *capable* of — pre-impact footage from the RAM buffer, a USB webcam mode, recording to
> a stick. This page is what we intend to build; that page is what is possible.

---

## 🚗 Trip — done

Working and verified on hardware. Remaining polish:

- [ ] English interface (currently Dutch only)
- [ ] Show where a trip started and ended by name, not just coordinates
- [ ] Choose full 4056×2048 per trip instead of the 2028×1024 RAM stream (~30 MB/km, so it needs a
      storage guard and a warning)
- [ ] Music or a title card on the assembled film
- [ ] Faster GPS cold start — see below

## 📡 Base station — built; the radio works, the range does not

A fixed unit at home that receives from the car over LoRa. Code in [`roles/base/`](roles/base).

Done and measured on two HDCs:

- [x] Own SX1262 driver, no LoRaWAN stack in the way. `lorawan-logger` masked; the vendor
      `datalogger` also claims the LoRa GPIOs through sysfs, so unexport them first
- [x] Link proven both directions, 100% reception across SF7–SF12 at BW125/250
- [x] Encryption with authentication; duty-cycle enforcement, demonstrably refusing to transmit
      when the 1% budget is spent
- [x] 21 dBm is the practical maximum — 22 dBm measures ~3 dB weaker (PA saturation)
- [x] Combi mode: guard the own channel while scanning in between
- [x] Range test with GPS positions, RSSI/SNR log and a map; per-unit LED feedback
- [x] Own Wi-Fi handling so the unit sits on the home network instead of its own access point

Open, and this is the one that matters:

- [ ] **Find out why the range collapses.** Furthest answered ping in a car: 21 m, with ~70 dB of
      margin at that distance. Not a power or sensitivity problem; cause unknown. Next test is the
      cheap one: walk away on foot in 25 m steps and watch LED 2
- [ ] Decode a packet from a foreign LoRa device — never managed yet
- [ ] Get an answer from a real Meshtastic node — 446 greetings, zero replies. Unit-to-unit works,
      interop is unproven
- [ ] MQTT out to Home Assistant, with discovery so entities appear by themselves
- [ ] Decide what the car should actually send in a few dozen bytes: position, speed, ignition
      state, a "coming home" ping
- [ ] Anonymise the Meshtastic greeting: a neutral name and a random node ID per trip

What this already settled: LoRa had never demonstrably worked on either radio backend, and the
reason turned out to be exactly what we suspected — there was never a second node to talk to. Two
of our own units removed every variable, and the radio answered immediately.

## 🏠 Home Assistant camera — designed, not built

- [ ] `/snapshot.jpg` and a stable MJPEG endpoint Home Assistant can consume as a generic camera
- [ ] Motion detection on the existing RAM frame stream (frame differencing; no decode needed if
      done on downscaled JPEGs)
- [ ] Snapshot on motion, pushed to Home Assistant
- [ ] Licence plates using the `n800_*_float16.tflite` models already on the device. Realistic on a
      static scene at a known distance; do not promise this from a moving car
- [ ] Sensible retention so a garden camera doesn't fill 28 GB with photos of a hedge

## 🔙 Rear camera — designed, not built

- [ ] Join the car unit's access point as a client (the Wi-Fi chip cannot do AP and station at the
      same time, so the rear unit is the client and the front unit stays the AP)
- [ ] Push frames and trip events to the front unit
- [ ] One dashboard, two views, one trip list

---

## Cross-cutting

### Faster GPS cold start

Every real power cycle is a ~30 second cold start, because the GNSS chip has no backup power. The
lever is fresh orbit data, not remembered position.

- [ ] Refresh `mgaoffline.ubx` automatically whenever the camera has internet. Valid up to five
      weeks, so standing on the driveway once a month is enough — no manual step
- [ ] Inject last known position at start-up as a small extra. Modest gain, but it helps most
      exactly where you need it: under trees, in a car park
- [ ] Measure it properly, with the plug. A software reboot proves nothing here

### Shared core

Extract the common parts — RAM frame buffer access, GPS/IMU reading, config storage, autostart, the
web shell — once the second role exists and there is something real to share. Not before.

The installer currently hard-codes the trip role's filenames rather than treating roles
generically. Same reasoning: it gets generalised alongside the core, when there is a second role to
generalise *against*. Changing the deployed filenames now would mean re-testing a working install
on hardware for no gain.

### Wi-Fi

Scan-first switching between the device's own access point and a home network, with a client-count
check so it never drops out from under you, and an automatic return to the access point if the home
network disappears. Built and largely tested in the Roamcam repo; needs porting here.

### Roles together

The roles are not mutually exclusive and shouldn't be built as if they were. A fixed unit at home
can be base station *and* Home Assistant camera at once; a car unit can be trip recorder *and*
front half of a two-camera pair. The interface should offer functions to switch on, not one
exclusive choice.
