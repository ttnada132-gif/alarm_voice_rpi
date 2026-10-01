#!/bin/bash
set -euo pipefail

SCRIPT_DIR="/home/pi/shinwhatech"
cd "$SCRIPT_DIR"

# Open the monitor for desktop launches, not systemd restarts.
if [[ -z "${INVOCATION_ID:-}" && -n "${DISPLAY:-}" ]]; then
  "$SCRIPT_DIR/show_logs.sh" >/dev/null 2>&1 &
fi

LOG_FILE="$SCRIPT_DIR/log.txt"
# Read saved portal values as data; never evaluate them as shell commands.
CONNECTION_VALUES="$(/usr/bin/python3 "$SCRIPT_DIR/connection_config.py" "$SCRIPT_DIR/wifi_portal.json")"
mapfile -t CONNECTION_FIELDS <<< "$CONNECTION_VALUES"
DEVICE_ID="${CONNECTION_FIELDS[0]}"
SERVER_URL="${CONNECTION_FIELDS[1]}"
STREAM_TOKEN="${CONNECTION_FIELDS[2]}"
WIFI_CSV="$SCRIPT_DIR/wifi_networks.csv"
touch "$LOG_FILE"
chmod 664 "$LOG_FILE" 2>/dev/null || true
chown pi:pi "$LOG_FILE" 2>/dev/null || true

log_message() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
}

/usr/bin/python3 "$SCRIPT_DIR/wifi_connect.py" \
  --csv "$WIFI_CSV" \
  --server-test-url "$SERVER_URL/api/pi/wifi-check" \
  --token "$STREAM_TOKEN" \
  --interface wlan0 \
  --retry-seconds 30 \
  --connect-timeout-seconds 25 \
  --hook-gpio 4 \
  --log-file "$LOG_FILE"

pkill -x aplay 2>/dev/null || true
pkill -f "/home/pi/shinwhatech/raspi_agent.py" 2>/dev/null || true
pulseaudio -k 2>/dev/null || true

HANDSET_CARD_ID="$(/usr/bin/python3 "$SCRIPT_DIR/find_handset.py" || true)"
if [ -n "$HANDSET_CARD_ID" ]; then
  amixer -c "$HANDSET_CARD_ID" sset PCM 90% unmute >/dev/null 2>&1 || true
  amixer -c "$HANDSET_CARD_ID" sset Mic 100% cap >/dev/null 2>&1 || true
else
  # Keep USB call audio separate from AUX broadcasts, even before USB is attached.
  HANDSET_CARD_ID="Device"
  log_message "USB handset audio unavailable; starting AUX broadcast reception without USB."
fi

HANDSET_DEVICE="plughw:CARD=$HANDSET_CARD_ID,DEV=0"
log_message "Using handset audio device $HANDSET_DEVICE"
log_message "Using handset hook on BCM GPIO4 (pull-up, HIGH=off-hook, LOW=on-hook)"

if [ "$(id -u)" -eq 0 ]; then
  PYTHON_RUNNER=(/usr/sbin/runuser -u pi -- /usr/bin/python3)
else
  PYTHON_RUNNER=(/usr/bin/python3)
fi

exec "${PYTHON_RUNNER[@]}" /home/pi/shinwhatech/raspi_agent.py \
  --server "$SERVER_URL" \
  --token "$STREAM_TOKEN" \
  --device-id "$DEVICE_ID" \
  --source-name raspberrypi \
  --handset-input-device "$HANDSET_DEVICE" \
  --handset-speaker-device "$HANDSET_DEVICE" \
  --aux-device plughw:CARD=Headphones,DEV=0 \
  --hook-gpio 4 \
  --gpio-alert-pin 23 \
  --gpio-alert-led-pin 24 \
  --gpio-alert-file /home/pi/shinwhatech/0001.mp3 \
  --gpio-alert-output-device plughw:CARD=Headphones,DEV=0 \
  --gpio-alert-interval-seconds 10 \
  --siren-file /home/pi/shinwhatech/0001.mp3 \
  --siren-output-device plughw:CARD=Headphones,DEV=0 \
  --log-file "$LOG_FILE" \
  --gps-port /dev/serial0 \
  --gps-interval-seconds 10 \
  --gps-read-timeout-seconds 10 \
  --gps-cache-file /home/pi/shinwhatech/last_location.json
