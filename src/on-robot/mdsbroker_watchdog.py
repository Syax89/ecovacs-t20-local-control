#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Watchdog del mini-broker comandi del T20. Mantiene vivo il broker e, se non riparte,
# fa FAIL-SAFE: toglie il redirect /etc/hosts cosi' medusa torna al cloud DIRETTO (mai isolata).
# Rispetta il flag /data/mdsbroker/ENABLED (kill-switch). Dispositivo del proprietario.
import os, sys, time, subprocess

B = "/data/mdsbroker"; FLAG = B + "/ENABLED"; LOG = B + "/watchdog.log"
HOSTS = "/etc/hosts"; HOST = "jmq-ngiot-it.area.ww.ecouser.net"
LINE = "127.0.0.1 " + HOST
CHECK = 20          # secondi tra i controlli
MAX_FAILS = 3       # tentativi di riavvio prima del fail-safe

def log(m):
    try:
        mode = "w" if os.path.exists(LOG) and os.path.getsize(LOG) > 256 * 1024 else "a"
        open(LOG, mode).write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), m))
    except Exception: pass

def daemonize():
    try:
        if os.fork() > 0: os._exit(0)
        os.setsid()
        if os.fork() > 0: os._exit(0)
        f = open(LOG, "a"); di = open(os.devnull, "r")
        os.dup2(di.fileno(), 0); os.dup2(f.fileno(), 1); os.dup2(f.fileno(), 2)
    except Exception: pass

def _procs_with(substr):
    me = os.getpid(); out = []
    for p in os.listdir("/proc"):
        if not p.isdigit() or int(p) == me: continue
        try: cl = open("/proc/%s/cmdline" % p).read().replace("\0", " ")
        except Exception: continue
        if substr in cl: out.append(int(p))
    return out

def broker_running(): return len(_procs_with("mdsbroker.py")) > 0
def watchdog_dups():  return _procs_with("mdsbroker_watchdog.py")

def hosts_has():
    try: return HOST in open(HOSTS).read()
    except Exception: return False
def hosts_add():
    if not hosts_has():
        try: open(HOSTS, "a").write(LINE + "\n"); log("hosts redirect aggiunto")
        except Exception as e: log("hosts_add err %s" % e)
def hosts_del():
    if hosts_has():
        try:
            ls = [l for l in open(HOSTS) if HOST not in l]
            open(HOSTS, "w").writelines(ls); log("hosts redirect rimosso -> cloud diretto")
        except Exception as e: log("hosts_del err %s" % e)

def main():
    log("=== watchdog avviato (pid %d) ===" % os.getpid())
    fails = 0
    while True:
        time.sleep(CHECK)
        if not os.path.exists(FLAG):          # kill-switch: broker disattivato
            hosts_del(); continue
        if broker_running():
            if fails: log("broker di nuovo attivo"); fails = 0
            hosts_add()                        # self-heal: assicura il redirect
        else:
            fails += 1; log("broker giu (fail #%d)" % fails)
            if fails > MAX_FAILS and hosts_has():
                log("FAIL-SAFE: broker giu -> medusa al cloud diretto (continuo a ritentare)")
                hosts_del()                    # non lasciare medusa isolata mentre e' giu
            subprocess.call(["/bin/sh", B + "/mdsbroker_start.sh"])   # ritenta sempre: self-heal

if __name__ == "__main__":
    if "--daemon" in sys.argv: daemonize()
    # istanza singola
    if watchdog_dups():
        log("watchdog gia' attivo, esco"); sys.exit(0)
    main()
