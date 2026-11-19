#!/bin/sh
# Roamcam Lab installer — run this ON a Hivemapper HDC, as root.
#
#   scp -r roles install.sh root@192.168.0.10:/tmp/
#   ssh root@192.168.0.10 'cd /tmp && sh install.sh'
#
# Pass a role to skip the question:  sh install.sh trip
#
set -e
SRC="$(dirname "$0")"
DATA=/mnt/data
ROLE="$1"

echo "== Roamcam Lab installer =="

if [ ! -d "$DATA" ]; then
  echo "ERROR: $DATA not found. This does not look like a Hivemapper HDC." >&2
  exit 1
fi

# ---- which role
if [ -z "$ROLE" ]; then
  echo ""
  echo "  Which role should this camera take?"
  echo "    1) trip          road-trip recorder, a photo every N metres"
  echo "    (other roles are not built yet — see ROADMAP.md)"
  echo ""
  printf "  Choice [1]: "
  read -r answer
  case "${answer:-1}" in
    1|trip) ROLE=trip ;;
    *) echo "ERROR: unknown choice '$answer'." >&2; exit 1 ;;
  esac
fi

if [ ! -f "$SRC/roles/$ROLE/travel_server.py" ]; then
  echo "ERROR: roles/$ROLE/travel_server.py not found next to install.sh." >&2
  exit 1
fi
if [ ! -f "$SRC/roles/core/roamcam_core.py" ]; then
  echo "ERROR: roles/core/roamcam_core.py not found. Every role needs the shared core." >&2
  exit 1
fi

# ---- the app and the shared core -> persistent storage. Only /mnt/data survives a reboot on
#      this device; everything else is a RAM overlay. The core goes in /mnt/data because that
#      is the first place every role looks for it.
cp "$SRC/roles/core/roamcam_core.py" "$DATA/roamcam_core.py"
cp "$SRC/roles/$ROLE/travel_server.py" "$DATA/travel_server.py"
cp "$SRC/roles/$ROLE/roamcam_travel.sh" "$DATA/roamcam_travel.sh"
chmod +x "$DATA/roamcam_travel.sh"
echo "  installed role '$ROLE' + roamcam_core.py"

# ---- autostart. The firmware reads /mnt/data/cron_config at boot and runs what's listed.
#      One entry starts it immediately, one acts as a watchdog every 60s.
#      Note: no "device" key. It is checked against the camera type and a wrong value makes
#      the job silently never run; leaving it out skips the check.
python3 - <<'PY'
import json
p = "/mnt/data/cron_config"
try:
    jobs = json.load(open(p))
    if not isinstance(jobs, list):
        jobs = []
except Exception:
    jobs = []
jobs = [j for j in jobs if not str(j.get("id", "")).startswith("roamcam_")]
jobs.append({"id": "roamcam_travel_boot", "log": True,
             "cmd": "sh /mnt/data/roamcam_travel.sh", "frequency": {}})
jobs.append({"id": "roamcam_travel_watch", "log": True,
             "cmd": "sh /mnt/data/roamcam_travel.sh", "frequency": {"interval": 60000}})
json.dump(jobs, open(p, "w"))
print("  registered autostart in cron_config")
PY

# ---- start now, and reload cron_config so autostart is live without a reboot
sh "$DATA/roamcam_travel.sh"
sleep 3
systemctl restart camera-node 2>/dev/null || true

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
NAME=$(hostname 2>/dev/null)
echo ""
echo "Done."
echo "  Open:  http://${IP:-<device-ip>}:8080"
[ -n "$NAME" ] && echo "     or: http://$NAME.local:8080"
echo "  It comes back automatically after every reboot."
echo ""
echo "To remove later: sh uninstall.sh"
