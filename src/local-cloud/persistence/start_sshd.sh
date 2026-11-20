#!/bin/sh
# Avvia sshd root a CHIAVE sul T20. Dispositivo del proprietario, accesso locale/interoperabilita'.
# Idempotente. Invocato al boot da usercustomize.py (Python di ROS, HOME=/data). Niente flash, niente broker.
exec >>/data/ssh/boot.log 2>&1
echo "=== start_sshd $(date) ==="
# gia' in ascolto? esci.
if netstat -ln 2>/dev/null | grep -q ':22 '; then echo "sshd gia' attivo"; exit 0; fi
mkdir -p /data/ssh
# 1) utente privsep 'sshd' (OpenSSH lo richiede); /etc/passwd e' symlink a tmpfs scrivibile
grep -q '^sshd:' /etc/passwd 2>/dev/null || echo 'sshd:x:99:99:sshd privsep:/data/ssh/empty:/bin/false' >> /etc/passwd
# 2) copia sshd in /data e patcha: path privsep -> /data/ssh/empty ; seccomp KILL(arch+default) -> ALLOW
cp /usr/sbin/sshd /data/ssh/sshd
PY=$(command -v python 2>/dev/null || command -v python2 2>/dev/null)
"$PY" - <<'PYEOF'
import binascii
f="/data/ssh/sshd"; d=open(f,"rb").read()
old=b"/home/release/work/k850/embuild/openssh/install/root/"; new=b"/data/ssh/empty"
new=new+b"\x00"*(len(old)-len(new)); d=d.replace(old,new)
for a,c in [("15000100280000400600000000000000","1500010028000040060000000000ff7f"),
            ("1500000140000000060000000000ff7f0600000000000000","1500000140000000060000000000ff7f060000000000ff7f")]:
    d=d.replace(binascii.unhexlify(a),binascii.unhexlify(c))
open(f,"wb").write(d)
print("patch applicate")
PYEOF
chmod 0755 /data/ssh/sshd
mkdir -p /data/ssh/empty; chmod 0755 /data/ssh/empty
# 3) host key solo se manca
[ -f /data/ssh/hostkey_ed25519 ] || /usr/bin/ssh-keygen -t ed25519 -f /data/ssh/hostkey_ed25519 -N "" </dev/null
# 4) chiave autorizzata (SOLO chiave)
echo "ssh-ed25519 AAAA_REPLACE_WITH_YOUR_OWN_PUBLIC_KEY local-access" > /data/ssh/authorized_keys
chmod 700 /data/ssh; chmod 600 /data/ssh/authorized_keys /data/ssh/hostkey_* 2>/dev/null
cat > /data/ssh/sshd_config <<CFG
Port 22
HostKey /data/ssh/hostkey_ed25519
PermitRootLogin yes
PubkeyAuthentication yes
PasswordAuthentication no
AuthorizedKeysFile /data/ssh/authorized_keys
StrictModes no
PidFile /data/ssh/sshd.pid
CFG
/data/ssh/sshd -f /data/ssh/sshd_config -E /data/ssh/sshd.log
echo "sshd avvio exit=$?"
