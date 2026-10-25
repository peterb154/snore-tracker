#!/bin/sh
# Wait for NTP before recording: the Pi has no RTC, and segment names and the boot-time window
# check use the clock. Gives up after NTP_WAIT_SECONDS with a warning, so a night is never lost.
# <4> is journald's warning priority.
limit=${NTP_WAIT_SECONDS:-120}
waited=0
until [ "$(timedatectl show -p NTPSynchronized --value)" = yes ]; do
    if [ "$waited" -ge "$limit" ]; then
        echo "<4>WARNING: no NTP sync after ${limit}s, recording with an unsynced clock ($(date))"
        exit 0
    fi
    sleep 5
    waited=$((waited + 5))
done
