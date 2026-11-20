# Hardware & platform (Deebot T20 Omni)

Model-level facts (no device-specific identifiers). Useful to know what you're working with and to adapt
the techniques to a sibling model.

## Compute / memory / storage

| Component | Detail |
|---|---|
| **SoC** | Rockchip **RK3326** (base board "RK3326 EVB") |
| **CPU** | ARM **Cortex-A35 quad-core**, aarch64 |
| **ISA/crypto** | fp, asimd, **aes, pmull, sha1, sha2, crc32** |
| **RAM** | **512 MB**, no swap |
| **Storage** | eMMC/NAND; rootfs **squashfs ~59 MB read-only** + **`/data` ext4 ~231 MB** |
| **Kernel** | Linux **4.4.185** aarch64, SMP PREEMPT |
| **Userland** | **ROS** ("eros" variant) + **BusyBox** + **Python 2.7**; hostname `deboot` |

### Flash layout (dual-slot A/B — explains OTA)
`uboot · trust · eco · reserve · sys · boot1/boot2 (A/B) · rootfs1/rootfs2 (A/B) · data`
→ OTA writes the inactive slot and switches.

## Sensors & peripherals

| Subsystem | Detail |
|---|---|
| **Camera** | SmartSens **SC031GS** — global shutter, **grayscale 1920×1080 @15 fps**. Obstacle/AIVI only; **no RGB, no `/dev/video*`, no streaming server** on this model (there is no live video feature). |
| **LiDAR** | rotating laser on UART (`ttyS*`) → SLAM/mapping. |
| **Audio out** | Everest **ES8156** codec (I2S). |
| **Mic + voice DSP** | **Baidu** audio DSP (wake-word / assistant). |
| **Other** | line-laser, ultrasonic, gyro (visible via bury-points). |

## Networking

| | |
|---|---|
| **Wi-Fi** | Realtek **RTL8189FS** (SDIO) — **2.4 GHz only**. `wlan0` = client; `wlan1` = AP used for pairing. |
| **Bluetooth** | not active at OS level (pairing uses the Wi-Fi AP + smartconfig). |
| **USB** | Rockchip gadget (adb/fastboot possible via USB if re-enabled). |

## Power / station

| | |
|---|---|
| **Battery** | Li-ion; live state via MCU (`{"battery":…, "inCharging":…}`). |
| **Omni station** | ROS node `omni` + `plug_station.so` (wash/dry/empty, steriliser, hot water). |

## Software identity

- Cloud daemon **`medusa`** (`/usr/bin/medusa` + `/usr/lib/medusa/*.so`); control socket
  `/tmp/mds_cmd.sock` (`mdsctl`, JSON).
- App class **`p1jij8`**, internal product **`zj2231`**.
- Network services are **off by default** in `/etc/rc.conf` (`!adbd.sh !vsftpd.sh !autostart.sh`); sshd
  present but disabled; a UART `getty` exists on `ttyFIQ0` (last-resort root path, not used here).
- `/etc/passwd`, `/var/{run,lib,log,spool}` are symlinks to a writable tmpfs under `/tmp/root` — which is
  why the privsep-user and `/etc/hosts` edits are possible without touching the RO rootfs.

## Why there's no "official" local control

The newer Ecovacs **NGIoT** local-P2P stack (multicast `224.0.1.187`, CoAP `5683/5684`, WebSocket/DTLS,
PSK) is **not implemented** on the T20 — a full-rootfs search finds none of its primitives. The T20 uses
the classic cloud `medusa` MQTT stack only. That's why the winning path is **root + speak the cloud
protocol locally**, rather than reimplementing NGIoT.
