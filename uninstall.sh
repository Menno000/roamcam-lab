#!/bin/sh
# Remove Roamcam Lab from a Hivemapper HDC. Recordings and trips are kept unless you
# ask for them to go.
set -e
DATA=/mnt/data

echo "== Roamcam Lab uninstaller =="

# stop it
if ps ax 2>/dev/null | grep -q "[t]ravel_server.py"; then
  kill $(ps ax | grep "[t]ravel_server.py" | awk '{print $1}') 2>/dev/null || true
  echo "  stopped travel_server"
fi

# remove the autostart entries, leave the vendor's own jobs alone
python3 - <<'PY'
import json
p = "/mnt/data/cron_config"
try:
    jobs = json.load(open(p))
    if not isinstance(jobs, list):
        jobs = []
except Exception:
    jobs = []
kept = [j for j in jobs if not str(j.get("id", "")).startswith("roamcam_")]
json.dump(kept, open(p, "w"))
print("  removed %d autostart entries" % (len(jobs) - len(kept)))
PY

rm -f "$DATA/travel_server.py" "$DATA/roamcam_travel.sh" "$DATA/travel_stdout.log"
echo "  removed program files"

printf "  Also delete recorded trips in $DATA/travel? [y/N]: "
read -r answer
case "$answer" in
  y|Y) rm -rf "$DATA/travel"; echo "  trips deleted" ;;
  *)   echo "  trips kept in $DATA/travel" ;;
esac

systemctl restart camera-node 2>/dev/null || true
echo ""
echo "Done. The stock firmware is untouched — it was never modified."
echo ""
echo "Note: $DATA/roamcam_core.py is left in place on purpose. If Roamcam or another role is"
echo "also installed on this camera, it needs that same file. Remove it only if nothing else"
echo "uses it."
