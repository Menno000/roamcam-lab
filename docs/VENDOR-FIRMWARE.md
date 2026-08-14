# What the stock firmware already gives you

This is the reverse-engineering write-up that everything in this repo is built on. If you own a
Hivemapper HDC and want to build your own thing on it, this page is probably worth more to you than
the code.

All of it was verified on a real HDC (`dashcam-2d43e58f`, CM4, firmware with `dashcam-api.js`
dated Dec 2024) in August 2026. Where something is untested, it says so.

---

## The big one: don't fight the camera, read along

The stock firmware runs `libcamera-bridge`, which continuously encodes **10 fps MJPEG at
2028×1024** and writes every frame as a separate JPEG into `/tmp/recording/pic/` — a tmpfs, so
this is RAM, not flash wear.

A vendor binary called `folder-purger` caps that directory at 400 MB, which works out to roughly
**100 seconds of rolling image history** sitting there at all times.

That means: if you want live video or stills, you do **not** need to open the camera. Take the
newest file. The frames are already encoded, so it costs you no CPU at all — only the bytes you
send over the network.

```sh
ls -t /tmp/recording/pic/*.jpg | head -1     # newest frame, right now
```

Practical details that will bite you otherwise:

- Filenames are `<10-digit epoch>_<6-digit microseconds>.jpg`, so a plain alphabetical sort is
  also a chronological sort. No `stat()` calls needed.
- The bridge writes `xxx.jpg.tmp` first and renames afterwards. Filter on `.jpg` and you always
  get a complete file — no torn reads.
- There is often a `latest.txt` holding the newest filename, which saves you listing a
  thousand-entry directory.

### Changing the camera settings

The bridge is started as:

```
libcamera-bridge --config camera_bridge_config.json \
                 --config-override /mnt/data/camera_bridge_config.json \
                 --segment 0 --timeout 0 --tuning-file imx477.json
```

That override path is a first-class vendor feature (`CAMERA_BRIDGE_CONFIG_FILE_OVERRIDE` in
`dashcam-api.js`). The file does not exist by default — create it and you can change resolution,
rotation, region of interest and output path **without touching the read-only root filesystem**.
Apply with `systemctl restart camera-bridge`.

The stock config is worth reading in full (`/opt/dashcam/bin/camera_bridge_config.json`). The
interesting parts:

| Setting | Stock value | Meaning |
|---|---|---|
| `encoding.fps` | 10 | frames per second |
| `encoding.width/height` | 4056×2048 | full capture size (8.3 MP) |
| `scale_width/height` | 2028×1024 | the downsampled stream you get in RAM |
| `adjustment.rotation` | 180 | the sensor is mounted upside down |
| `adjustment.roi` | `0,496,4056,2048` | 2:1 crop out of the 4056×3040 sensor |
| `directory.output2` | `/media/usb0/recording/` | where full-size frames go if a USB stick is present |
| `skip_4k` | true | |

---

## GPS and IMU, already parsed for you

The `datalogger` binary owns the GNSS receiver and writes a complete, parsed fix to
`/mnt/data/gps/latest.log` **twice per second**:

```json
{"fix":"3D","latitude":45.9330492,"longitude":8.4836437,"height":231.8,
 "heading":230.78,"speed":0.311,"horizontal_accuracy":1.53,
 "satellites":{"seen":29,"used":8},"time_resolved":1,
 "timestamp":"2026-08-14T10:49:20.944Z","dop":{"hdop":1.56,...},"gga":"$GNGGA,..."}
```

The IMU lands in `/mnt/data/imu/latest.log` the same way (accelerometer, gyro, temperature).

So there is no reason to open a serial port or speak UBX yourself just to know where you are.

> **⚠️ `latest.log` survives a power cut.** Right after boot, before the datalogger has written
> anything, you are reading the position and time from *before the device was last switched off*.
> This cost us a clock that was 25 seconds wrong. Read the file twice a second apart and require
> the `timestamp` field to change before you trust it.

---

## Only `/mnt/data` survives a reboot

The root filesystem is an overlay: a squashfs lower layer (`/dev/mmcblk0p3`, mounted at `/ro`)
with a **RAM** upper layer, set up by `/opt/dashcam/bin/overlayroot.sh`. Its own header says it
plainly: *changes made anywhere in the root filesystem are lost on reboot.*

Consequences:

- A systemd unit in `/etc/systemd/system` will be gone after a power cycle. Don't bother.
- `/etc/hostapd.conf` is regenerated from the CPU serial at every boot by `set_hostname.sh`, so a
  custom Wi-Fi name has to be re-applied each time.
- `/mnt/data` (ext4 on `/dev/mmcblk0p4`, ~28 GB) is the only place anything persists.

---

## Autostart: `/mnt/data/cron_config`

`dashcam-api.js` reads this JSON file and schedules the jobs in it. Commands go through
`child_process.spawn(cmd, { shell: true })`, so arbitrary shell is allowed.

```json
[{"id":"my_thing","log":true,"cmd":"sh /mnt/data/my_thing.sh","frequency":{}},
 {"id":"my_watchdog","log":true,"cmd":"sh /mnt/data/my_thing.sh","frequency":{"interval":60000}}]
```

Frequency options, from the bundled source:

| `frequency` | Behaviour |
|---|---|
| `{}` | runs once, immediately, when jobs are scheduled at boot |
| `{"interval": ms}` | repeats forever — good for a watchdog |
| `{"delay": ms}` | runs once after a delay |
| `{"oncePerDevice": true}` | runs once ever; remembered in `/mnt/data/cron_executed` |
| `+ "executeOnce": true` | with an interval, cancels itself after the first run |

Other fields: `timeout` (ms, kills the child), `log`, `if` (run a command and test its output with
`contains` / `equals` / `greaterThan`), and `device`.

> **Leave `device` out.** It is checked as `if (config.device && config.device !== CAMERA_TYPE)
> return;` — set it to the wrong string and your job silently never runs. Omitting it skips the
> check entirely, which is what you want if you don't know whether you're on an HDC or HDC-S.

Two caveats:

- The vendor rewrites this file. An entry we added earlier had disappeared by the next session.
  A plain reboot does preserve added entries — that we tested — but treat it as a place the vendor
  may reset, not as your own config file.
- New entries are only picked up when the API restarts: `systemctl restart camera-node`, or a
  reboot.

Measured: a job with `frequency: {}` runs about **23 seconds after power-on**.

---

## GNSS: why your first fix is slow, and what you can do

The receiver is a **u-blox M9** (protocol version 32.01) on `/dev/ttyAMA1` at 38400 baud.

- **`datalogger` holds that port.** You can still write to it alongside, but you cannot reliably
  read replies — the datalogger consumes them. That rules out anything needing a response, such as
  polling the navigation database with `UBX-MGA-DBD` for a true hot start.
- **There is no I²C path to the module.** Address `0x42` was probed on buses 0, 1, 10 and 22:
  all return `Remote I/O error`. The UART is the only channel.
- **The GNSS chip has no backup power.** A *reboot* keeps it powered, so it re-fixes within a
  second — meaning a software reboot tells you nothing about real start-up time. A genuine power
  cut, which is what happens in a car, wipes ephemeris, almanac, time and position: a ~30 second
  cold start.

The stock firmware's answer is AssistNow Offline. `datalogger` is launched with
`--gnss-mga-offline-file-path /mnt/data/mgaoffline.ubx` and pushes that file to the receiver at
boot — predicted orbits, exactly what a cold start is missing.

`gps_assist_now.py` shows how simple the push is: split the file on the UBX preamble `b5 62`, send
each message raw, after enabling `CFG-NAVSPG-ACKAIDING`. That also suggests you could append your
own UBX messages to that file and have the vendor deliver them at the ideal moment — *untested.*

**Check whether yours has expired.** MGA-ANO records are class `0x13`, id `0x20`, with the date in
payload bytes 4, 5 and 6 as year-2000 / month / day. On our unit the file covered 9 Feb – 9 Mar
2026 and was therefore contributing nothing at all by August.

Don't bother with the `ttff` field in `latest.log` — it reads as int64-max, i.e. never measured.

---

## Everything else that's already running

| Service / binary | What it gives you |
|---|---|
| `avahi-daemon` | mDNS works out of the box — `<hostname>.local` resolves, no setup |
| `led-controller` | LEDs are controllable, config at `/opt/dashcam/led-controller-config.txt` |
| `usb-write.py`, `usb-update.service` | the USB-C port is a **host** port, intended for a stick to record onto |
| `folder-purger` | size-capped directory cleanup, already solved |
| `lorawan-logger` | holds the SX1262 LoRa radio — kill it first if you want the radio |
| `n800_*_float16.tflite` | object-detection models ship on the device |
| `wifiman.service` | reads `/mnt/data/wifi.cfg` (`AP,` = access point mode) |
| `datalogger` HTTP | listens on `:9000` and `:9001`; `/` returns nothing, endpoints unexplored |

---

## Gotchas worth knowing before you start

**There is no RTC battery.** The device boots believing it is December 2022 and jumps years
forward the moment GPS sets the clock. Anything that measures *elapsed* time must use a monotonic
clock. We learned this when a recording session closed itself one second after starting: the
wall-clock jump convinced the idle timer that the car had been parked for three and a half years.

**A reboot is not a power cycle.** Peripherals that keep their state across a warm reboot — the
GNSS module especially — will behave completely differently when the ignition actually cuts power.
Test with the plug, not with `reboot`.

**Root SSH is open by default** on these cameras. That is what makes all of this possible, and
also what you should think about before putting one on a network you don't control.
