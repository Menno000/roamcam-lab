#!/bin/sh
# Wist alle rit- en gebruikersdata van de vorige eigenaar van deze HDC.
# Laat firmware, kalibratie (testBkup, eeprom), GPS-hulpbestanden, modellen en swap staan.
# Na afloop herstart het toestel.
set -u
D=/mnt/data
echo "== services stoppen"
for s in api-health-manager camera-node camera-bridge data-logger object-detection folder_purger usb-write; do
  systemctl stop "$s" 2>/dev/null
done

echo "== beeld, metadata en sensordata wissen"
for d in framekm unprocessed_framekm metadata unprocessed_metadata ml_metadata pic pic_lg raw gps imu lorawan; do
  rm -rf "$D/$d"; mkdir -p "$D/$d"
done
rm -rf "$D/fotos"
rm -f "$D/events.log" "$D/object-detection.log" "$D/camera-node.log" "$D/health-manager.log" "$D/testout.bin"

echo "== oude databases weg, de nieuwste leegmaken zoals de vendor-resetDB"
NEWEST=$(ls "$D"/data-logger.v*.db 2>/dev/null | sort -V | tail -1)
for db in "$D"/data-logger.v*.db; do
  [ "$db" = "$NEWEST" ] || rm -f "$db" "$db"-shm "$db"-wal "$db"-journal
done
rm -f "$D"/gnss.v*.db* "$D"/mm_cursor.log
python3 - <<'EOF'
import sqlite3, glob
for p in glob.glob('/mnt/data/data-logger.v*.db'):
    c = sqlite3.connect(p)
    tabellen = [r[0] for r in c.execute("select name from sqlite_master where type='table'")]
    for t in ('framekms','packed_framekms','gnss','imu','frames','error_logs','gnss_auth','magnetometer'):
        if t in tabellen:
            c.execute('DELETE FROM %s' % t)
    c.commit(); c.execute('VACUUM'); c.close()
    print('leeggemaakt:', p, tabellen)
EOF

echo "== ook de RAM-buffer met recente beelden"
rm -rf /tmp/recording/pic/* /tmp/recording/preview/* 2>/dev/null

sync
echo "== klaar, overgebleven gebruik:"
df -h $D | tail -1
echo "== herstart over 3 s"
sleep 3
reboot
