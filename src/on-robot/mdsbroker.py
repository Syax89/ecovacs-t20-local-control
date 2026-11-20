#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# "Bumper minimale" ON-ROBOT: bridge MQTT/TLS tra medusa e il cloud reale Ecovacs,
# con iniezione locale di comandi iot/p2p/* (per start/stop/dock da Home Assistant).
# medusa non valida il cert -> usiamo openssl s_server (il Python del robot non ha _ssl).
# Il cloud resta PIENAMENTE funzionante: tutto il traffico viene inoltrato nei due sensi.
# Dispositivo e rete del proprietario, controllo locale.
#
#   medusa --TLS--> [openssl s_server :443] --plain--> (relay) --plain--> [openssl s_client] --TLS--> cloud
#                                                         ^ iniezione iot/p2p/* via FIFO
#
import os, sys, socket, struct, subprocess, threading, time, json, random, string

HERE = "/data/mdsbroker"
CERT = HERE + "/mirror.crt"; KEY = HERE + "/mirror.key"
LOG  = HERE + "/broker.log"
FIFO = HERE + "/cmd.fifo"          # scrivi qui: nome breve (start/stop/...) OPPURE JSON {"cmd":..,"data":..}
CAPFLAG = HERE + "/CAPTURE"        # se presente: logga i comandi iot/p2p/*/q che l'app invia dal cloud
CAPLOG  = HERE + "/capture.log"    # cattura payload app (per replicare funzioni non in deebot-client)
NOANALYTICS = HERE + "/NO_ANALYTICS"   # se presente: NON inoltra al cloud gli analytics FwBuryPoint (SSID/BSSID)
CLOUD_IP = os.environ.get("CLOUD_IP", "8.211.113.136")  # example (IT region); discover yours with: dig +short jmq-ngiot-<cc>.area.ww.ecouser.net
CLOUD_SNI = "jmq-ngiot-it.area.ww.ecouser.net"
LISTEN_PORT = 443
DID = "00000000-0000-0000-0000-000000000000"; CLS = "p1jij8"; RES = "bIsv"

def daemonize():
    try:
        if os.fork() > 0: os._exit(0)
        os.setsid()
        if os.fork() > 0: os._exit(0)
        f = open(HERE + "/run.out", "a"); di = open(os.devnull, "r")
        os.dup2(di.fileno(), 0); os.dup2(f.fileno(), 1); os.dup2(f.fileno(), 2)
    except Exception: pass

LOG_MAX = 512 * 1024
def log(m):
    line = "%s  %s" % (time.strftime("%H:%M:%S"), m)
    try:
        mode = "w" if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX else "a"
        open(LOG, mode).write(line + "\n")
    except Exception: pass
    print(line); sys.stdout.flush()

# ---------- framing MQTT ----------
def enc_rem(n):
    out = b""
    while True:
        b = n % 128; n //= 128
        if n > 0: b |= 0x80
        out += struct.pack("B", b)
        if n == 0: return out

def mk_publish(topic, payload):
    tb = topic.encode() if isinstance(topic, unicode) else topic
    pb = payload.encode() if isinstance(payload, unicode) else payload
    body = struct.pack(">H", len(tb)) + tb + pb
    return struct.pack("B", 0x30) + enc_rem(len(body)) + body

def rid(n=4):
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))

# topic di comando verso il robot (direzione "q" = richiesta). medusa e' iscritto a questi.
def cmd_topic(cmd):
    return "iot/p2p/%s/hlprLocal/ecosys/1234/%s/%s/%s/q/%s/j" % (cmd, DID, CLS, RES, rid())

def cmd_payload(data):
    hdr = {"pri": 1, "tzm": 120, "ts": str(int(time.time() * 1000)), "ver": "0.0.1"}
    return json.dumps({"header": hdr, "body": {"data": data}})

# mappa nomi brevi -> (cmd, data) protocollo deebot. Oltre a questi, la FIFO accetta
# una riga JSON {"cmd":"setSpeed","data":{"speed":1}} per iniettare QUALSIASI comando.
CMDS = {
    "start":    ("clean",  {"act": "start", "type": "auto"}),
    "stop":     ("clean",  {"act": "stop"}),
    "pause":    ("clean",  {"act": "pause"}),
    "resume":   ("clean",  {"act": "resume"}),
    "charge":   ("charge", {"act": "go"}),
    "locate":   ("playSound", {"count": 1, "sid": 30}),
    "relocate": ("setRelocationState", {"mode": "manu"}),
}

class Bridge(object):
    def __init__(self):
        self.srv = None; self.cli = None
        self.lock = threading.Lock()   # serializza le scritte verso medusa
        self.running = True
        self.tel_t = 0; self.dirty = False   # telemetria sniffata (passiva), persistente tra riconnessioni
        try: self.tel = json.load(open(HERE + "/telemetry.json"))
        except Exception: self.tel = {}

    def write_to_medusa(self, data):
        with self.lock:
            try: self.srv.stdin.write(data); self.srv.stdin.flush()
            except Exception as e: log("write->medusa err: %s" % e)

    # legge pacchetti MQTT interi da un file-like (per non spezzarli durante l'inoltro)
    def read_packet(self, f):
        b0 = f.read(1)
        if not b0: return None
        mult = 1; ln = 0
        while True:
            c = f.read(1)
            if not c: return None
            b = struct.unpack("B", c)[0]; ln += (b & 0x7f) * mult
            if not (b & 0x80): break
            mult *= 128
        rest = b""
        while len(rest) < ln:
            c = f.read(ln - len(rest))
            if not c: return None
            rest += c
        return b0 + enc_rem(ln) + rest

    def pkt_type(self, pkt): return struct.unpack("B", pkt[0])[0] >> 4

    # riconosce i pacchetti analytics FwBuryPoint (SSID/BSSID/uso) da NON inoltrare al cloud
    def _is_analytics(self, pkt):
        try:
            if not os.path.exists(NOANALYTICS): return False
            if struct.unpack("B", pkt[0:1])[0] >> 4 != 3: return False
            i = 1; mult = 1; ln = 0
            while True:
                b = struct.unpack("B", pkt[i:i+1])[0]; i += 1; ln += (b & 0x7f) * mult
                if not (b & 0x80): break
                mult *= 128
            tl = struct.unpack(">H", pkt[i:i+2])[0]; topic = pkt[i+2:i+2+tl]
            if not isinstance(topic, str): topic = topic.decode("utf-8", "ignore")
            if "BuryPoint" in topic or "buryPoint" in topic:
                log("DROP analytics: %s" % topic); return True
        except Exception:
            pass
        return False

    # medusa -> cloud (inoltro grezzo + log delle SUBSCRIBE/CONNECT)
    def up(self):
        while self.running:
            pkt = self.read_packet(self.srv.stdout)
            if pkt is None: log("medusa chiuso"); self.running = False; break
            t = self.pkt_type(pkt)
            if t == 1: log("medusa CONNECT (%d B)" % len(pkt))
            elif t == 8:  # SUBSCRIBE: estrai i topic filtri
                log("medusa SUBSCRIBE: %s" % self._subs(pkt))
            elif t == 3 and self._is_analytics(pkt):
                continue   # analytics FwBuryPoint: NON inoltrare (privacy); medusa prosegue normale
            try: self.cli.stdin.write(pkt); self.cli.stdin.flush()
            except Exception as e: log("write->cloud err: %s" % e); self.running = False; break
            self.sniff(pkt)

    def _subs(self, pkt):
        # parse elementare dei filtri topic in un SUBSCRIBE
        try:
            i = 1; mult = 1; ln = 0
            while True:
                b = struct.unpack("B", pkt[i:i+1])[0]; i += 1; ln += (b & 0x7f) * mult
                if not (b & 0x80): break
                mult *= 128
            body = pkt[i:i+ln]; p = 2  # salta packet id
            subs = []
            while p < len(body):
                tl = struct.unpack(">H", body[p:p+2])[0]; p += 2
                subs.append(body[p:p+tl]); p += tl + 1  # +1 QoS
            return b", ".join(subs)
        except Exception as e:
            return "parse err %s" % e

    # cloud -> medusa (inoltro a pacchetti interi, sotto lock)
    def down(self):
        while self.running:
            pkt = self.read_packet(self.cli.stdout)
            if pkt is None: log("cloud chiuso"); self.running = False; break
            self.write_to_medusa(pkt)
            self.capture_req(pkt)   # logga i comandi app (solo se flag CAPTURE)
            self.sniff(pkt)

    def inject(self, cmd, data, label=None):
        pkt = mk_publish(cmd_topic(cmd), cmd_payload(data))
        self.write_to_medusa(pkt)
        log("INIETTATO %s -> %s %s" % (label or cmd, cmd, json.dumps(data)))

    # FIFO di iniezione comandi. Una riga puo' essere:
    #   - un nome breve in CMDS (start/stop/pause/resume/charge/locate/relocate)
    #   - JSON {"cmd":"setSpeed","data":{"speed":1}}  -> inietta QUALSIASI comando deebot
    def injector(self):
        if not os.path.exists(FIFO):
            os.mkfifo(FIFO)
        while self.running:
            try:
                f = open(FIFO, "r")       # blocca finche' qualcuno scrive
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    if s[0] == "{":
                        try:
                            o = json.loads(s)
                            self.inject(o["cmd"], o.get("data", {}))
                        except Exception as e:
                            log("JSON cmd invalido: %s (%s)" % (s[:120], e))
                    elif s in CMDS:
                        cmd, data = CMDS[s]
                        self.inject(cmd, data, label=s)
                    else:
                        log("comando sconosciuto: %s" % s)
                f.close()
            except Exception as e:
                log("injector err: %s" % e); time.sleep(1)

    # cattura i comandi iot/p2p/*/q che l'app invia (cloud -> medusa), SOLO se flag CAPTURE presente.
    # Serve a raccogliere i payload delle funzioni non modellate da deebot-client (es. stazione).
    def capture_req(self, pkt):
        try:
            if not os.path.exists(CAPFLAG):
                return
            if struct.unpack("B", pkt[0:1])[0] >> 4 != 3:
                return
            i = 1; mult = 1; ln = 0
            while True:
                b = struct.unpack("B", pkt[i:i+1])[0]; i += 1; ln += (b & 0x7f) * mult
                if not (b & 0x80): break
                mult *= 128
            tl = struct.unpack(">H", pkt[i:i+2])[0]; i += 2
            topic = pkt[i:i+tl]; payload = pkt[i+tl:]
            if not isinstance(topic, str):
                topic = topic.decode("utf-8", "ignore"); payload = payload.decode("utf-8", "ignore")
            parts = topic.split("/")
            # tieni solo le RICHIESTE p2p (parts[9]=="q"): sono i comandi che l'app manda
            if len(parts) < 10 or parts[0] != "iot" or parts[1] != "p2p" or parts[9] != "q":
                return
            cmd = parts[2]
            j = payload.find("{")
            body = payload[j:] if j >= 0 else payload
            line = "%s  CMD=%s  PAYLOAD=%s\n" % (time.strftime("%H:%M:%S"), cmd, body)
            mode = "w" if os.path.exists(CAPLOG) and os.path.getsize(CAPLOG) > 256 * 1024 else "a"
            open(CAPLOG, mode).write(line)
            log("CATTURATO comando app: %s" % cmd)
        except Exception:
            pass


    def sniff(self, pkt):
        # best-effort: estrae telemetria dai PUBLISH relayati. Non solleva mai.
        try:
            if struct.unpack("B", pkt[0:1])[0] >> 4 != 3: return
            # salta header fisso + remaining length
            i = 1; mult = 1; ln = 0
            while True:
                b = struct.unpack("B", pkt[i:i+1])[0]; i += 1; ln += (b & 0x7f) * mult
                if not (b & 0x80): break
                mult *= 128
            tl = struct.unpack(">H", pkt[i:i+2])[0]; i += 2
            topic = pkt[i:i+tl]; payload = pkt[i+tl:]
            if not isinstance(topic, str):       # neutro py2/py3
                topic = topic.decode("utf-8", "ignore"); payload = payload.decode("utf-8", "ignore")
            parts = topic.split("/")
            if len(parts) < 3 or parts[0] != "iot": return
            name = parts[2]                      # es. onBattery, getTotalStats, onStationState
            # iot/p2p/<cmd>/.../<q|p>/<rid>/<fmt>: tieni solo le RISPOSTE (p), non le richieste
            if parts[1] == "p2p" and (len(parts) < 10 or parts[9] != "p"): return
            j = payload.find("{")
            if j < 0: return
            obj = json.loads(payload[j:])
            data = obj.get("body", {}).get("data", obj.get("data"))
            if data is None: return
            self.tel[name] = data; self.dirty = True
            # cambio mappa attiva: scarta i frammenti vecchi (il cache resta legato al mid corrente)
            if name in ("getMajorMap", "onMajorMap") and isinstance(data, dict) and data.get("mid"):
                if self.tel.get("_map_mid") != data.get("mid"):
                    self.tel["_map_mid"] = data.get("mid"); self.tel["minorPieces"] = {}
            # frammenti mappa: accumula per indice (altrimenti si sovrascrivono) e scrivi SUBITO
            if name in ("getMinorMap", "onMinorMap") and isinstance(data, dict) \
               and data.get("pieceIndex") is not None and data.get("pieceValue"):
                mp = self.tel.get("minorPieces")
                if not isinstance(mp, dict): mp = {}; self.tel["minorPieces"] = mp
                mp[str(data.get("pieceIndex"))] = data.get("pieceValue")
                self.tel_t = 0   # bypassa il throttle: scrivi subito
            self.flush()
        except Exception:
            pass

    def flush(self):
        # scrive telemetry.json (atomico) al massimo ogni 3s; richiamato anche dal loop principale
        try:
            if not self.dirty or time.time() - self.tel_t < 3: return
            self.tel_t = time.time(); self.dirty = False
            tmp = HERE + "/telemetry.json.tmp"
            open(tmp, "w").write(json.dumps(self.tel))
            os.rename(tmp, HERE + "/telemetry.json")
        except Exception:
            pass

    def run(self):
        log("=== mdsbroker start: in ascolto 443, attendo medusa ===")
        # downstream TLS: accetta medusa (upstream aperto SOLO dopo il 1o pacchetto -> no churn)
        self.srv = subprocess.Popen(
            ["openssl", "s_server", "-quiet", "-accept", str(LISTEN_PORT),
             "-cert", CERT, "-key", KEY, "-naccept", "1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open("/dev/null", "w"), bufsize=0)
        first = self.read_packet(self.srv.stdout)   # blocca finche' medusa non invia CONNECT
        if first is None:
            log("nessun client, chiudo")
            try: self.srv.kill()
            except Exception: pass
            return
        log("medusa connessa (1o pkt %d B) -> apro upstream cloud" % len(first))
        self.cli = subprocess.Popen(
            ["openssl", "s_client", "-quiet", "-connect", "%s:443" % CLOUD_IP,
             "-servername", CLOUD_SNI],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open("/dev/null", "w"), bufsize=0)
        self.cli.stdin.write(first); self.cli.stdin.flush()
        for fn in (self.up, self.down, self.injector):
            th = threading.Thread(target=fn); th.daemon = True; th.start()
        while self.running:
            time.sleep(1); self.flush()
        self.tel_t = 0; self.flush()
        for p in (self.srv, self.cli):
            try: p.kill()
            except Exception: pass
        log("=== mdsbroker stop ===")

if __name__ == "__main__":
    if "--daemon" in sys.argv: daemonize()
    while True:
        try: Bridge().run()
        except Exception as e: log("run err: %s" % e)
        time.sleep(2)   # medusa si riconnette -> nuovo ciclo
