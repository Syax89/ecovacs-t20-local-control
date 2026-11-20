# Privacy & security audit

A defensive audit of what the T20 Omni (fw 1.24.0) sends to Ecovacs, from static firmware analysis, a
real transparent-MITM capture, and live inspection over root SSH. Focus: **out-of-context** data — more
than a vacuum needs to send a manufacturer. All real values below are **redacted to placeholders**.

## Two channels to Ecovacs

| Channel | Where | What flows |
|---|---|---|
| **MQTT / TLS** | `jmq-ngiot-<cc>.area.ww.ecouser.net:443` | state telemetry (`iot/atr/on*`), commands (`iot/p2p/*`), **analytics `onFwBuryPoint-bd_*`** |
| **HTTPS bulk upload** | `iotin.ecouser.net` / `iotin-ww.ecouser.net` → `/data_collect/upload/generalData` | **bulk uploads**: maps, furniture, AI photos, wake-word audio, diagnostic logs |

Everything is signed with the device identity (`did`, `mid`, serial), so the data is tied to the account.

## Out-of-context findings

### 🔴 Home Wi-Fi identity (confirmed live)
The autonomous analytics events `bd_online` / `bd_offline` and the `getNetInfo` response send:
- **SSID** (network name, often reveals router brand/model),
- **router BSSID** (the AP's MAC) — a **geolocation vector**: an AP MAC resolves to a physical location
  via Wi-Fi-geolocation databases (Google/Apple/WiGLE). This is the single most sensitive item.
- the robot's **MAC**, **local IP**, **RSSI**, band, and a **history** of connect events with timestamps.

A vacuum has no functional need to report your router's MAC to the manufacturer.

### 🟠 Home floor plan, furniture, trajectory, stats
A ROS node (`liberos_node_postTunnel`, "Reporter") uploads `MajorMap`, `MinorMap`, `MapSubSet`,
`MapTrace`, **`Furniture`**, `Pos`, `Statistic`, `Work` to `data_collect/generalData`; `rwCfg.json` has
`FRSync:true`. A detailed, furnished floor plan of your home, stored in the cloud.

### 🟠 Interior photos (AI obstacle images)
The `objectDetector` plugin produces interior JPEGs; `eros_data_upload.sh` tars `.json` + `.jpg` and
uploads them (optional AES-128-CBC), **then deletes the local copies**. Mitigating note: the detector
itself saves locally; upload goes through the generic feature pipeline.

### 🟠 Microphone audio (wake-word) — subsystem live
`upload_wakeup_audio.sh` uploads the wake-word audio clip (`rn=smartSpeakerWakeup`, voice engine
**Baidu**). The speech path is running on the device. Not continuous streaming — it fires on the
wake-word — but it is a live capability: a clip of your voice leaves toward Ecovacs (and Baidu).

### 🟡 Diagnostic logs
`onlinelog.sh` (ROS logs) and `wifi_repair.sh` (Wi-Fi logs, which include SSID/BSSID) upload archives,
typically on error/diagnostics.

## In-context (normal) data
Battery, charge state, clean mode/speed, water/mop, error codes, station state, schedules, counters,
relative robot/base position. Legitimate telemetry.

## Positive notes (what it does **not** do)
- `settings.yaml` → `SaveImage: 0` (debug vSLAM image saving off).
- `deebot_k960.conf` → `enable_push_sys_info: 0`, `enable_soft_mac: 0`.
- No evidence of continuous audio streaming — audio leaves only on the wake-word.

## Mitigations (with root + DNS/network control)

| Data | Channel | Targeted mitigation | Impact on features |
|---|---|---|---|
| Maps, furniture, AI photos, wake audio, logs | HTTPS `iotin(-ww)` | **Block/sink `iotin.ecouser.net` + `iotin-ww.ecouser.net`** (DNS, or `/etc/hosts` on the robot) | None on MQTT control; you lose only the in-app obstacle-photo gallery and "data sharing" |
| Microphone audio | HTTPS data_collect | Disable the voice assistant **+** block `iotin*` | Lose voice commands |
| SSID / BSSID / MAC | MQTT (same channel as commands) | Bridge **drops `onFwBuryPoint-*` packets** before forwarding | None — bury-points are analytics only |
| Everything cloud | MQTT + HTTPS | `medusa` kill-switch | Lose the app/OTA until local control replaces it |

### The two mitigations applied here (both reversible, zero-risk)

1. **Bulk-upload block** — [`privacy_start.sh`](../src/local-cloud/persistence/privacy_start.sh) adds
   `0.0.0.0 iotin.ecouser.net` and `iotin-ww.ecouser.net` to the robot's `/etc/hosts`. Since `/etc/hosts`
   is tmpfs, the boot hook rewrites it each boot; gated by `/data/privacy_hosts.on`. This kills the
   maps/furniture/AI-images/wake-audio/log exfil **without touching MQTT control or the app**.
2. **MQTT analytics filter** — the bridge, when `/data/mdsbroker/NO_ANALYTICS` is present, **does not
   forward** PUBLISH packets whose topic contains `BuryPoint` (the SSID/BSSID geolocation events). Verified
   dropping real `bd_online`/`bd_offline` while control/telemetry stay intact and `medusa` never flaps.

Reverse either by removing its flag file. The single best privacy/effort move is the DNS block of the two
`iotin*` hosts — it's reversible and risk-free for the robot.

> Why this is safe to do: once you control the robot from Home Assistant (reachable from outside your LAN
> via HA's own remote access), the Ecovacs cloud is no longer needed for remote control, so cutting the
> data exfil costs you nothing operationally.
