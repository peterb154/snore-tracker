#!/bin/sh
# Network status on the board LEDs (issue #17). Runs as root from /usr/local/sbin/snore-leds,
# installed by install.sh: never from ~pi, which pi can write.
#   joining Wi-Fi / no NTP            green blink
#   online, garmin_connect reachable  green solid
#   online, garmin_connect down       green blink + red solid
#   no network for 3 min              red blink
# Dark 20:30-08:30 (the recording window), except for the first 10 min after boot.
# `snore-leds decide <net> <synced> <reachable> <offline_s> <hhmm> <uptime_s>` prints the state;
# `snore-leds show <state>` sets the LEDs once.

API=${SNORE_API:-https://fitness.epetersons.com/api/snore}
GREEN=/sys/class/leds/ACT
RED=/sys/class/leds/PWR  # inverted on a Pi 4: kernel "on" is dark

decide() {  # net synced reachable: yes|no; hhmm like 0830
    net=$1 synced=$2 reachable=$3 offline=$4 hhmm=$5 uptime=$6
    if [ "$uptime" -ge 600 ] && { [ "$hhmm" -ge 2030 ] || [ "$hhmm" -lt 830 ]; }; then
        echo dark
    elif [ "$net" = no ]; then
        if [ "$offline" -ge 180 ]; then echo offline; else echo joining; fi
    elif [ "$synced" = no ]; then
        echo joining
    elif [ "$reachable" = yes ]; then
        echo online
    else
        echo unreachable
    fi
}

green() {  # off | on | blink
    case $1 in
        blink) echo timer >$GREEN/trigger; echo 1000 >$GREEN/delay_on; echo 1000 >$GREEN/delay_off ;;
        on) echo none >$GREEN/trigger; echo 1 >$GREEN/brightness ;;
        *) echo none >$GREEN/trigger; echo 0 >$GREEN/brightness ;;
    esac
}

red() {
    case $1 in
        blink) echo timer >$RED/trigger; echo 1000 >$RED/delay_on; echo 1000 >$RED/delay_off ;;
        on) echo none >$RED/trigger; echo 0 >$RED/brightness ;;
        *) echo default-on >$RED/trigger ;;
    esac
}

show() {
    case $1 in
        joining) green blink; red off ;;
        online) green on; red off ;;
        unreachable) green blink; red on ;;
        offline) green off; red blink ;;
        *) green off; red off ;;
    esac
}

if [ "$1" = decide ]; then shift; decide "$@"; exit; fi
if [ "$1" = show ]; then show "$2"; exit; fi  # e.g. `snore-leds show offline` to eyeball a state

host=$(echo "$API" | sed -E 's|(https?://[^/]+).*|\1|')
offline_since=$(date +%s)
last=
while :; do
    now=$(date +%s)
    net=no
    [ "$(nmcli -t -g STATE general)" = connected ] && net=yes && offline_since=$now
    synced=$(timedatectl show -p NTPSynchronized --value)
    reachable=no
    if [ "$net" = yes ]; then
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$host/")
        [ "$code" != 000 ] && reachable=yes
    fi
    uptime=$(cut -d. -f1 /proc/uptime)
    state=$(decide "$net" "$synced" "$reachable" $((now - offline_since)) "$(date +%H%M)" "$uptime")
    # Rewriting the timer trigger restarts the blink, so only touch the LEDs on a change
    [ "$state" != "$last" ] && show "$state" && echo "state: $state" && last=$state
    sleep 30
done
