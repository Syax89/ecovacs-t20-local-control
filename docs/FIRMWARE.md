# Firmware notes (no binaries here)

This project **does not flash firmware** — root is obtained at runtime (see [SSH-ROOT.md](SSH-ROOT.md)).
The firmware was analysed only to understand the device and to decide whether updating helps. **No
firmware image or decrypted root filesystem is included in this repository**; the decryptor is provided
so you can work with your **own** firmware if you wish.

## Container format (CVE-2024-52331)

Ecovacs firmware for this family is a sequence of **sections**:

```
[ 8-byte header ] [ AES-128-CBC encrypted data ] [ SHA-256 checksum, 64 ASCII hex chars ]
header = 01 01 | type (uint16 LE) | size (uint32 LE)
```

The AES **key and IV are derived deterministically from (section type, section size)** — there is **no
per-device secret**, and the key schedule is publicly known
([CVE-2024-52331](https://www.tenable.com/cve/CVE-2024-52331);
see also [denysvitali/ecovacs-firmware-tools](https://github.com/denysvitali/ecovacs-firmware-tools)).
There is **no cryptographic signature**, only per-section SHA-256 checksums (recomputable).

A working Python port is [`src/firmware-tools/decrypt_ecovacs.py`](../src/firmware-tools/decrypt_ecovacs.py):

```bash
python3 decrypt_ecovacs.py <your_firmware.bin> <output_dir>
```

Typical section layout for this model (contents vary by version):

| # | section | type | note |
|---|---|---|---|
| 0 | `manifest.json` | json | version, MCU/station versions, build date |
| 1 | `pre_upgrade_script.sh` | shell | flashes MCU/DSP |
| 2 | `normal_boot.bin` | boot | ARM64 kernel |
| 3 | `normal_fs.img` | **squashfs** | the robot's Linux root filesystem |
| 4–6 | mcu / mcu_station / sl | bin | microcontroller firmware |
| 7 | dsp | img | audio DSP |
| 8 | `post_upgrade_script.sh` | shell | post-flash cleanup |

## OTA behaviour (why a unit may sit on an older version)

Observed live on the device:

- Auto-OTA is enabled (`/data/config/medusa/enableAutoOta.conf = 1`); `autoOTA.sh` runs nightly (local
  hours 0–6, battery ≥ 20 % and charging) and queries the cloud.
- The OTA check (`mdsctl fw … getOta`) returns the **currently installed** version with "no update",
  even when a newer version exists on the public CDN.
- The manifest URL is `…/class/<zj-model>/firmware/latest.json?sn=<SERIAL>&ver=<cur>&mac=…` — **the
  server decides per serial/region**.

**Conclusion:** a unit that won't update is usually being held back by Ecovacs's **staged rollout**
(per serial/cohort/region), not by anything local. Neither the DNS rewrite nor the MITM touch the OTA
CDN or the `getOta` check (those are forwarded to the real cloud).

## Should you update? (for *this* project's goals)

| Criterion | Effect of updating |
|---|---|
| **Root access** | ⚠️ May **close** the `iot/p2p/shell` hole used for root. No upside. |
| **Local control** | No gain — newer versions are still cloud-`medusa`, with no local NGIoT channel. |
| **Privacy** | No improvement — same telemetry; may add anti-analysis changes. |
| **Device security** | Newer firmware closes known weaknesses — but on your own network those same weaknesses are what give *you* control. |

**Verdict used here: stay on the working version.** Updating helps none of the goals (root, local
control, privacy) and risks closing the door. With persistent root over SSH you get everything at
runtime without flashing.

## Custom firmware (the path NOT taken)

Because there is no signature, a custom firmware image is *technically* buildable (recompute the
checksums). It was **deliberately not pursued**: the installer's runtime acceptance checks can't be fully
verified by static analysis, so a bad flash risks a **brick**, and runtime root already gives everything.
The build scripts for that approach are intentionally **omitted** from this repo to avoid handing out
brick-prone flashing tooling.
