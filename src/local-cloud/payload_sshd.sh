#!/bin/sh
# Avvia sshd (SOLO chiave) sul T20. Dispositivo del proprietario, accesso locale per interoperabilita'.
# Questa build di OpenSSH 7.9p1 ha il path privsep compilato su un dir di build inesistente
# (/home/release/.../root) e /home non e' creabile su / read-only. Soluzione: copia sshd in /data
# e patcha la stringa del path privsep -> /data/ssh/empty (privsep resta ATTIVA).
exec >/data/diag.txt 2>&1
set -x
id; uname -a

pkill -f /data/ssh/sshd 2>/dev/null; sleep 1
if false; then echo skip; else

mkdir -p /data/ssh
chmod 0755 /data /data/ssh 2>/dev/null
ls -ld /data /data/ssh

# 1) utente privsep 'sshd' (OpenSSH lo richiede). /etc/passwd e' symlink a tmpfs (scrivibile).
grep -q '^sshd:' /etc/passwd 2>/dev/null || echo 'sshd:x:99:99:sshd privsep:/data/ssh/empty:/bin/false' >> /etc/passwd
echo "-- id sshd --"; id sshd 2>&1

# 2) copia sshd in /data e patcha il path privsep -> /data/ssh/empty
cp /usr/sbin/sshd /data/ssh/sshd
PY=$(command -v python 2>/dev/null || command -v python2 2>/dev/null || command -v python3 2>/dev/null)
echo "python: $PY"
"$PY" - <<'PYEOF'
import binascii
f="/data/ssh/sshd"
d=open(f,"rb").read()
old=b"/home/release/work/k850/embuild/openssh/install/root/"
new=b"/data/ssh/empty"
new=new+b"\x00"*(len(old)-len(new))
n=d.count(old)
d=d.replace(old,new)
# neutralizza il sandbox seccomp-BPF. Il filtro controlla arch==AUDIT_ARCH_ARM(32bit) ma il
# processo e' aarch64 -> ogni syscall fallisce il check e colpisce il KILL del mismatch arch.
# Patch: KILL del mismatch arch -> ALLOW (e anche il KILL di default, per sicurezza).
m=0
for a,c in [
  ("15000100280000400600000000000000","1500010028000040060000000000ff7f"),
  ("1500000140000000060000000000ff7f0600000000000000","1500000140000000060000000000ff7f060000000000ff7f"),
]:
    ao=binascii.unhexlify(a); co=binascii.unhexlify(c)
    k=d.count(ao); m+=k
    d=d.replace(ao,co)
open(f,"wb").write(d)
print("patch privsep: %d occ (old=%dB); patch seccomp: %d occ"%(n,len(old),m))
PYEOF
chmod 0755 /data/ssh/sshd
mkdir -p /data/ssh/empty; chown 0:0 /data/ssh/empty 2>/dev/null; chmod 0755 /data/ssh/empty
# verifica che la nuova stringa sia nel binario
strings /data/ssh/sshd 2>/dev/null | grep -E '/data/ssh/empty|embuild' | head

# 3) host key solo se manca (evita il prompt "Overwrite?")
[ -f /data/ssh/hostkey_ed25519 ] || /usr/bin/ssh-keygen -t ed25519 -f /data/ssh/hostkey_ed25519 -N "" </dev/null
echo "ssh-ed25519 AAAA_REPLACE_WITH_YOUR_OWN_PUBLIC_KEY local-access" > /data/ssh/authorized_keys
chmod 700 /data/ssh; chmod 600 /data/ssh/authorized_keys /data/ssh/hostkey_* 2>/dev/null

# 4) config (senza UsePAM, non supportata da questa build)
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

rm -f /data/ssh/sshd.log /data/ssh/sshd_debug.log
echo "-- avvio sshd patchato --"
/data/ssh/sshd -f /data/ssh/sshd_config -E /data/ssh/sshd.log
echo "exit sshd=$?"
sleep 1
echo "-- sshd.log --"; cat /data/ssh/sshd.log 2>/dev/null
fi

echo "-- porte in ascolto --"; netstat -ln 2>/dev/null | grep -E ':22 |:8099 ' | head
pidof httpd >/dev/null 2>&1 || busybox httpd -p 8099 -h /data
echo "fine payload"
