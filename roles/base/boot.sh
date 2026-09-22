#!/bin/sh
# roamcam-lab rol 'basis' — draait bij elke boot (via /mnt/data/cron_config + systemd-run).
# 1. Legt alle Hivemapper-diensten stil (runtime-mask: / is een RAM-overlay, dus dit moet elke boot).
# 2. Geeft het toestel een eigen naam (hostnaam, wifi-SSID, mDNS).
# 3. Start het LoRa-basisstation.
set -u
BASIS=/mnt/data/basis
NAME=$(cat $BASIS/hostname 2>/dev/null || echo roamcam-basis)
LOG=$BASIS/boot.log
exec >>"$LOG" 2>&1
echo "=== boot $(date) naam=$NAME"

# --- 1. Hivemapper eruit -------------------------------------------------------
# api-health-manager eerst: die herstart anders de rest weer.
HIVE="api-health-manager camera-node camera-bridge data-logger object-detection folder_purger
      usb-write lorawan-logger rauc lldpd usb-update"
for s in $HIVE; do
  systemctl mask --runtime "$s" >/dev/null 2>&1
  systemctl stop "$s" >/dev/null 2>&1
done
# alles wat nog uit /opt/dashcam draait (behalve led-controller) afschieten
for p in $(ps | grep /opt/dashcam/bin | grep -v -e grep -e led-controller | awk '{print $1}'); do kill "$p" 2>/dev/null; done

# --- 2. eigen naam ---------------------------------------------------------------
OLD=$(hostname)
if [ "$OLD" != "$NAME" ]; then
  echo "$NAME" > /etc/hostname
  sed -i "s/$OLD/$NAME/g" /etc/hosts
  hostname "$NAME"
fi
# wifi-AP: eigen SSID, landcode NL; wachtwoord uit $BASIS/ap_pass als dat bestaat
SSID="$NAME"
sed -i -e "s/^ssid=.*/ssid=$SSID/" -e "s/^country_code=.*/country_code=NL/" /etc/hostapd.conf
if [ -s $BASIS/ap_pass ]; then
  sed -i "s/^wpa_passphrase=.*/wpa_passphrase=$(cat $BASIS/ap_pass)/" /etc/hostapd.conf
fi
systemctl restart avahi-daemon >/dev/null 2>&1
if ! grep -q "ssid=$SSID" /run/hostapd.ssid 2>/dev/null; then
  systemctl restart hostapd
  echo "ssid=$SSID" > /run/hostapd.ssid
fi

# --- 3. LoRa-basisstation --------------------------------------------------------
systemctl stop roamcam-lora >/dev/null 2>&1
systemd-run --unit=roamcam-lora --collect -p Restart=always -p RestartSec=5 \
  /usr/bin/python3 $BASIS/lora_base.py
echo "klaar: $(systemctl is-active roamcam-lora)"
