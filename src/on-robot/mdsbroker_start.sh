#!/bin/sh
# Avvio del mini-broker comandi al boot. Gated da /data/mdsbroker/ENABLED (kill-switch:
# rimuovi il flag e riavvia per tornare a medusa->cloud diretto). Idempotente.
B=/data/mdsbroker
[ -f "$B/ENABLED" ] || { echo "$(date) broker DISABILITATO (no flag)" >> "$B/start.log"; exit 0; }
# 1) redirect jmq -> broker locale (hosts e' tmpfs, va riscritto ad ogni boot)
grep -q "jmq-ngiot-it" /etc/hosts || echo "127.0.0.1 jmq-ngiot-it.area.ww.ecouser.net" >> /etc/hosts
# 2) avvia il broker se non gia' attivo
if pgrep -f "mdsbroker.py" >/dev/null 2>&1; then
    echo "$(date) broker gia' attivo" >> "$B/start.log"; exit 0
fi
PY=$(command -v python2 || command -v python)
echo "$(date) avvio broker" >> "$B/start.log"
"$PY" -u "$B/mdsbroker.py" --daemon
