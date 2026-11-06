#!/bin/bash
# Run once after installing the RTL88x2bu driver. See README.md.
set -Eeuo pipefail
BACKUP=/home/pi/dongle-config-backup
fail() { echo "$*" >&2; exit 1; }
test "$(id -u)" = 0 || fail 'Run with sudo.'
test -d /sys/class/net/wlan0 || fail 'Missing built-in Wi-Fi wlan0.'
test -d /sys/class/net/wlan1 || fail 'Missing USB Wi-Fi wlan1; install/load 88x2bu first.'
test ! -e /sys/class/net/wlan-internal || fail 'Already configured: wlan-internal exists.'
test "$(basename "$(readlink -f /sys/class/net/wlan0/device/driver)")" = brcmfmac || fail 'wlan0 is not the built-in brcmfmac adapter.'
test "$(basename "$(readlink -f /sys/class/net/wlan1/device/driver)")" = rtl88x2bu || fail 'wlan1 is not an rtl88x2bu adapter.'
test ! -e "$BACKUP" || fail "Backup already exists: $BACKUP; inspect before retrying."
for link in /etc/systemd/network/10-alarm-internal.link /etc/systemd/network/11-alarm-dongle.link; do
    test ! -e "$link" && test ! -L "$link" || fail "Existing configuration: $link"
done
systemctl is-active --quiet dhcpcd.service || fail 'Requires an active dhcpcd service.'
systemctl is-active --quiet shinwhatech-wifi-portal.service || fail 'Requires an active alarm Wi-Fi portal.'
mkdir -m 700 "$BACKUP"
cp -a /etc/dhcpcd.conf "$BACKUP/dhcpcd.conf"
internal_mac=$(cat /sys/class/net/wlan0/address)
usb_mac=$(cat /sys/class/net/wlan1/address)
rollback() {
    trap - ERR HUP INT TERM
    set +e
    systemctl stop shinwhatech-wifi-portal.service
    systemctl stop dhcpcd.service
    for iface in wlan0 wlan1 wlan-internal; do wpa_cli -i "$iface" terminate; done
    if test -d /sys/class/net/wlan-internal; then
        if test -d /sys/class/net/wlan0; then
            ip link set wlan0 down
            ip link set wlan0 name wlan1
        fi
        ip link set wlan-internal down
        ip link set wlan-internal name wlan0
    fi
    cp -a "$BACKUP/dhcpcd.conf" /etc/dhcpcd.conf
    rm -f /etc/systemd/network/10-alarm-internal.link /etc/systemd/network/11-alarm-dongle.link
    udevadm control --reload
    systemctl start dhcpcd.service
    systemctl start shinwhatech-wifi-portal.service
    echo ROLLED_BACK
    exit 1
}
trap rollback ERR HUP INT TERM
cat > /etc/systemd/network/10-alarm-internal.link <<EOF
[Match]
MACAddress=$internal_mac
[Link]
Name=wlan-internal
EOF
cat > /etc/systemd/network/11-alarm-dongle.link <<EOF
[Match]
MACAddress=$usb_mac
[Link]
Name=wlan0
EOF
cat >> /etc/dhcpcd.conf <<'EOF'

# USB Wi-Fi is the primary connection for the alarm.
interface wlan0
metric 100
interface wlan-internal
metric 600
EOF
udevadm control --reload
systemctl stop shinwhatech-wifi-portal.service
systemctl stop dhcpcd.service
for iface in wlan0 wlan1; do wpa_cli -i "$iface" terminate || true; done
sleep 2
ip link set wlan0 down
ip link set wlan0 name wlan-internal
ip link set wlan1 down
ip link set wlan1 name wlan0
systemctl start dhcpcd.service
connected=0
for attempt in {1..90}; do
    if wpa_cli -i wlan0 status | grep -q 'wpa_state=COMPLETED' && ip -4 -o address show dev wlan0 scope global | awk '$4 !~ /^169\.254\./ && $4 != "" { found=1 } END { exit !found }'; then
        connected=1
        break
    fi
    sleep 2
done
test "$connected" = 1
systemctl start shinwhatech-wifi-portal.service
echo SWITCH_COMPLETE
ip -brief address
ip -4 route
