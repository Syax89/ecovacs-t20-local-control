# `local-cloud/` — break-in & capture tooling

These run on `<PROXY_HOST>` (a Linux/macOS box on the robot's LAN, Python 3). They are the temporary
scaffolding used to learn the protocol and to deliver the first root shell. After boot persistence is
installed, SSH no longer needs them.

## Files

| File | Purpose |
|---|---|
| `mitm_proxy.py` | Transparent TLS MITM: terminates TLS with your cert, re-resolves the real Ecovacs IP via DNS-over-HTTPS, bridges to the real cloud and logs MQTT in clear text. |
| `local_broker.py` | Minimal fake MQTT broker: answers CONNECT/SUBSCRIBE/PINGREQ and sends `iot/p2p/shell` carrying `payload_sshd.sh` (→ root). |
| `payload_sshd.sh` | The root script the robot runs: copies & binary-patches `sshd`, adds the privsep user, starts key-only root SSH on `:22`. |
| `persistence/usercustomize.py` | Boot hook auto-imported by the robot's ROS Python; launches the installers. |
| `persistence/start_sshd.sh` | Idempotent sshd reinstaller (run at boot). |
| `persistence/privacy_start.sh` | Boot hook that blocks the bulk-upload hosts via `/etc/hosts`. |

## Before you start: generate a TLS cert (not shipped)

The robot does **not** validate the certificate, so a self-signed one is fine. No cert/key is included in
this repo — make your own next to the scripts:

```bash
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout mirror.key -out mirror.crt -days 3650 \
  -subj "/CN=ecouser.net"
```

## Usage

```bash
# 0) In your DNS (AdGuard/Pi-hole/router), point the robot's broker domain(s) at <PROXY_HOST>.
#    Keep it narrow (robot only) so the phone app and Home Assistant keep validating real certs.

# 1) Learn the protocol (robot <-> real cloud, logged in clear):
sudo python3 mitm_proxy.py            # needs :443; edit MY_IP at the top of the file first

# 2) Switch to the fake broker to deliver root:
#    - put YOUR ed25519 public key into payload_sshd.sh (replace the placeholder)
#    - set DID at the top of local_broker.py to your robot's device id
sudo python3 local_broker.py payload_sshd.sh
#    bounce it after a robot reboot/disconnect so the robot reconnects and re-runs the payload.

# 3) You now have root:
ssh -i your_private_key -o IdentitiesOnly=yes root@<ROBOT_IP>
```

> Edit the placeholders first: `MY_IP` (mitm_proxy.py), `DID` (local_broker.py), and your public key
> (`payload_sshd.sh`, `persistence/start_sshd.sh`). File copies to the robot use `scp -O` (its sshd has
> no sftp subsystem).

See [`../../docs/SSH-ROOT.md`](../../docs/SSH-ROOT.md) and [`../../docs/PROTOCOL.md`](../../docs/PROTOCOL.md).
