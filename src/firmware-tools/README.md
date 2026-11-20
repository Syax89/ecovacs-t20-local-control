# `firmware-tools/` — firmware decryptor

`decrypt_ecovacs.py` decrypts and extracts an Ecovacs firmware container into its sections. It is a
Python port of **public** research
([CVE-2024-52331](https://www.tenable.com/cve/CVE-2024-52331);
[denysvitali/ecovacs-firmware-tools](https://github.com/denysvitali/ecovacs-firmware-tools)): the AES
key/IV are derived from each section's (type, size), with no per-device secret.

```bash
pip install pycryptodome
python3 decrypt_ecovacs.py <your_firmware.bin> <output_dir>
```

> This repository ships **no firmware images and no extracted filesystem**. Use this only with firmware
> you obtained for your **own** device. Background and the "should you update?" verdict are in
> [`../../docs/FIRMWARE.md`](../../docs/FIRMWARE.md). This project does **not** flash firmware — root is
> obtained at runtime.
