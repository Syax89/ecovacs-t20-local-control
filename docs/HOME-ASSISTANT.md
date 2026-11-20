# Local control through Home Assistant

Goal: control the robot entirely from Home Assistant's own Mosquitto broker, with **app parity** and the
**Ecovacs cloud left intact**. Nothing is flashed; everything runs on the robot or the HA host.

## The two on-robot pieces

### 1. The agent — `t20_ha_mqtt.py`

A single Python 2.7 file that runs **on the robot**, with **zero dependencies and no TLS** (the robot's
Python 2.7 has no `_ssl` and no `paho`, so MQTT 3.1.1 is implemented over a raw socket). It:

- connects to HA's **Mosquitto** (`<HA_HOST>:1883`, user/pass from config);
- publishes **Home Assistant MQTT discovery** for ~60 entities and a JSON state on `t20/<did8>/state`;
- subscribes to command topics (`t20/<did8>/cmd`, `/room`, and per-entity topics) and forwards commands
  to the bridge FIFO;
- runs **active polling**: periodically injects `getXxx` commands so the bridge captures fresh
  responses → all sensors populate without any device movement;
- publishes a **Last-Will (LWT)** `offline` so HA marks entities *unavailable* if the robot/agent dies,
  instead of showing stale values;
- self-daemonises with `os.fork()` + `os.setsid()` (BusyBox has no `setsid`/`nohup`).

Configuration is a one-line-per-key file at `/data/t20_mqtt.conf` (see
[`t20_mqtt.conf.example`](../src/on-robot/t20_mqtt.conf.example)); defaults in the script are
placeholders you must override:

```ini
host=<HA_HOST>
port=1883
user=CHANGEME
pass=CHANGEME
did=<DID>
name=Deebot T20 Omni
poll=10
ignore_errors=        # comma-separated error codes to not flag as "Problem" (e.g. 314)
```

### 2. The command bridge — `mdsbroker.py`

Commands such as start/stop/dock are only honoured on the **cloud `iot/p2p/*` channel** that `medusa` is
subscribed to. The bridge sits transparently between `medusa` and the real cloud and lets us inject
them:

```
medusa ──TLS──▶ [openssl s_server :443] ──plain──▶ relay ──plain──▶ [openssl s_client] ──TLS──▶ real cloud
                                                     ▲ inject iot/p2p/* via FIFO
```

(`openssl` is used because the robot's Python can't do TLS.) The bridge:

- forwards all traffic both ways → **the cloud stays fully functional**;
- exposes a FIFO `/data/mdsbroker/cmd.fifo` that accepts either a short name (`start`, `stop`, `pause`,
  `resume`, `charge`, `locate`, `relocate`) **or** a generic JSON line `{"cmd":…,"data":…}` to inject
  **any** command;
- **passively sniffs** the relayed `iot/atr/*` and `iot/p2p/.../p/...` responses into
  `telemetry.json` (atomic writes, map fragments accumulated by index) — this is how the agent gets
  rich state **without querying the device**;
- is **gated** by `/data/mdsbroker/ENABLED` (remove it + reboot → medusa talks to the cloud directly);
- optional modes via flag files: `CAPTURE` (log the app's `iot/p2p/*/q` commands, to learn new
  payloads) and `NO_ANALYTICS` (drop `FwBuryPoint` packets — see [PRIVACY.md](PRIVACY.md)).

### 3. The watchdog — `mdsbroker_watchdog.py`

Keeps the bridge alive (20 s loop, single-instance via `/proc` scan), self-heals the `/etc/hosts`
redirect, and — crucially — if the bridge won't come back it **removes the redirect as a fail-safe** so
`medusa` falls back to the **direct cloud** and is **never isolated**. It keeps retrying and restores the
redirect the moment the bridge is healthy again. Respects the `ENABLED` kill-switch.

## The entities (~60)

- **vacuum** (state + battery + fan speed)
- **select** × 3: fan speed, water level, work mode
- **number** × 3: volume (0–10), cleaning passes (1–2), mop-wash interval (0–60)
- **switch** × 7: continuous cleaning, advanced mode, carpet boost, TrueDetect, clean preference,
  multi-map, auto-empty
- **button** × 10+: locate, relocate, 3× consumable reset, station (empty / wash / dry start / dry stop
  / self-clean)
- **sensor** × ~20: activity, current-clean area/time/type, total area/time/count, water, mop, fan,
  work mode, station state, volume, error (+ station error, with code attribute), consumable life
  (brush/side-brush/filter), Wi-Fi RSSI/SSID/IP, uptime, firmware, map name, rooms (mssid)
- **camera**: the rendered map (published by the renderer — see [MAP-RENDERING.md](MAP-RENDERING.md))

Per-room cleaning: publish the room `mssid`s to `t20/<did8>/room` (e.g. `2,3`) → the agent injects
`clean {act:start, type:spotArea, content:"2,3", count:1}`.

## Networking you need

- A firewall rule allowing `<ROBOT_IP>` → `<HA_HOST>:1883` (robots are often on a separate VLAN).
- An MQTT user on Mosquitto for the robot (the agent uses `user`/`pass` from its config).

## Deploy & restart safely (the `pgrep` self-match trap)

A real outage happened here once, worth repeating: the start scripts and the watchdog's single-instance
guard use `pgrep -f "<name>.py"` / `/proc` scans. **If your deploy SSH command contains those `.py`
filenames verbatim, it matches your own shell**, so the start scripts think the daemons are already
running and don't start them.

**Rule:** the SSH command that launches the start scripts must **not** contain the `.py` names — call
only the `.sh` scripts (`sh /data/mdsbroker/mdsbroker_start.sh; sh /data/mdsbroker/watchdog_start.sh;
sh /data/t20_mqtt_start.sh`) and verify status in a **separate, clean** SSH session.

Safe procedure: `scp -O` to `*.py.new` → `python2 -m py_compile` each on the robot → only if OK,
`cp` a `.bak` and `mv` into place → stop (watchdog first) → start via the `.sh` scripts → verify in a
clean session.
