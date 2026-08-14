# Ideas

Things this hardware turns out to be capable of, that nobody has built yet.

**Nothing on this page is implemented.** It is not a promise or a plan — [ROADMAP.md](../ROADMAP.md)
is where intended work lives. This is the pile of "wait, we could actually do that", written down
with the evidence for each, so the next person doesn't have to rediscover it.

Every claim below is marked:

- ✅ **verified** — we tested it on a real device
- 🤔 **plausible** — the pieces are demonstrably there, the combination is untested
- ❓ **unknown** — would need work to find out

---

## 📼 Pre-impact recording

**The best thing we found, and nobody is using it.**

The stock firmware keeps roughly **100 seconds of image history in RAM** at all times — 10 fps
JPEGs in `/tmp/recording/pic/`, size-capped by the vendor's own `folder-purger` at 400 MB.

That means a device sitting there doing nothing already *has* the last hundred seconds. If
something happens, you don't need to have been recording — you need to copy what is already in
memory.

Every consumer dashcam advertises "pre-buffer" as a premium feature and gives you 3 to 10 seconds
of it. This hardware hands you a hundred, for free, and the frames are already encoded so saving
them costs almost nothing.

Uses: impact detection, a manual "keep the last two minutes" button, motion on a parked car, or a
base station that keeps whatever happened before a doorbell rang.

- ✅ The buffer exists, is 400 MB, and turns over continuously — measured
- ✅ Frames are complete and readable (`.jpg.tmp` renamed on completion)
- 🤔 Reading ~1000 files quickly enough during an impact — trivially fast on tmpfs, but untested
  under load

## 📸 High-resolution burst on impact

The IMX477 does full **4056×3040 stills** happily via `libcamera-jpeg`, even though the hardware
H.264 encoder flatly refuses anything above about 1080p for video. So the one moment that matters
most — a collision — could be captured in far more detail than the video can ever hold. Plates,
damage, the other car.

Originally this looked awkward: only one process can own the camera, so a burst meant interrupting
the recording. The stock-firmware discovery changes that. The vendor bridge can be told to write
**4056×2048** frames itself via the config override, so the high-resolution frames would already be
there — no pause, no handover.

- ✅ 12 MP stills work; the H.264 encoder refuses ≥4K video (both tested)
- ✅ The camera config can be overridden from `/mnt/data` — vendor-supported mechanism
- 🤔 Enabling full-resolution output and reading those frames instead
- ❓ Whether writing 8 MP frames continuously is sane for the eMMC, or should be switched on only
  when something happens

## 🎥 A plain USB webcam

The kernel modules for USB gadget mode are on the device, including **`usb_f_uvc`** — the one that
makes a Linux box present itself as a standard webcam. Plug the camera into a laptop with a USB-C
cable and have it show up in any video app, no drivers, no network.

There is a real obstacle: the boot config sets `otg_mode=1`, putting the USB-C port in **host**
mode, and the OTG device tree node reads `status=disabled`. Gadget mode needs the opposite. Whether
that is a config change or a hardware limitation is the open question.

- ✅ `usb_f_uvc`, `usb_f_ncm`, `usb_f_ecm`, `usb_f_serial`, `usb_f_mass_storage` and `libcomposite`
  are all present
- ✅ The port is currently in host mode — a USB charger plugged in makes the device shut down
- ❓ Whether device/gadget mode can be enabled at all on this board

## 🔌 A network cable that is a USB cable

Same gadget machinery, different function: `usb_f_ncm` or `usb_f_ecm` would make the camera appear
as a network adapter. Plug it into a laptop, get an IP, open the dashboard — no Wi-Fi involved.

That is genuinely useful on this device, because the Wi-Fi chip cannot run its own access point and
join your network at the same time. A USB link would sidestep that entirely: keep the camera on
your home network *and* still reach it directly.

- ✅ Modules present · ❓ same host/device-mode question as above

## 💾 Recording onto a USB stick

This one the vendor already built and it is simply switched off. The stock camera config contains
`"output2": "/media/usb0/recording/"`, there is a `usb-write.py` service running, and
`/media/usb0` through `/media/usb7` are waiting. Plug in a stick and full-resolution frames have
somewhere to go — which also answers the eMMC-wear worry above.

- ✅ Config, mount points and service all confirmed present
- ❓ Not tested — we had no USB-C stick on hand

## 🚗 Licence plates, but only where it works

The device ships with object-detection models: `n800_1x2_float16.tflite` and
`n800_2x2_float16.tflite` in `/opt/dashcam/bin/`. Hivemapper uses them for blurring.

Reading plates from a *moving car* is not realistic on a CM4 — the geometry changes constantly, the
resolution at distance is marginal, and you have milliseconds. Reading plates on a **driveway** is a
different problem entirely: fixed camera, known distance, a car that stops. That is a solvable
problem, and it is the one worth attempting.

- ✅ Models are on the device
- ❓ What they actually detect, and whether anything usable can be built on them

## 💡 The LEDs as an information channel

`led-controller` runs with a config file at `/opt/dashcam/led-controller-config.txt`. Three RGB
LEDs that could mean something: GPS fix acquired, trip recording, storage nearly full, base station
in contact with the car. On a device with no screen, that is the entire user interface.

Roamcam already does per-LED functions; nothing in this repo touches them yet.

- ✅ Controller and config confirmed · 🤔 the config format itself is unexamined here

## 📡 What the car should actually say over LoRa

Once two units can talk (see the base station role), the interesting question is what is worth
sending in a few dozen bytes. Position and speed are obvious. Less obvious and more useful:
"ignition on", "arrived", "still parked and the battery is getting low", "impact detected".

A LoRa packet costs airtime, so this is a design question, not a coding one — and worth thinking
about before writing the protocol rather than after.

- ❓ Entirely open

---

## Ideas that turned out to be dead ends

Written down so nobody spends a weekend on them:

- **Rotating the image 90° or 270°** — the software rotation exists but costs far more CPU than it
  is worth on this hardware. 180° is free because it is a sensor-level flip.
- **4K video** — the hardware H.264 encoder refuses to start above roughly 1080p. Stills are fine;
  video is not.
- **AP and station Wi-Fi at the same time** — the brcmfmac43455 advertises the combination but
  returns `Operation not supported (-95)`. You switch between them; you don't have both.
- **A true GPS hot start** — would need to poll the receiver's navigation database over the serial
  port, but the vendor's datalogger owns that port and eats the replies, and there is no I²C path to
  the module. See the GNSS section of [VENDOR-FIRMWARE.md](VENDOR-FIRMWARE.md).
