# Architecture

This page explains the moving parts and the data flow once the project is fully deployed. For the
step-by-step build order, follow the [Wiki](../../../wiki).

## Placeholders used throughout

| Placeholder | Meaning | Example |
|---|---|---|
| `<ROBOT_IP>` | the robot's address on your LAN | `192.168.4.100` |
| `<PROXY_HOST>` | the LAN machine running the MITM / fake broker; the DNS-rewrite target | `192.168.1.20` |
| `<HA_HOST>` | Home Assistant host running the Mosquitto broker | `192.168.1.10` |
| `<DID>` | the robot's device id (a UUID) | `00000000-0000-…` |
| `<SERIAL>` | the robot's serial number | `XXXXXXXXXXXXXXXX` |

## The device, in one paragraph

The Deebot T20 Omni runs a Rockchip **aarch64** SoC on **Linux 4.4**, with a **read-only squashfs**
root filesystem and a writable **`/data`** (ext4) partition. Userland is **ROS + BusyBox + Python 2.7**.
A daemon called **`medusa`** is the cloud client: it speaks **MQTT over TLS to port 443** of an Ecovacs
broker. There is no official local API on this model (the newer "NGIoT" local P2P stack is not
implemented here), so the path to local control is: **get root, then speak the cloud protocol locally.**

## Two phases

### Phase 1 — Break in (temporary scaffolding)

```
   robot ──DNS rewrite──▶ <PROXY_HOST>
     │                        │
     │   MQTT/TLS :443        │  mitm_proxy.py  : terminate TLS, re-resolve real cloud IP via DoH,
     ├────────────────────────┤                   bridge to real Ecovacs, log everything in clear
     │                        │  local_broker.py: pretend to be the broker, send iot/p2p/shell (root)
     ▼                        ▼
  learns protocol        delivers payload_sshd.sh → sshd on :22 (key-only root)
```

These components run on `<PROXY_HOST>` and are only needed **while breaking in** and while you want to
capture traffic. Once persistence is installed, SSH no longer needs them.

### Phase 2 — Steady state (everything on the robot / HA)

```
                   ┌──────────────────────── robot (/data, persistent) ───────────────────────┐
                   │                                                                           │
   boot ─ ROS Python ─ usercustomize.py ─┬─▶ start_sshd.sh      (key-only root SSH)            │
                                         ├─▶ mdsbroker_start.sh (TLS command bridge, gated)    │
                                         ├─▶ watchdog_start.sh  (keeps the bridge alive)       │
                                         ├─▶ t20_mqtt_start.sh  (the Home Assistant agent)     │
                                         └─▶ privacy_start.sh   (hosts-based upload block)     │
                   └───────────────────────────────────────────────────────────────────────────┘
                                         │ plain MQTT                       │ MQTT/TLS (bridged)
                                         ▼                                  ▼
                              Home Assistant Mosquitto               real Ecovacs cloud (intact)
                                         ▲
                              t20_map_render.py on <HA_HOST> renders the map camera
```

## Components

| Component | Runs on | Language | Role |
|---|---|---|---|
| `mitm_proxy.py` | `<PROXY_HOST>` | Python 3 | Transparent TLS MITM to learn the protocol and the OTA/login flow. |
| `local_broker.py` | `<PROXY_HOST>` | Python 3 | Minimal fake MQTT broker that answers CONNECT/SUBSCRIBE and injects `iot/p2p/shell`. |
| `payload_sshd.sh` | robot (as root) | `/bin/sh` | Copies & binary-patches `sshd`, adds the privsep user, starts key-only root SSH. |
| `usercustomize.py` | robot (boot) | Python 2 | Auto-imported by ROS's Python; launches the boot hooks. Never raises, never blocks ROS. |
| `start_sshd.sh` | robot (boot) | `/bin/sh` | Idempotent reinstaller of the patched sshd. |
| `mdsbroker.py` | robot | Python 2 | On-robot TLS bridge medusa↔cloud; injects `iot/p2p/*` commands via a FIFO; passively sniffs telemetry; can drop analytics packets. |
| `mdsbroker_watchdog.py` | robot | Python 2 | Keeps the bridge alive; fail-safe removes the redirect so medusa is never isolated. |
| `t20_ha_mqtt.py` | robot | Python 2 | The Home Assistant agent: pure-socket MQTT 3.1.1, HA discovery, state, command handling, active polling. |
| `privacy_start.sh` | robot (boot) | `/bin/sh` | Blocks the bulk-upload hosts via `/etc/hosts` (tmpfs → rewritten each boot). |
| `t20_map_render.py` | `<HA_HOST>` | Python 3 | Decodes map fragments and publishes a PNG as an HA camera. |

## Why a bridge *and* an agent?

Two different jobs:

- The **agent** (`t20_ha_mqtt.py`) is the Home Assistant face: it needs **plain MQTT** to HA's
  Mosquitto, which the robot's Python 2.7 can do with raw sockets (it has no `_ssl`, no `paho`).
- The **bridge** (`mdsbroker.py`) is needed because **commands like start/stop/dock are only honoured
  when they arrive on the cloud `iot/p2p/*` channel** that `medusa` is subscribed to. The bridge sits
  transparently between `medusa` and the real cloud (using `openssl s_server`/`s_client`, because the
  robot's Python can't do TLS) and lets us **inject** those commands locally while the cloud keeps
  working. The agent writes a command to the bridge's FIFO; the bridge wraps it and hands it to medusa.

The bridge is **gated** by a flag file (`/data/mdsbroker/ENABLED`). Remove it and reboot → medusa talks
to the cloud directly, as if nothing was ever installed.

## Data flow for one "start cleaning" from Home Assistant

1. You press *Start* in HA → Mosquitto publishes to `t20/<did8>/cmd` = `start`.
2. The on-robot **agent** receives it and writes `start` to the bridge FIFO `/data/mdsbroker/cmd.fifo`.
3. The **bridge** wraps it as `iot/p2p/clean/.../q/<rid>/j` with `{"act":"start","type":"auto"}` and
   writes it into the medusa↔cloud stream.
4. `medusa` turns it into typed ROS messages → the robot leaves the dock.
5. Telemetry (`iot/atr/*`, and the `p` responses) flows back; the bridge **sniffs** it into
   `telemetry.json`; the agent publishes it to HA as entity state.

Nothing extra leaves your LAN; the cloud sees exactly the same traffic it always did.
