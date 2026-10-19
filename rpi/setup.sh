#!/usr/bin/env bash
# ALERTO hub installer for Raspberry Pi 5 (Raspberry Pi OS Bookworm/Trixie, 64-bit).
#
#   1. Edit config.env (SSID, DEVICE_KEY, ADMIN_PIN, station name)
#   2. sudo ./setup.sh
#
# What it does
#   - installs the app to /opt/commuter-density with its own Python venv
#   - turns wlan0 into a Wi-Fi hotspot (NetworkManager, 10.42.0.1/24, DHCP)
#   - captive portal DNS: every hostname resolves to the Pi, plus DHCP option 114
#   - systemd: density-web      (dashboard, sensor collector, OLED/gate feed on port 80)
#              density-forecast (ARIMA/ARIMAX/Prophet per segment, every 5 minutes)
#
# wlan0 becomes the hotspot, so if you're connected to the Pi over Wi-Fi you'll lose
# that connection. Use Ethernet or a keyboard + monitor while running this.
# Re-running is safe; it updates the code and keeps your data.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo ./setup.sh"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"
APP=/opt/commuter-density
ENV_FILE=/etc/commuter-density.env
RUN_USER="${SUDO_USER:-pi}"
WIFI_IF="${WIFI_IF:-wlan0}"

# config: keep an existing /etc file (your edits) unless --reset-config is given
if [ ! -f "$ENV_FILE" ] || [ "${1:-}" = "--reset-config" ]; then
  install -m 640 -o root -g "$RUN_USER" "$SRC/config.env" "$ENV_FILE"
fi
set -a; . "$ENV_FILE"; set +a

if [ -z "${ASSUME_YES:-}" ]; then
  echo "This will turn $WIFI_IF into the hotspot \"$HOTSPOT_SSID\" ($PORTAL_IP)."
  read -r -p "Continue? [y/N] " ans; [ "${ans,,}" = "y" ] || exit 1
fi

echo "==> Packages"
apt-get update
apt-get install -y python3-venv python3-dev build-essential sqlite3

echo "==> Time zone and Wi-Fi country"
timedatectl set-timezone Asia/Manila
raspi-config nonint do_wifi_country PH 2>/dev/null || iw reg set PH || true

echo "==> App files -> $APP"
mkdir -p "$APP/data" "$APP/outputs" "$APP/static"
install -m 644 "$SRC"/{app.py,store.py,frames.py,settings.py,forecaster.py,simulate_data.py,requirements.txt} "$APP/"
install -m 644 "$SRC/static/index.html" "$APP/static/"
rm -rf "$APP/static/fonts" "$APP/forecast.py"
chown -R "$RUN_USER":"$RUN_USER" "$APP"

echo "==> Python environment (a few minutes on first run)"
sudo -u "$RUN_USER" python3 -m venv "$APP/venv"
sudo -u "$RUN_USER" "$APP/venv/bin/pip" install -q --upgrade pip
sudo -u "$RUN_USER" "$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"
# Prophet is optional: forecaster.py runs ARIMA and ARIMAX only if it is missing
if ! sudo -u "$RUN_USER" "$APP/venv/bin/pip" install -q prophet; then
  echo "!! Prophet failed to install - continuing with ARIMA and ARIMAX only."
fi

echo "==> Wi-Fi hotspot"
nmcli connection delete density-hotspot >/dev/null 2>&1 || true
nmcli connection add type wifi ifname "$WIFI_IF" con-name density-hotspot autoconnect yes \
  ssid "$HOTSPOT_SSID" 802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
  ipv4.method shared ipv4.addresses "$PORTAL_IP/24" ipv6.method disabled \
  connection.autoconnect-priority 100
if [ -n "$HOTSPOT_PASS" ]; then
  nmcli connection modify density-hotspot wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$HOTSPOT_PASS"
fi

echo "==> Captive portal DNS/DHCP"
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
cat > /etc/NetworkManager/dnsmasq-shared.d/captive-portal.conf <<EOF
# Every hostname resolves to the hub, so phones' connectivity checks land on the dashboard
address=/#/$PORTAL_IP
# RFC 8910: tell modern iOS/Android where the portal is
dhcp-option=114,"http://$PORTAL_IP/"
EOF
nmcli connection up density-hotspot || echo "!! Hotspot didn't start - check: nmcli device status"

echo "==> systemd services"
cat > /etc/systemd/system/density-web.service <<EOF
[Unit]
Description=Commuter density dashboard + ESP32 collector
After=network-online.target NetworkManager.service
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP
EnvironmentFile=$ENV_FILE
ExecStart=$APP/venv/bin/waitress-serve --listen=0.0.0.0:80 --threads=8 app:app
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/density-forecast.service <<EOF
[Unit]
Description=ALERTO forecaster (ARIMA / ARIMAX / Prophet per segment, every 5 min)
After=density-web.service

[Service]
User=$RUN_USER
WorkingDirectory=$APP
EnvironmentFile=$ENV_FILE
Nice=10
ExecStart=$APP/venv/bin/python forecaster.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# the old hourly timer is replaced by the service above
systemctl disable --now density-forecast.timer 2>/dev/null || true
rm -f /etc/systemd/system/density-forecast.timer

systemctl daemon-reload
systemctl enable --now density-web.service density-forecast.service
systemctl restart density-web.service density-forecast.service

echo
echo "Done."
echo "  Hotspot:    $HOTSPOT_SSID  (password: ${HOTSPOT_PASS:-none, open network})"
echo "  Dashboard:  http://$PORTAL_IP/  or  http://$PORTAL_NAME/  (pops up automatically on phones)"
echo "  ESP32:      SERVER_URL http://$PORTAL_IP/ingest, DEVICE_KEY from $ENV_FILE"
echo "  Logs:       journalctl -u density-web -f      journalctl -u density-forecast"
echo "  Forecasts:  every 5 min once a segment has 3 days of data (journalctl -u density-forecast -f)"
[ "$DEVICE_KEY" = "change-me-device-key" ] && echo "  !! Change DEVICE_KEY in $ENV_FILE (and in the sketch), then: sudo systemctl restart density-web"
[ -z "$ADMIN_PIN" ] && echo "  Note: ADMIN_PIN is empty, so remote recalibration is disabled."
exit 0
