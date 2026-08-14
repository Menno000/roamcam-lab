#!/bin/sh
# Start the Roamcam Lab trip role if it isn't already running.
#
# Called by the firmware's own cron (/mnt/data/cron_config): once directly at boot, and every
# minute after that as a watchdog. Only /mnt/data survives a reboot on this device, so this
# script lives there rather than in /etc or /opt.
#
# setsid detaches it from whatever called it, so it keeps running when the caller goes away.
# The [t] in the pattern stops grep from matching itself.

ps ax | grep -q "[t]ravel_server.py" && exit 0

echo "$(date '+%Y-%m-%d %H:%M:%S')  watchdog starting travel_server" >> /mnt/data/travel_stdout.log
cd /mnt/data || exit 1
setsid python3 /mnt/data/travel_server.py >> /mnt/data/travel_stdout.log 2>&1 </dev/null &
exit 0
