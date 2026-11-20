#!/bin/sh
# Avvia l'agente MQTT locale del T20 (verso HA Mosquitto). Idempotente, invocato al boot.
# L'agente si auto-daemonizza (--daemon): BusyBox non ha setsid/nohup. Niente flash, cloud intatto.
echo "=== t20_mqtt_start $(date) ===" >> /data/t20_mqtt.log
if pgrep -f "t20_ha_mqtt.py" >/dev/null 2>&1; then echo "agente gia' attivo" >> /data/t20_mqtt.log; exit 0; fi
PY=$(command -v python2 || command -v python)
"$PY" -u /data/t20_ha_mqtt.py --daemon
echo "launcher: agente lanciato (--daemon)" >> /data/t20_mqtt.log
