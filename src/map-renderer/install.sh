#!/bin/sh
# Installa il renderer come servizio systemd (host Linux/Pi/NAS/HA supervised senza Docker). Esegui come root.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
DST=/opt/t20-map
mkdir -p "$DST"
cp "$HERE/t20_map_render.py" "$DST/"
python3 -m venv "$DST/venv"
"$DST/venv/bin/pip" -q install --upgrade pip
"$DST/venv/bin/pip" -q install paho-mqtt Pillow
cp "$HERE/t20-map-render.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now t20-map-render.service
echo "Servizio avviato. Log: journalctl -u t20-map-render -f"
