# Ecovacs Deebot T20 Omni — Local Control & Privacy Audit

A complete, reproducible write-up of how one **Ecovacs Deebot T20 Omni** robot vacuum was brought
under **full local control** — root access, live control and map through **Home Assistant**, and
targeted **privacy mitigations** — **without flashing firmware, without a UART cable, and without
depending on the Ecovacs cloud** for day-to-day operation.

This repository is a **guide and the code we wrote**. It contains **no firmware binaries, no private
keys, and no captured data** from the device. Every device-specific identifier has been replaced with
a placeholder so you can reproduce the project on **your own** robot from scratch.

> **Ethics & scope.** All of this was done on a device, a network and an account **owned by the author**,
> for interoperability and to audit what the device sends to the manufacturer. Do this only on hardware
> you own. See [`DISCLAIMER.md`](DISCLAIMER.md).

📖 **Step-by-step reproduction guide → the [Wiki](../../wiki).**
🧠 **Reference docs → [`docs/`](docs/).**  🧩 **The code → [`src/`](src/).**

---

## TL;DR — what was achieved

- 🔓 **Root shell on the robot** (key-only SSH, uid 0), with **no flash, no UART, no brick risk**,
  obtained over the LAN by abusing a cloud command the robot already trusts.
- ♻️ **Boot persistence** of that access, surviving a real reboot, using only the writable `/data`
  partition — the read-only root filesystem is never touched. Fully reversible.
- 🏠 **Home Assistant integration** with **~60 entities** (vacuum, fan/water/mode selects, station
  actions, per-room cleaning, 20+ sensors) — using the **same `iot/p2p/*` channel the official app
  uses**, with the Ecovacs cloud left fully working in parallel.
- 🗺️ **Live map rendered locally** into a Home Assistant camera entity (floor/walls + robot/charger +
  cleaning path), decoding the robot's own 7z/LZMA map fragments.
- 🕵️ **Privacy audit** of everything the robot sends to Ecovacs, plus **two reversible mitigations**
  that cut the out-of-context data (Wi-Fi geolocation, home floor plan, AI photos, wake-word audio)
  without breaking control or the app.

---

## The result, in one picture

```
                        YOUR LAN
  ┌───────────────────────────────────────────────────────────────────────┐
  │                                                                         │
  │   ┌───────────────┐        MQTT/TLS :443         ┌──────────────────┐   │
  │   │  Deebot T20    │ ───────────────────────────▶│  on-robot bridge │   │
  │   │  (medusa cloud │◀─────────────────────────── │  (mdsbroker.py)  │   │
  │   │   daemon)      │   iot/p2p/*  &  iot/atr/*    │  injects commands│   │
  │   └──────┬────────┘                               │  sniffs telemetry│   │
  │          │ runs our agent (t20_ha_mqtt.py)        └────────┬─────────┘   │
  │          │ plain MQTT 3.1.1, no TLS                        │             │
  │          ▼                                        TLS bridge│ (transparent)
  │   ┌──────────────────┐   HA discovery + state     ┌─────────▼─────────┐  │
  │   │ Home Assistant   │◀──────────────────────────│  REAL Ecovacs     │  │
  │   │ Mosquitto broker │   ~60 entities + map       │  cloud (intact)   │  │
  │   └──────────────────┘                            └───────────────────┘  │
  │          ▲  renders /map/raw → PNG (t20_map_render.py)                    │
  └──────────┼───────────────────────────────────────────────────────────────┘
             └── map camera entity
```

Everything that matters runs **on the robot** or **on your HA host**. The cloud path is kept only so
the phone app and OTA keep working; it can be cut entirely with a documented kill-switch.

---

## The journey (how we got in)

This is the chain the Wiki walks you through, step by step:

1. **DNS redirect.** Point the robot's cloud domains (`jmq-ngiot-it.area.ww.ecouser.net`,
   `api-rapp.cn.ecouser.net`, …) at a machine on your LAN using AdGuard Home / Pi-hole / your router.
   Keep it **narrow** — only the robot — so the phone app and Home Assistant (which *do* validate TLS)
   keep working.
2. **Transparent MITM.** A small proxy ([`src/local-cloud/mitm_proxy.py`](src/local-cloud/mitm_proxy.py))
   terminates TLS with a self-signed cert, re-resolves the **real** Ecovacs IP over DNS-over-HTTPS, and
   bridges the robot to the real cloud **while logging everything in clear text**. This works because
   **the robot does not validate the broker's TLS certificate.**
3. **Protocol.** The captured traffic is plain **Ecovacs MQTT** (`iot/atr/*` telemetry, `iot/p2p/*`
   request/response) — the exact protocol the open-source **deebot-client** library implements.
4. **The discovery: `iot/p2p/shell`.** The cloud routinely sends the robot a command whose payload is a
   **base64 `/bin/sh` script that the robot executes as root**. If we own the broker, we can send our
   own `shell` command → **remote root over the LAN.**
5. **A working sshd.** OpenSSH 7.9p1 ships on the robot but is crippled by three build-specific bugs
   (a nonexistent privsep path, a missing `sshd` user, and a **seccomp-BPF sandbox built for the wrong
   CPU architecture** that SIGSYS-kills the pre-auth child). We copy `sshd` to `/data` and binary-patch
   it — details in [`docs/SSH-ROOT.md`](docs/SSH-ROOT.md).
6. **Persistence.** ROS starts Python as root at boot with `HOME=/data` and user-site enabled, so a
   `usercustomize.py` we drop in `/data` is auto-imported on every boot and re-installs sshd + the
   bridge. No flash, reversible by deleting one file.
7. **Local control → Home Assistant.** An agent on the robot speaks plain MQTT to HA's Mosquitto and
   injects `iot/p2p/*` commands through the on-robot bridge, giving full app parity locally.
8. **The map, locally.** The agent publishes raw map fragments; a renderer on the HA host decodes them
   and publishes a PNG as an HA camera.
9. **Privacy mitigations.** Block the bulk-upload hosts and drop the analytics (`FwBuryPoint`) packets
   that carry Wi-Fi geolocation — both reversible, zero-risk.

---

## What's in this repository

```
.
├── README.md                 ← you are here
├── DISCLAIMER.md             ← ethics, legality, safety
├── LICENSE                   ← MIT (code) / docs CC-BY-4.0
├── docs/                     ← reference documentation
│   ├── ARCHITECTURE.md       ← components & data flow
│   ├── PROTOCOL.md           ← the Ecovacs MQTT protocol, captured & explained
│   ├── SSH-ROOT.md           ← the iot/p2p/shell root + the 3 OpenSSH blockers + persistence
│   ├── HOME-ASSISTANT.md     ← the bridge, the agent, the ~60 entities
│   ├── MAP-RENDERING.md      ← decoding and drawing the map
│   ├── PRIVACY.md            ← data-collection audit + mitigations
│   ├── HARDWARE.md           ← full device spec sheet (model-level)
│   └── FIRMWARE.md           ← firmware container notes (CVE-2024-52331), no binaries
└── src/                      ← the code we wrote (sanitised)
    ├── local-cloud/          ← MITM proxy, fake broker, SSH payload, boot hooks
    ├── on-robot/             ← the HA agent, the command bridge, the watchdog
    ├── map-renderer/         ← map → PNG service (Docker / systemd)
    └── firmware-tools/       ← firmware decryptor (port of public research)
```

> **Deliberately not included:** firmware images (`.bin`), the decrypted root filesystem, TLS keys, SSH
> private keys, MQTT captures, and anything tied to a real device/account. You generate your own.

---

## Reproduce it on your robot

Full instructions are in the **[Wiki](../../wiki)**. In broad strokes you will need:

- An Ecovacs **Deebot T20 Omni** (class `p1jij8`) **that you own**, on firmware where the
  `iot/p2p/shell` command is still honoured (this project was done on **1.24.0**).
- A machine on the robot's LAN (Linux or macOS) to run the MITM/broker, plus Python 3.
- A DNS server you control for the redirect (AdGuard Home, Pi-hole, or a capable router).
- A **Home Assistant** instance with the **Mosquitto** broker add-on (for local control + map).

The headline chain once you have all that:

```bash
# 1) generate a self-signed cert the robot will happily accept (it doesn't validate it)
openssl req -x509 -newkey rsa:2048 -nodes -keyout mirror.key -out mirror.crt -days 3650 -subj "/CN=ecouser.net"

# 2) redirect the robot's broker domain to <PROXY_HOST> in your DNS, then capture:
sudo python3 src/local-cloud/mitm_proxy.py          # learn the protocol in clear text

# 3) swap the MITM for the fake broker to deliver a root shell:
sudo python3 src/local-cloud/local_broker.py src/local-cloud/payload_sshd.sh
ssh -i your_key -o IdentitiesOnly=yes root@<ROBOT_IP>   # you are root
```

From there you install the boot hooks and the Home Assistant agent — see the Wiki.

---

## Key technical findings

| Finding | Why it matters |
|---|---|
| The robot **does not validate the broker's TLS certificate** | A LAN machine can impersonate the Ecovacs broker and MITM all traffic. |
| `iot/p2p/shell` executes a **base64 script as root** | Whoever controls the broker gets **remote root** — no flash, no UART. |
| OpenSSH's **seccomp-BPF filter checks `AUDIT_ARCH_ARM` on an aarch64 build** | Every syscall mismatches → pre-auth child is SIGSYS-killed; a 1-byte class patch re-enables sshd. |
| Analytics `FwBuryPoint-bd_online/bd_offline` leak **SSID + router BSSID** | Router BSSID is a **geolocation vector** (resolvable to a physical address). |
| Bulk `data_collect/upload/generalData` carries **maps, furniture, AI interior photos, wake-word audio** | Out-of-context data a vacuum does not need to send a manufacturer. |

Details and evidence (with real values redacted) are in [`docs/PRIVACY.md`](docs/PRIVACY.md) and
[`docs/PROTOCOL.md`](docs/PROTOCOL.md).

---

## What stays local vs. cloud-only

**Fully local:** start/stop/pause/dock, fan/water/mode, volume, per-room cleaning, station actions
(empty/wash/dry/self-clean), all functional telemetry, schedules (read), and the **map image**.
Obstacle avoidance / TrueDetect is on-device and keeps working offline.

**Cloud-only by nature:** access from outside your LAN (do it via HA instead), the YIKO voice assistant's
command understanding, the obstacle-photo gallery, OTA updates, and account/sharing/push.

---

## Credits & prior art

- **[deebot-client](https://github.com/DeebotUniverse/Deebot-4-Home-Assistant)** — the open-source
  implementation of the Ecovacs MQTT protocol this project leans on.
- **[dontvacuum.me](https://dontvacuum.me/) (Dennis Giese)** — foundational Ecovacs/robot-vacuum
  security research, firmware format, and rooting techniques.
- **[CVE-2024-52331](https://www.tenable.com/cve/CVE-2024-52331)** and
  **[denysvitali/ecovacs-firmware-tools](https://github.com/denysvitali/ecovacs-firmware-tools)** — the
  firmware encryption scheme the decryptor in `src/firmware-tools/` is ported from.

## License

Code is released under the **MIT License**; documentation under **CC-BY-4.0**. See [`LICENSE`](LICENSE).
