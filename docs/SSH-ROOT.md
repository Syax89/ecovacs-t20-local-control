# Root access via `iot/p2p/shell`, and a working sshd

This is the heart of the project: how to get a **key-only root SSH shell** on the robot with **no
firmware flash, no UART, and no brick risk**, and how to make it **survive reboots**.

## The idea

The robot's cloud daemon (`medusa`) connects to an MQTT broker over TLS **without validating the
certificate**. If you redirect its broker domain to a machine you control (see the Wiki, "DNS redirect")
and present any TLS certificate, the robot connects to **your** broker. The cloud protocol includes
`iot/p2p/shell`, which the robot executes **as root**. So your broker simply sends a `shell` command
whose payload is your own script.

```
robot ──DNS rewrite──▶ local_broker.py  ──iot/p2p/shell (base64 script)──▶ robot runs it as root
```

[`src/local-cloud/local_broker.py`](../src/local-cloud/local_broker.py) is a ~90-line MQTT server that
answers `CONNECT`/`SUBSCRIBE`/`PINGREQ` and, once the robot subscribes, publishes the `shell` command
carrying [`src/local-cloud/payload_sshd.sh`](../src/local-cloud/payload_sshd.sh).

## Why a stock `sshd` doesn't just work

The firmware **ships** OpenSSH 7.9p1 (`/usr/sbin/sshd`) but it is disabled, and three build-specific
problems stop it from running even once launched. All three are solved **without touching the read-only
squashfs root filesystem** — only `/data`, tmpfs, and the symlinked `/etc/passwd`.

### Blocker 1 — privsep directory points at a nonexistent build path

The binary has its privilege-separation directory hard-compiled to
`/home/release/work/k850/embuild/openssh/install/root/`. `/home` doesn't exist on the device and `/` is
read-only, so privsep can't `chroot` there.

**Fix:** copy `sshd` to `/data/ssh/sshd` and **binary-patch the path string** to `/data/ssh/empty`
(same length, NUL-padded), then create that empty dir. Privsep stays **enabled**.

### Blocker 2 — missing privsep user `sshd`

OpenSSH needs an unprivileged `sshd` user for privilege separation; it doesn't exist on the device.

**Fix:** `/etc/passwd` is a symlink to a **writable tmpfs** copy, so append:

```
sshd:x:99:99:sshd privsep:/data/ssh/empty:/bin/false
```

### Blocker 3 — the real one: a seccomp-BPF sandbox built for the wrong architecture

This is the subtle bug. OpenSSH's privsep child installs a **seccomp-BPF** syscall filter. The filter's
first instruction checks the audit architecture:

```
if (arch != AUDIT_ARCH_ARM)  -> SECCOMP_RET_KILL
```

But `AUDIT_ARCH_ARM` is **0x40000028** (32-bit ARM), while the binary and the running process are
**aarch64** (`AUDIT_ARCH_AARCH64` = **0xC00000B7**). So **every** syscall fails the architecture check
and hits the mismatch `SECCOMP_RET_KILL` path → the pre-auth child is **SIGSYS-killed during key
exchange**, and the connection dies right after the banner.

**Fix:** binary-patch the two relevant BPF return statements from `KILL` to `ALLOW`. In the shipped
binary these are the `RET_KILL` for the arch mismatch and the default `RET_KILL`:

```python
# (py2.7-safe: use binascii.unhexlify, the robot has no bytes.fromhex)
for old, new in [
    ("15000100280000400600000000000000", "1500010028000040060000000000ff7f"),
    ("1500000140000000060000000000ff7f0600000000000000",
     "1500000140000000060000000000ff7f060000000000ff7f"),
]:
    data = data.replace(unhexlify(old), unhexlify(new))
```

`0x7fff0000` is `SECCOMP_RET_ALLOW`; `0x00000000` is `SECCOMP_RET_KILL`. Patching these neutralises the
broken sandbox while leaving privilege separation itself intact.

> ⚠️ These byte offsets/sequences are **specific to this firmware build** of `sshd`. On a different
> build they will differ — locate the BPF program (the `AUDIT_ARCH_*` constants are good anchors) and
> patch the equivalent returns. The method generalises; the exact bytes do not.

## Putting it together

[`payload_sshd.sh`](../src/local-cloud/payload_sshd.sh) does, as root:

1. add the `sshd` user to `/etc/passwd` (tmpfs);
2. `cp /usr/sbin/sshd /data/ssh/sshd` and apply the privsep-path + seccomp patches (in py2.7);
3. generate an ed25519 host key **only if missing**;
4. install **your** public key to `/data/ssh/authorized_keys` (replace the placeholder!);
5. write a minimal `sshd_config` (key-only, `PermitRootLogin yes`, no PAM) and start sshd on `:22`.

Then connect:

```bash
ssh -i your_private_key -o IdentitiesOnly=yes root@<ROBOT_IP>
```

`IdentitiesOnly=yes` matters (your agent may otherwise offer the wrong key first). Note the robot's sshd
has **no sftp subsystem**, so use `scp -O` for file copies.

## Boot persistence (no flash, reversible)

Verified with a **real reboot while the fake broker was off**: sshd comes back on its own.

**Mechanism:** at boot, ROS (`roscore`/`rosmaster`) starts Python **as root with `HOME=/data`** and
Python's *user site* enabled. Python therefore auto-imports `usercustomize` from
`/data/.local/lib/python2.7/site-packages/`. We drop
[`usercustomize.py`](../src/local-cloud/persistence/usercustomize.py) there; it launches (detached, so
it never blocks ROS) the idempotent installer
[`start_sshd.sh`](../src/local-cloud/persistence/start_sshd.sh), plus the other boot hooks (bridge,
watchdog, agent, privacy block). Each hook is guarded by a one-shot flag in `/tmp` so it runs once per
boot, and `usercustomize.py` **never raises**.

Files on the robot (all under the persistent `/data`):

```
/data/.local/lib/python2.7/site-packages/usercustomize.py   ← the boot hook
/data/ssh/start_sshd.sh                                      ← idempotent sshd installer
/data/ssh/{sshd, authorized_keys, hostkey_ed25519, sshd_config}
```

**To remove persistence:** delete `usercustomize.py` (and optionally `/data/ssh/`). Nothing else was
touched; the root filesystem is intact.

> ⚠️ A **factory reset** wipes all of `/data`, so you lose root, the agent, the bridge and the Wi-Fi
> config. Recovery = re-pair with the app, then repeat the break-in. Don't factory reset.
