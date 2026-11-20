# Disclaimer — ethics, legality, and safety

## Ownership and intent

This project documents interoperability and security-research work performed by the author on a device,
a local network and a manufacturer account that **the author owns**. The goal is twofold:

1. **Interoperability** — to control a device you bought, locally, without a mandatory cloud dependency.
2. **Transparency** — to audit and understand what the device sends to the manufacturer.

Both are legitimate activities on your own property in most jurisdictions. Nothing here is intended to
help anyone access, control, or extract data from a device they do not own.

## Do this only on hardware you own

Every technique in this repository assumes **physical and network control of your own robot**. Using
these techniques against a device you do not own — or against someone else's network or account — may be
illegal and is **not** condoned. The author and contributors accept no responsibility for misuse.

## No warranty, real risk

Modifying an embedded device can **brick it, void its warranty, or cause unexpected behaviour**. The
approach documented here was specifically chosen to **minimise** that risk:

- It uses **no firmware flashing** and **no UART** — the read-only root filesystem is never modified.
- All changes live on the writable `/data` partition and are **reversible** (delete one boot hook).
- The manufacturer cloud is left **fully functional**; a kill-switch is documented but optional.

Even so, you proceed **at your own risk**. Keep backups, understand each step before running it, and do
not run commands you have not read.

## Responsible disclosure

The security-relevant findings referenced here (no TLS certificate validation on the device-to-broker
link; a root-capable `shell` command on a cloud channel; a mis-architected seccomp sandbox; bulk upload
of out-of-context data) build on **publicly documented** research and public CVEs. This repository is a
defensive/interoperability write-up, not a weaponised exploit: it ships **no firmware, no keys, and no
captured data**, and it targets **the owner's own device**.

## Trademarks

"Ecovacs", "Deebot", and related names are trademarks of their respective owners. This project is
**not affiliated with, endorsed by, or sponsored by** Ecovacs or any other company. Product names are
used only for identification and interoperability.
