# usercustomize.py -- hook di boot per il T20 (dispositivo del proprietario, accesso locale).
# Caricato dal Python di ROS (parte al boot come root, HOME=/data, user-site attiva), PRIMA di medusa.
# Avvia: (1) mini-broker comandi (redirect jmq->broker, gated da flag), (2) sshd, (3) agente MQTT,
# (4) watchdog del broker (riavvio / fail-safe verso cloud diretto).
# Robusto: non solleva mai eccezioni, non blocca ROS.
try:
    import os, subprocess
    hooks = (("/tmp/.mdsbroker_started", "/data/mdsbroker/mdsbroker_start.sh"),
             ("/tmp/.ssh_boot_started",  "/data/ssh/start_sshd.sh"),
             ("/tmp/.t20_mqtt_started",  "/data/t20_mqtt_start.sh"),
             ("/tmp/.mdswd_started",     "/data/mdsbroker/watchdog_start.sh"),
             ("/tmp/.privacy_started",   "/data/privacy_start.sh"))
    for flag, script in hooks:
        if os.path.exists(script) and not os.path.exists(flag):
            open(flag, "w").close()
            subprocess.Popen(["/bin/sh", script],
                stdin=open(os.devnull, "r"),
                stdout=open("/data/boot_hooks.log", "a"),
                stderr=subprocess.STDOUT, close_fds=True)
except Exception:
    pass
