#!/bin/sh
# Mitigazione privacy T20 (dispositivo del proprietario). Blocca via /etc/hosts gli endpoint di
# RACCOLTA DATI in blocco (mappe/arredi/immagini AI/audio wake/log) SENZA toccare il controllo MQTT
# ne' l'app. /etc/hosts e' tmpfs: va riscritto a ogni boot (per questo e' un hook). Idempotente.
# Reversibile: rm /data/privacy_hosts.on  (e rimuovi le righe da /etc/hosts) -> torna tutto com'era.
FLAG=/data/privacy_hosts.on
[ -f "$FLAG" ] || exit 0          # gate: attivo solo se presente il flag
block() {
    grep -q "[[:space:]]$1\$" /etc/hosts 2>/dev/null || echo "0.0.0.0 $1" >> /etc/hosts
}
block iotin.ecouser.net
block iotin-ww.ecouser.net
echo "$(date) privacy: iotin/iotin-ww bloccati" >> /data/privacy.log
