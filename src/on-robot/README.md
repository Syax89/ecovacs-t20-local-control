# `on-robot/` — the Home Assistant agent, command bridge & watchdog

These run **on the robot** (Python 2.7, deployed under `/data`). Full design in
[`../../docs/HOME-ASSISTANT.md`](../../docs/HOME-ASSISTANT.md).

## Files

| File | Deploy path | Purpose |
|---|---|---|
| `t20_ha_mqtt.py` | `/data/t20_ha_mqtt.py` | The HA agent: pure-socket MQTT 3.1.1, HA discovery, state, commands, active polling, LWT. |
| `mdsbroker.py` | `/data/mdsbroker/mdsbroker.py` | TLS bridge medusa↔cloud; injects `iot/p2p/*` via FIFO; sniffs telemetry; optional analytics drop. |
| `mdsbroker_watchdog.py` | `/data/mdsbroker/mdsbroker_watchdog.py` | Keeps the bridge alive; fail-safe to direct cloud. |
| `mdsbroker_start.sh` | `/data/mdsbroker/` | Boot/launch for the bridge (gated by `ENABLED`). |
| `watchdog_start.sh` | `/data/mdsbroker/` | Boot/launch for the watchdog. |
| `t20_mqtt_start.sh` | `/data/t20_mqtt_start.sh` | Boot/launch for the agent. |
| `t20_mqtt.conf.example` | copy to `/data/t20_mqtt.conf` | Per-device config (host, creds, did). |

## Configure

Copy `t20_mqtt.conf.example` to `/data/t20_mqtt.conf` and fill in your values (the defaults in the code
are `CHANGEME` placeholders):

```ini
host=<HA_HOST>
port=1883
user=<mqtt_user>
pass=<mqtt_pass>
did=<DID>
name=Deebot T20 Omni
poll=10
ignore_errors=
```

Flag files (create/delete on the robot to toggle behaviour):

| Flag | Effect |
|---|---|
| `/data/mdsbroker/ENABLED` | Master switch for the command bridge (remove + reboot → direct cloud). |
| `/data/mdsbroker/NO_ANALYTICS` | Drop `FwBuryPoint` analytics (SSID/BSSID) instead of forwarding. |
| `/data/mdsbroker/CAPTURE` | Log the app's `iot/p2p/*/q` commands to `capture.log` (to learn payloads). |
| `/data/privacy_hosts.on` | Enable the `/etc/hosts` bulk-upload block at boot. |

## Deploy safely

> ⚠️ **The `pgrep` self-match trap:** do **not** put the `.py` filenames in your deploy SSH command — it
> matches your own shell and the start scripts will think the daemons are already up. Launch only the
> `.sh` scripts and verify in a **separate, clean** SSH session.

```bash
# stage, byte-compile, swap, restart (watchdog first), verify in a clean session
scp -O -i key t20_ha_mqtt.py root@<ROBOT_IP>:/data/t20_ha_mqtt.py.new
ssh -i key root@<ROBOT_IP> 'python2 -m py_compile /data/t20_ha_mqtt.py.new && \
  cp /data/t20_ha_mqtt.py /data/t20_ha_mqtt.py.bak && mv /data/t20_ha_mqtt.py.new /data/t20_ha_mqtt.py'
ssh -i key root@<ROBOT_IP> 'sh /data/mdsbroker/mdsbroker_start.sh; \
  sh /data/mdsbroker/watchdog_start.sh; sh /data/t20_mqtt_start.sh'
```

Install the boot hooks (so everything restarts after a reboot) by placing
[`../local-cloud/persistence/usercustomize.py`](../local-cloud/persistence/usercustomize.py) at
`/data/.local/lib/python2.7/site-packages/usercustomize.py` and the matching `*_start.sh` scripts in the
paths above.
