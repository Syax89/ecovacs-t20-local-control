#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Agente MQTT locale che gira SUL robot Deebot T20 Omni (Python 2.7, zero dipendenze, no TLS).
# Si connette al Mosquitto di Home Assistant, pubblica MQTT Discovery + stato, ed esegue i
# comandi in locale. Parita' con l'app ECOVACS: tutte le funzioni passano per lo stesso canale
# iot/p2p/* che usa l'app, iniettato dal mini-broker on-robot (FIFO). Lo stato viene mantenuto
# aggiornato con POLLING ATTIVO (query getXxx -> il broker intercetta le risposte -> telemetry.json).
# Il cloud resta intatto: medusa continua a girare in parallelo. Dispositivo/rete del proprietario.
import socket, struct, json, subprocess, threading, time, sys, os

# ---- configurazione (override via /data/t20_mqtt.conf, una riga key=val) ----
CFG = {
    "host": "192.168.1.10", "port": "1883",
    "user": "CHANGEME", "pass": "CHANGEME",
    "did":  "00000000-0000-0000-0000-000000000000",
    "name": "Deebot T20 Omni", "poll": "10", "sweep": "90", "map_interval": "300",
}
def load_cfg():
    p = "/data/t20_mqtt.conf"
    if os.path.exists(p):
        for ln in open(p):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1); CFG[k.strip()] = v.strip()
load_cfg()
DID = CFG["did"]; BASE = "t20/" + DID[:8]          # prefisso topic di stato/comando
HADISC = "homeassistant"                            # prefisso HA discovery
UID = "t20_" + DID[:8]

LOG_MAX = 512 * 1024   # tetto log su /data (troncato oltre questa soglia)
def log(m):
    try:
        if os.fstat(1).st_size > LOG_MAX: os.ftruncate(1, 0)
    except Exception: pass
    print("[t20] %s %s" % (time.strftime("%H:%M:%S"), m)); sys.stdout.flush()

def daemonize(logpath="/data/t20_mqtt.log"):
    # doppio fork + setsid: stacca il processo (BusyBox non ha setsid/nohup)
    try:
        if os.fork() > 0: os._exit(0)
        os.setsid()
        if os.fork() > 0: os._exit(0)
        f = open(logpath, "a"); di = open(os.devnull, "r")
        os.dup2(di.fileno(), 0); os.dup2(f.fileno(), 1); os.dup2(f.fileno(), 2)
    except Exception:
        pass

# ================= mini-client MQTT 3.1.1 (QoS0) =================
def _rl_encode(n):
    out = b""
    while True:
        b = n % 128; n //= 128
        if n > 0: b |= 0x80
        out += struct.pack("B", b)
        if n == 0: return out
def _str(s):
    s = s.encode("utf-8") if isinstance(s, unicode) else s
    return struct.pack(">H", len(s)) + s

class MQTT(object):
    def __init__(self, host, port, user, pw, cid):
        self.host, self.port, self.user, self.pw, self.cid = host, port, user, pw, cid
        self.sock = None; self.lock = threading.Lock(); self.on_msg = None; self.alive = False
        self.will = None                               # (topic, payload): LWT -> HA marca offline se cadiamo
    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), 10)
        self.sock.settimeout(None)
        payload = _str(self.cid)
        flags = 0x02                                   # clean session
        if self.will:                                  # will flag (0x04) + will retain (0x20), QoS0
            flags |= 0x04 | 0x20
            payload += _str(self.will[0]) + _str(self.will[1])   # dopo il client id, prima di user/pass
        if self.user: flags |= 0x80; payload += _str(self.user)
        if self.pw:   flags |= 0x40; payload += _str(self.pw)
        vh = _str("MQTT") + struct.pack("BBH", 4, flags, 45)   # keepalive 45s
        pkt = struct.pack("B", 0x10) + _rl_encode(len(vh + payload)) + vh + payload
        self.sock.sendall(pkt)
        t, _ = self._read_packet()
        if t >> 4 != 2: raise IOError("no CONNACK (got %d)" % (t >> 4))
        self.alive = True; log("connesso a %s:%d" % (self.host, self.port))
    def _send(self, pkt):
        with self.lock:
            if self.sock: self.sock.sendall(pkt)
    def publish(self, topic, payload, retain=False):
        if isinstance(payload, unicode): payload = payload.encode("utf-8")
        hdr = 0x30 | (0x01 if retain else 0x00)
        body = _str(topic) + payload
        self._send(struct.pack("B", hdr) + _rl_encode(len(body)) + body)
    def subscribe(self, topic):
        body = struct.pack(">H", 1) + _str(topic) + struct.pack("B", 0)
        self._send(struct.pack("B", 0x82) + _rl_encode(len(body)) + body)
    def ping(self): self._send(b"\xc0\x00")
    def _read_n(self, n):
        buf = b""
        while len(buf) < n:
            c = self.sock.recv(n - len(buf))
            if not c: raise IOError("peer closed")
            buf += c
        return buf
    def _read_packet(self):
        b0 = self._read_n(1); t = struct.unpack("B", b0)[0]
        mult = 1; ln = 0
        while True:
            b = struct.unpack("B", self._read_n(1))[0]
            ln += (b & 0x7f) * mult
            if not (b & 0x80): break
            mult *= 128
        return t, (self._read_n(ln) if ln else b"")
    def loop(self):
        while self.alive:
            try:
                t, body = self._read_packet()
            except Exception as e:
                log("rx chiusa: %s" % e); self.alive = False; break
            if t >> 4 == 3:                              # PUBLISH in arrivo
                tl = struct.unpack(">H", body[:2])[0]
                topic = body[2:2 + tl].decode("utf-8", "ignore")
                payload = body[2 + tl:]
                if t & 0x06:                             # QoS>0: salta packet id
                    payload = payload[2:]
                if self.on_msg: self.on_msg(topic, payload)

# ================= interfaccia robot (mdsctl, locale) =================
def mds(elem, obj):
    try:
        out = subprocess.check_output(["timeout", "-t", "5", "/usr/bin/mdsctl", elem, json.dumps(obj)],
                                      stderr=subprocess.STDOUT)
        return out.strip()
    except Exception as e:
        return '{"err":"%s"}' % e

def _sh(cmd):
    try: return subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    except Exception: return ""

# ---- comandi verso il robot: iniettati via FIFO del broker come iot/p2p/* (come fa l'app) ----
FIFO = "/data/mdsbroker/cmd.fifo"
def fifo_write(s):
    try:
        fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
        os.write(fd, (s + "\n").encode("utf-8")); os.close(fd)
        return True
    except Exception as e:
        log("fifo write fallita (broker attivo?): %s -> %s" % (s[:80], e)); return False
def inject(cmd, data):
    return fifo_write(json.dumps({"cmd": cmd, "data": data}))

# ---- mappe valori protocollo deebot <-> etichette (= stesse dell'app/HA) ----
FAN_NAMES   = {1000: "quiet", 0: "standard", 1: "strong", 2: "max"}
FAN_SET     = {"quiet": 1000, "standard": 0, "strong": 1, "max": 2}
WATER_NAMES = {1: "basso", 2: "medio", 3: "alto", 4: "molto alto"}
WATER_SET   = {"basso": 1, "medio": 2, "alto": 3, "molto alto": 4}
WORKMODE_NAMES = {0: "aspira+lava", 1: "solo aspira", 2: "solo lava", 3: "aspira poi lava"}
WORKMODE_SET   = {"aspira+lava": 0, "solo aspira": 1, "solo lava": 2, "aspira poi lava": 3}
LIFE_LABEL  = {"brush": "life_brush", "sideBrush": "life_sideBrush", "heap": "life_heap"}

# ---- codici errore -> messaggio, estratti dall'app ECOVACS HOME 3.17.2 (modulo dt10 = T20) ----
# Fonte: codici da com.eco.dt10...DeviceErrorParams + enum DeviceErr/XianbotDefine.WARN; testi dalla
# funzione s79.f() (codice -> chiave) risolta nel pacchetto lingua assets/lang/app/it.json.
# [*] = l'app non ha un messaggio dedicato per quel codice: descrizione ricavata dal nome della costante.
ERR_NONE = (0, 100)
ERR_TEXT = {
    101: "Batteria scarica. Il robot sta tornando alla stazione di ricarica.",
    102: "DEEBOT non si trova sul pavimento. Rimettilo a terra.",
    103: "Ruota motrice bloccata. Si consiglia di controllare.",
    104: "Polvere eccessiva sul sensore anticaduta. Pulisci.",
    105: "DEEBOT si è incastrato. Sbloccalo.",
    106: "La spazzola laterale è scaduta. Sostituisci.",
    107: "Il filtro è scaduto. Sostituiscilo.",
    108: "Spazzola laterale impigliata. Si consiglia di pulire.",
    109: "Spazzola principale impigliata. Si consiglia di pulire.",
    110: "Vaschetta raccogli-polvere non installata. Si consiglia di controllare.",
    111: "Il paracolpi è bloccato. Controlla.",
    112: "Malfunzionamento di LDS. Controllare.",
    113: "La spazzola principale è scaduta. Sostituirla.",
    114: "Contenitore della polvere pieno. Svuotalo.",                          # [*]
    115: "Anomalia della batteria.",                                           # [*]
    116: "Il paracolpi è coperto di polvere. Pulisci il paracolpi.",
    117: "Anomalia del giroscopio.",                                           # [*]
    118: "Filtro/griglia ostruito. Pulisci.",                                  # [*]
    119: "Anomalia della ventola di aspirazione.",                             # [*]
    120: "Il serbatoio OZMO Pro è bloccato. Controlla.",
    121: "Piastra del mop inceppata.",                                         # [*]
    126: "Installare correttamente il serbatoio OZMO Pro o il profumatore per ambienti.",
    128: "Le piastre della base di lavaggio non sono installate entrambe. Controlla.",
    129: "La piastra della base di lavaggio è impigliata. Controlla.",
    201: "Filtro dell'aria non installato.",                                   # [*]
    202: "Anomalia del sensore a ultrasuoni (rilevamento tappeti).",           # [*]
    203: "Anomalia della ruota piroettante.",                                  # [*]
    204: "La ruota motrice non si trova sul pavimento. Sposta DEEBOT in una nuova posizione per riavviare.",
    209: "Il componente D-ToF ha smesso di funzionare, controllare.",
    301: "Livello acqua insufficiente nel serbatoio acqua pulita. Si consiglia di riempire.",
    302: "Il serbatoio acqua sporca è pieno. Puliscilo.",
    303: "Serbatoio acqua pulita non installato. Si consiglia di controllare.",
    304: "Serbatoio acqua sporca non installato. Si consiglia di controllare.",
    305: "Il lavello di pulizia è intasato. Puliscilo.",
    306: "Filtro del lavello di pulizia non installato. Si consiglia di controllare.",
    307: "Malfunzionamento motore stazione di pulizia automatica. Contatta il servizio post-vendita.",
    308: "Problema di comunicazione. Scollega la stazione di pulizia automatica e ricollegala.",
    309: "Il serbatoio si sta surriscaldando. Estrailo e lascialo raffreddare.",
    310: "Chiudere il coperchio antipolvere.",
    311: "Il sacchetto di raccolta polvere non è installato correttamente. Controlla.",
    312: "Sostituisci il sacchetto di raccolta polvere.",
    313: "Anomalia dell'auto-svuotamento.",                                    # [*]
    314: "Modulo di circolazione acqua (o modulo fittizio) non installato.",
    315: "Modulo antibatterico Ag+ non installato.",
    316: "Il lavello di pulizia è pieno. Controlla.",
    317: "Malfunzionamento riempimento serbatoio acqua pulita. Controlla.",
    318: "Il serbatoio acqua sporca è pieno. Controlla.",                      # testo app solo in EN
    319: "La soluzione detergente è in fase di esaurimento. Aggiungi la soluzione detergente.",
    601: "Spazzola principale: anomalia con AIVI disattivato.",               # [*]
    602: "Spazzola laterale: anomalia con AIVI disattivato.",                 # [*]
}
def _codes(v):
    # normalizza il campo errore (int | "123" | [int,...] | None) in lista di int, senza "nessun errore"
    if v is None: return []
    if not isinstance(v, list): v = [v]
    out = []
    for x in v:
        try: c = int(x)
        except Exception: continue
        if c not in ERR_NONE and c not in out: out.append(c)
    return out
def _err_text(codes):
    return " | ".join([ERR_TEXT.get(c, "Codice %d (non documentato nell'app)" % c) for c in codes])
def _err_items(codes, ign):
    return [{"codice": c, "significato": ERR_TEXT.get(c, "Codice %d (non documentato nell'app)" % c),
             "ignorato": c in ign} for c in codes]
def publish_error_catalog(c):
    # catalogo completo codice -> significato (retained): consultabile da HA o da qualsiasi client MQTT
    cat = dict([(str(k), v) for k, v in sorted(ERR_TEXT.items())])
    c.publish(BASE + "/errors/catalog", json.dumps(cat, ensure_ascii=False, sort_keys=True), retain=True)
# ---- codici evento onEvt -> messaggio (app 3.17.2: assets/t10_evtCode_dock.json + lang/app/it.json) ----
# 114 codici su 128 hanno un testo nell'app; gli altri sono eventi silenziosi.
EVT_TEXT = {
    1001: "Serbatoio OZMO Pro installato. Passaggio alla modalità Aspirazione e Lavaggio.",
    1004: "Modalità pavimenti duri attivata",
    1005: "Modalità pavimenti duri disattivata",
    1007: "Serbatoio OZMO Pro installato. Passaggio alla modalità Aspirazione e Lavaggio.",
    1008: "Serbatoio OZMO Pro rimosso. Passaggio alla modalità Aspirazione.",
    1011: "Attività programmata iniziata.",
    1012: "Batteria in esaurimento. Impossibile eseguire l'attività programmata.",
    1013: "DEEBOT è controllato tramite telecomando. Impossibile iniziare l'attività programmata.",
    1014: "Malfunzionamento DEEBOT. Impossibile iniziare l'attività programmata.",
    1015: "La modalità Non disturbare è attivata. Impossibile iniziare l'attività programmata.",
    1016: "Pulizia personalizzata in corso. Impossibile iniziare l'attività programmata.",
    1017: "DEEBOT sta andando nella posizione impostata. Impossibile iniziare l'attività programmata.",
    1018: "Perlustrazione in corso. Impossibile iniziare l'attività programmata.",
    1019: "Nuova pulizia in corso. Impossibile iniziare l'attività programmata.",
    1020: "Attività programmata iniziata.",
    1021: "Pulizia completata. Ritorno alla stazione di carica.",
    1022: "La pulizia è stata terminata.",
    1023: "DEEBOT si sta caricando",
    1024: "Bassa potenza. Ritornare.",
    1025: "Batteria in esaurimento. Tieni DEEBOT in carica.",
    1026: "Stazione di carica non trovata. Aiuta DEEBOT a ricaricarsi.",
    1027: "Stazione di carica non trovata. Ritornato alla posizione iniziale.",
    1028: "Scarsa carica nella batteria. Ricaricarla.",
    1029: "Batteria troppo scarica. Spegnimento in corso per protezione.",
    1031: "L'area attuale è bloccata. Nuova creazione del percorso.",
    1032: "DEEBOT è stato bloccato e la pulizia è stata terminata.",
    1033: "In Modalità Lavaggio, non usare DEEBOT sui tappeti.",
    1034: "Impossibile raggiungere la posizione designata.  Seleziona nuovamente la posizione.",
    1035: "DEEBOT si è incastrato. Sbloccalo.",
    1036: "Posizionamento in corso...",
    1037: "Attività programmata iniziata. Posizionamento in corso...",
    1039: "Non è presente acqua sufficiente nel serbatoio. Effettua il riempimento.",
    1041: "Il firmware è stato aggiornato alla versione più recente",
    1052: "Lava il panno di pulizia.",
    1053: "DEEBOT si sta caricando.   Rimuovi la base del panno di pulizia.",
    1054: "DEEBOT bloccato, fine della pulizia. Ritorno alla stazione di carica.",
    1055: "Impossibile raggiungere l'area specificata. Ritorno alla stazione di carica.",
    1056: "L'area da pulire è al di fuori della mappa. Riprova.",
    1057: "Raggiunta la posizione designata",
    1058: "Impossibile raggiungere la posizione definita",
    1061: "Individuazione completata. Avvio pulizia.",
    1062: "Nuovo ambiente rilevato. Avvio di un nuovo ciclo di pulizia.",
    1063: "Rilevato nuovo ambiente, avvio di un nuovo ciclo di pulizia.\\nPuoi anche riportarlo alla stazione di carica per ripristinare la mappa.",
    1064: "Ripristino della mappa eseguito.",
    1065: "Nuovo ambiente rilevato. La mappa è stata aggiornata.",
    1066: "Rilevato nuovo ambiente. È stata generata una nuova mappa.",
    1067: "Individuazione completata. Ritorno al dock di ricarica.",
    1068: "Individuazione completata. Ricerca del dock di ricarica.",
    1069: "Mappa aggiornata",
    1070: "Ripristino mappa scaduto. La mappa dalla posizione precedente è stata visualizzata in modo predefinito.",
    1071: "La posizione di DEEBOT è stata aggiornata.",
    1072: "Individuazione scaduta. Porta DEEBOT alla stazione di carica per ripristinare la mappa.",
    1073: "Area selezionata non trovata. La pulizia è stata terminata.",
    1074: "Rilevato nuovo ambiente. Eliminare la mappa attuale e ricrearla?",
    1075: "Livello acqua insufficiente nel serbatoio. Impossibile iniziare l'attività programmata.",
    1076: "Nuova pulizia completata. Ritorno alla stazione di carica.",
    1077: "Posiziona con successo, avvia la missione",
    1078: "Posizionamento scaduto, l'attività corrente è terminata. Puoi riportare DEEBOT alla stazione di carica per ripristinare la mappa.",
    1079: "Svuotamento automatico cassetta di raccolta. Impossibile iniziare l'attività programmata.",
    1080: "Nessuna posizione di perlustrazione impostata. Impossibile iniziare l'attività programmata.",
    1081: "Unione non riuscita. Riprova.",
    1082: "Divisione non riuscita, riprova.",
    1083: "L'area da dividere è troppo piccola. Riprova.",
    1084: "Aree non ancora divise completamente.",
    1085: "Perlustrazione completata.",
    1086: "Impossibile raggiungere l'area specifica di perlustrazione. Ritorno alla stazione di carica.",
    1087: "Pulizia in corso. Impossibile iniziare la perlustrazione programmata.",
    1088: "Posizionamento DEEBOT scaduto",
    1089: "Nessuna posizione di perlustrazione impostata. Impossibile iniziare l'attività programmata.",
    1092: "Impossibile trovare l'area che deve essere pulita. Ritorno alla stazione di carica.",
    1093: "Impossibile recuperare la mappa",
    1094: "Dopo il lavaggio, pulisci subito il serbatoio acqua sporca per evitare la formazione di cattivi odori.",
    1096: "Area specifica per l'erogazione deodorante non trovata. Ritorno alla stazione di carica.",
    1097: "Erogazione deodorante terminata.",
    1098: "Erogazione deodorante terminata. Ritorno alla carica.",
    1099: "Svuotamento automatico completato",
    1100: "Malfunzionamento comunicazione. Sposta DEEBOT nella stazione e riprova.",
    1101: "Asciuga il fondo della stazione di pulizia automatica e il pavimento.",
    1102: "Il modulo di sterilizzazione Ag+ è in vigore.",
    1103: "Pulizia base di lavaggio in corso. Attendi.",
    1104: "DEEBOT è controllato con telecomando, attendi...",
    1105: "Interruttore di accensione non attivato.",
    1106: "Il robot è scollegato dalla stazione. Controlla.",
    1107: "La stazione sta eseguendo la pulizia automatica. Attendi.",
    1108: "Livello acqua insufficiente nel serbatoio acqua pulita. Si consiglia di riempire.",
    1109: "Il serbatoio acqua sporca è pieno. Puliscilo.",
    1110: "Serbatoio acqua pulita non installato. Si consiglia di controllare.",
    1111: "Serbatoio acqua sporca non installato. Si consiglia di controllare.",
    1112: "Il sacchetto di raccolta polvere non è installato correttamente. Controlla.",
    1113: "Sostituisci il sacchetto di raccolta polvere.",
    1116: "Pulizia in corso. Impossibile iniziare Trova il mio animale domestico programmata.",
    1117: "Perlustrazione in corso. Impossibile iniziare Trova il mio animale domestico programmata.",
    1118: "Ricerca dell'animale domestico. Impossibile iniziare l'attività programmata.",
    1119: "Trova il mio animale domestico in corso. Impossibile iniziare la perlustrazione programmata.",
    1120: "Lavaggio delle basi di lavaggio. Impossibile iniziare l'attività programmata.",
    1121: "Chiamata vocale in corso. Impossibile iniziare l'attività programmata.",
    1123: "Pulizia in pausa. Ritorno alla stazione di carica.",
    1124: "Pulizia area mobili in corso. Impossibile iniziare l'attività programmata.",
    1126: "Svuotamento automatico della vaschetta raccogli-polvere, attendi...",
    1127: "Mappatura completata",
    1128: "Le piastre della base di lavaggio non sono installate entrambe. Controlla.",
    1129: "Accendi DEEBOT e riprova.",
    1130: "Mappatura rapida in corso. Impossibile iniziare l'attività programmata.",
    1131: "Non sono state rilevate operazioni per un lungo periodo di tempo. L'attività è terminata.",
    1135: "La stazione sta eseguendo il riempimento d'acqua. Riprova più tardi.",
    1136: "La stazione sta eseguendo lo scaricamento d'acqua. Riprova più tardi.",
    1137: "Localizzazione riuscita. Riprendi attività.",
    1140: "Passato a modalità Lavaggio",
    1141: "Impostata correttamente. Attivata quando le basi di lavaggio sono installate.",
    1142: "Usare questa funzione con la base del panno per il lavaggio installata.",
    1143: "Usare questa funzione dopo che il robot è tornato alla stazione.",
    1145: "La funzione \"Chiamata per pulizia\" è in corso. Impossibile iniziare l'attività programmata.",
    1146: "La funzione \"Vai a pulire là\" è in corso. Impossibile iniziare l'attività programmata.",
    1150: "Modalità domestica in corso. Impossibile iniziare l'attività programmata.",
}

def evt_text(code):
    try: code = int(code)
    except Exception: return None
    return EVT_TEXT.get(code, "Evento %d (senza messaggio nell'app)" % code)

# ---- oggetti riconosciuti dall'AI (getAIMap.pointValue[].type) — tipi da RecognizationParams dell'app ----
AI_TYPES = {0: "sconosciuto", 1: "cestino", 2: "base di ricarica", 3: "tappeto", 4: "moquette",
            5: "scarpa", 6: "cavo"}
AI_STATUS = {0: "da pulire", 1: "in pulizia", 2: "pulito"}
DAYS = ["dom", "lun", "mar", "mer", "gio", "ven", "sab"]   # repeat "0111100": 1 carattere per giorno, da domenica
def sched_items(lst):
    out = []
    for it in (lst if isinstance(lst, list) else []):
        rep = str(it.get("repeat") or "")
        days = [DAYS[i] for i, ch in enumerate(rep[:7]) if ch == "1"]
        try: hm = "%02d:%02d" % (int(it.get("hour")), int(it.get("minute")))
        except Exception: hm = "?"
        out.append({"id": it.get("sid"), "ora": hm, "giorni": ",".join(days) or "una volta",
                    "attiva": bool(it.get("enable")), "repeat": rep, "mappa": it.get("mid")})
    return out
def _hm(s):
    # "22:0" -> "22:00"
    try:
        h, m = str(s).split(":")[:2]; return "%02d:%02d" % (int(h), int(m))
    except Exception: return None

def _ignored():
    # codici da NON considerare "problema" (es. avvisi permanenti noti): ignore_errors=314,319 in t20_mqtt.conf
    return [int(x) for x in CFG.get("ignore_errors", "").replace(";", ",").split(",") if x.strip().isdigit()]

# ---- polling attivo: GET iniettati (medusa risponde -> broker intercetta -> telemetry.json) ----
GETS = [
    ("getChargeState", {}), ("getCleanInfo", {}), ("getError", {}),
    ("getSpeed", {}), ("getWaterInfo", {}), ("getWorkMode", {}), ("getVolume", {}),
    ("getBreakPoint", {}), ("getCleanCount", {}), ("getCleanPreference", {}),
    ("getAdvancedMode", {}), ("getCarpertPressure", {}), ("getWashInfo", {}),
    ("getTrueDetect", {}), ("getMultiMapState", {}),
    ("getStats", {}), ("getTotalStats", {}),
    ("getLifeSpan", ["brush", "heap", "sideBrush"]),
    ("getCachedMapInfo", {}),
    ("getAutoEmpty", {}), ("getStationState", {}),
    ("getMapSet", {"type": "ar"}),
    ("getBlock", {}), ("getChildLock", {}), ("getBorderSpin", {}), ("getCarpetInfo", {}),
    ("getEfficiency", {}), ("getSweepMode", {}), ("getCustomAreaMode", {}), ("getSched_V2", {"type": 1}),
]
def sweep_gets():
    for cmd, data in GETS:
        inject(cmd, data); time.sleep(0.35)

# ---- raccolta mappa: inietta i getter, legge le risposte dalla telemetria, pubblica grezzo su MQTT ----
TELE = "/data/mdsbroker/telemetry.json"
def _tele():
    try: return json.load(open(TELE))
    except Exception: return {}
EMPTY_CRC = "1295764014"
def _fetch_trace(max_points=6000):
    # percorso della pulizia corrente: frammenti 7z/base64 da 400 punti (decodificati dal renderer)
    chunks = []; start = 0; tid = None; total = 0
    while start < max_points:
        inject("getMapTrace", {"pointCount": 400, "traceStart": start})
        tr = {}
        for _ in range(12):                                # attende la risposta di QUESTO blocco (max ~3,6 s)
            time.sleep(0.3)
            tr = _tele().get("getMapTrace") or {}
            if tr.get("traceStart") == start and (start == 0 or tr.get("traceValue")): break
        if tr.get("traceStart") != start: break           # nessuna risposta: tengo i blocchi gia' presi
        tid = tr.get("tid"); total = int(tr.get("totalCount") or 0)
        if tr.get("traceValue"): chunks.append(tr.get("traceValue"))
        start += 400
        if start >= total: break
    return {"tid": tid, "total": total, "chunks": chunks}

def collect_and_publish_map(c, full=True):
    # full=True: rilegge anche i frammenti mappa; full=False (durante la pulizia): solo posizione,
    # percorso e oggetti AI, riusando i frammenti gia' in cache -> aggiornamento rapido "dal vivo".
    try:
        t = _tele(); mid = t.get("_map_mid")
        mj = t.get("getMajorMap") or {}
        vals = str(mj.get("value", "")).split(",")
        if full or not mid or len(vals) < 2:
            inject("getMajorMap", {}); time.sleep(1.5)
            mj = _tele().get("getMajorMap") or {}
            vals = str(mj.get("value", "")).split(",")
            mid = mj.get("mid")
        if not mid or len(vals) < 2:
            info = (_tele().get("getCachedMapInfo") or {}).get("info") or []
            for it in info:
                if it.get("using") == 1: mid = it.get("mid"); break
        if not mid: return
        want = [i for i, crc in enumerate(vals) if crc and crc != EMPTY_CRC]
        if full:                                       # richiede i frammenti mancanti, fino a 3 passate
            for _ in range(3):
                mp = _tele().get("minorPieces") or {}
                missing = [i for i in want if str(i) not in mp]
                if not missing: break
                for i in missing:
                    inject("getMinorMap", {"mid": mid, "pieceIndex": i, "type": "ol"}); time.sleep(0.3)
                time.sleep(2)
        mp = _tele().get("minorPieces") or {}
        pieces = dict([(str(i), mp[str(i)]) for i in want if str(i) in mp])
        inject("getPos", ["chargePos", "deebotPos"]); time.sleep(0.8)
        inject("getAIMap", {"mid": str(mid), "pointStart": 0, "pointCount": 200}); time.sleep(1.0)
        trace = _fetch_trace()
        t = _tele()
        mset = t.get("getMapSet") or {}
        ai = (t.get("getAIMap") or {}).get("pointValue") or []
        doc = {"mid": mid, "cols": 8, "rows": 8, "piece": 100, "cell_mm": 50,
               "pieces": pieces, "pos": t.get("getPos") or {},
               "rooms": [{"mssid": x.get("mssid"), "name": x.get("name")} for x in (mset.get("subsets") or [])],
               "trace": trace,
               "ai": [{"type": o.get("type"), "name": AI_TYPES.get(o.get("type"), "tipo %s" % o.get("type")),
                       "x": o.get("x"), "y": o.get("y"), "status": o.get("status")} for o in ai],
               "ts": int(time.time())}
        c.publish(BASE + "/map/raw", json.dumps(doc), retain=True)
        log("mappa pubblicata (%s: %d frammenti, percorso %d punti, %d oggetti AI)" %
            ("completa" if full else "rapida", len(pieces), trace.get("total", 0), len(ai)))
    except Exception as e:
        log("collect_map err: %s" % e)

def _cleaning():
    cs = (_tele().get("onCleanInfo_V2") or _tele().get("getCleanInfo_V2") or {}).get("state")
    return cs in ("clean", "goCharging")

def map_loop(c):
    time.sleep(25)                          # lascia finire il primo sweep
    iv = int(CFG["map_interval"]); live = int(CFG.get("map_live_interval", "30"))
    last_full = 0
    while c.alive:
        now = time.time()
        if now - last_full >= iv:
            collect_and_publish_map(c, full=True); last_full = time.time()
        elif _cleaning():
            collect_and_publish_map(c, full=False)
        for _ in range(live if _cleaning() else 10):
            if not c.alive: return
            time.sleep(1)

def get_state():
    # batteria/carica via rtctl; rete/uptime/fw da fonti locali (no cloud)
    st = {}
    try:
        r = json.loads(mds("rosnode", {"todo": "rtctl", "cmd": "getBatteryInfo"}))
        st["battery"] = r.get("battery"); st["charging"] = (r.get("inCharging") == 1)
    except Exception as e:
        log("getBatteryInfo parse: %s" % e); st["battery"] = None; st["charging"] = None
    try:
        for ln in open("/proc/net/wireless"):
            if ln.strip().startswith("wlan0:"):
                st["rssi"] = int(float(ln.split()[3].rstrip(".")))
    except Exception: pass
    try:
        for ln in _sh(["/sbin/iw", "dev", "wlan0", "link"]).split("\n"):
            ln = ln.strip()
            if ln.startswith("SSID:"): st["ssid"] = ln[5:].strip()
    except Exception: pass
    try:
        import re as _re
        m = _re.search(r"inet (\d+\.\d+\.\d+\.\d+)", _sh(["/sbin/ip","-4","addr","show","wlan0"]))
        if m: st["ip"] = m.group(1)
    except Exception: pass
    try: st["uptime"] = int(float(open("/proc/uptime").read().split()[0]))
    except Exception: pass
    try:
        import re as _re2
        m = _re2.search(r'"fw_ver"\s*:\s*"([^"]+)"', open("/etc/fw.manifest").read())
        if m: st["fw"] = m.group(1)
    except Exception: pass
    # --- parametri dalla telemetria (popolata dal polling attivo + sniff passivo del broker) ---
    try: t = json.load(open("/data/mdsbroker/telemetry.json"))
    except Exception: t = {}
    def g(*names):
        for n in names:
            v = t.get(n)
            if isinstance(v, dict): return v
        return {}
    cinfo = g("onCleanInfo_V2", "onCleanInfo", "getCleanInfo_V2", "getCleanInfo")
    stats = g("onStats", "getStats")
    tot   = g("getTotalStats", "onTotalStats")
    water = g("onWaterInfo", "getWaterInfo")
    speed = g("onSpeed", "getSpeed")
    wmode = g("onWorkMode", "getWorkMode")
    err   = g("onError", "getError")
    sta   = g("onStationState", "getStationState")
    vol   = g("onVolume", "getVolume")
    brk   = g("onBreakPoint", "getBreakPoint")
    cnt   = g("onCleanCount", "getCleanCount")
    pref  = g("onCleanPreference", "getCleanPreference")
    adv   = g("onAdvancedMode", "getAdvancedMode")
    aemp  = g("onAutoEmpty", "getAutoEmpty")
    carp  = g("onCarpertPressure", "getCarpertPressure")
    wash  = g("onWashInfo", "getWashInfo")
    tdet  = g("onTrueDetect", "getTrueDetect")
    mmap  = g("onMultiMapState", "getMultiMapState")
    life  = t.get("getLifeSpan", t.get("onLifeSpan"))
    maps  = g("getCachedMapInfo", "onCachedMapInfo")
    mset  = g("getMapSet", "onMapSet")

    src = stats or cinfo
    if src.get("area") is not None: st["clean_area"] = src.get("area")
    if src.get("time") is not None:
        try: st["clean_time"] = int(src.get("time")) // 60
        except Exception: pass
    if src.get("type") is not None: st["clean_type"] = src.get("type")
    # riconoscimento AI nella pulizia corrente/ultima (onStats/getStats): ostacoli evitati + tipi riconosciuti
    if stats.get("avoidCount") is not None: st["avoid_count"] = stats.get("avoidCount")
    if isinstance(stats.get("aitypes"), list):
        names = [AI_TYPES.get(x, "tipo %s" % x) for x in stats.get("aitypes")]
        st["ai_types"] = ", ".join(names) if names else "nessuno"
    if stats.get("aiopen") is not None: st["ai_active"] = "ON" if stats.get("aiopen") else "OFF"
    cs = cinfo.get("state"); ms = (cinfo.get("cleanState") or {}).get("motionState")
    if cs == "clean":        st["activity"] = "paused" if ms == "pause" else "cleaning"
    elif cs == "goCharging": st["activity"] = "returning"
    elif cs:                 st["activity"] = "idle"
    if tot.get("area")  is not None: st["total_area"]  = tot.get("area")
    if tot.get("count") is not None: st["total_count"] = tot.get("count")
    if tot.get("time")  is not None:
        try: st["total_time"] = int(tot.get("time")) // 3600
        except Exception: pass
    if water.get("amount") is not None: st["water"] = WATER_NAMES.get(water.get("amount"), water.get("amount"))
    if water.get("enable") is not None: st["mop"] = "montato" if water.get("enable") else "assente"
    if speed.get("speed")  is not None: st["fan"]   = FAN_NAMES.get(speed.get("speed"), speed.get("speed"))
    if wmode.get("mode")   is not None: st["work_mode"] = WORKMODE_NAMES.get(wmode.get("mode"), wmode.get("mode"))
    if sta.get("state")    is not None: st["station"] = sta.get("state")
    if vol.get("volume")   is not None: st["volume"] = vol.get("volume")
    if brk.get("enable")   is not None: st["continuous"] = "ON" if brk.get("enable") else "OFF"
    if cnt.get("count")    is not None: st["clean_count"] = cnt.get("count")
    if pref.get("enable")  is not None: st["clean_pref"] = "ON" if pref.get("enable") else "OFF"
    if adv.get("enable")   is not None: st["advanced"] = "ON" if adv.get("enable") else "OFF"
    if aemp.get("enable")  is not None: st["auto_empty"] = "ON" if aemp.get("enable") else "OFF"
    if carp.get("enable")  is not None: st["carpet"] = "ON" if carp.get("enable") else "OFF"
    if tdet.get("enable")  is not None: st["true_detect"] = "ON" if tdet.get("enable") else "OFF"
    if mmap.get("enable")  is not None: st["multimap"] = "ON" if mmap.get("enable") else "OFF"
    if wash.get("interval") is not None: st["wash_freq"] = wash.get("interval")
    # errori robot (getError/onError: {"code":[...]}) e stazione (StationState: content.error [...])
    try:
        rc = _codes(err.get("code")) if err else None
        sc = _codes((sta.get("content") or {}).get("error")) if sta else None
        ign = _ignored()                                # codici innocui: non mostrati come errore
        if rc is not None:
            re_ = [c for c in rc if c not in ign]
            st["error"] = _err_text(re_) if re_ else "nessuno"
            st["error_code"] = ",".join([str(c) for c in re_]) if re_ else "0"
        if sc is not None:
            se_ = [c for c in sc if c not in ign]
            st["station_error"] = _err_text(se_) if se_ else "nessuno"
            st["station_error_code"] = ",".join([str(c) for c in se_]) if se_ else "0"
        if rc is not None or sc is not None:
            active = [c for c in (rc or []) + (sc or []) if c not in ign]
            st["problem"] = "ON" if active else "OFF"
            st["_rc"] = rc; st["_sc"] = sc              # liste grezze (con ignorati) per errors/active
    except Exception as e:
        log("decodifica errori: %s" % e)
    # --- impostazioni extra ---
    blk = g("onBlock", "getBlock"); chl = g("onChildLock", "getChildLock"); bsp = g("onBorderSpin", "getBorderSpin")
    if blk.get("enable") is not None:
        st["dnd"] = "ON" if blk.get("enable") else "OFF"
        st["dnd_start"] = _hm(blk.get("start")); st["dnd_end"] = _hm(blk.get("end"))
    if chl.get("on") is not None:     st["child_lock"] = "ON" if chl.get("on") else "OFF"
    if bsp.get("enable") is not None: st["border_spin"] = "ON" if bsp.get("enable") else "OFF"
    for key, names, fld in (("efficiency", ("onEfficiency", "getEfficiency"), "efficiency"),
                            ("sweep_mode", ("onSweepMode", "getSweepMode"), "type"),
                            ("carpet_mode", ("onCarpetInfo", "getCarpetInfo"), "mode"),
                            ("custom_area_mode", ("onCustomAreaMode", "getCustomAreaMode"), "sweepMode")):
        v = g(*names).get(fld)
        if v is not None: st[key] = v
    # --- programmazioni salvate sul robot ---
    sch = t.get("getSched_V2", t.get("onSched_V2"))
    if isinstance(sch, list):
        items = sched_items(sch)
        st["schedules"] = len([x for x in items if x["attiva"]]); st["_sched"] = items
    # --- ultimo evento (onEvt) ---
    ev = g("onEvt")
    if ev.get("code") is not None:
        st["event_code"] = str(ev.get("code")); st["event"] = evt_text(ev.get("code"))
    # --- oggetti riconosciuti dall'AI (getAIMap) ---
    am = g("getAIMap", "onAIMap")
    if "pointValue" in am:
        objs = []
        for o in (am.get("pointValue") or []):
            ty = o.get("type")
            objs.append({"tipo": ty, "nome": AI_TYPES.get(ty, "tipo %s" % ty), "x": o.get("x"), "y": o.get("y"),
                         "stato": AI_STATUS.get(o.get("status"), o.get("status")), "id": o.get("pid")})
        st["ai_objects"] = len(objs); st["_ai"] = objs
    try:
        for it in (life if isinstance(life, list) else []):
            ty = it.get("type"); tot_v = it.get("total")
            if ty in LIFE_LABEL and tot_v: st[LIFE_LABEL[ty]] = int(100 * it.get("left", 0) / tot_v)
    except Exception: pass
    try:
        info = maps.get("info") or []
        # mappa attiva: using==1; fallback built==1; altrimenti primo slot con nome
        pick = None
        for it in info:
            if it.get("using") == 1: pick = it; break
        if pick is None:
            for it in info:
                if it.get("built") == 1 and it.get("name"): pick = it; break
        if pick is None:
            for it in info:
                if it.get("name"): pick = it; break
        if pick and pick.get("name"): st["map_name"] = pick.get("name")
    except Exception: pass
    # elenco stanze (mssid[:nome]) dalla mappa attiva — per pulizia per-stanza (tutto locale)
    try:
        subs = mset.get("subsets") or []
        names = []
        for s in subs:
            mssid = s.get("mssid")
            if mssid is None: continue
            nm = s.get("name") or ""
            names.append(("%s:%s" % (mssid, nm)) if nm else str(mssid))
        if names: st["rooms"] = ", ".join(names)
    except Exception: pass
    return st

# ================= comandi (dalle entita' HA) =================
def do_room(p):
    # p = "2" o "2,3" (mssid stanze) -> pulizia mirata spotArea (count 1). Tutto locale.
    ids = ",".join([x.strip() for x in p.replace(";", ",").split(",") if x.strip() != ""])
    if ids:
        inject("clean", {"act": "start", "type": "spotArea", "content": ids, "count": 1})

def do_vacuum(p):
    if   p == "start":          inject("clean",  {"act": "start", "type": "auto"})
    elif p == "stop":           inject("clean",  {"act": "stop"})
    elif p == "pause":          inject("clean",  {"act": "pause"})
    elif p == "resume":         inject("clean",  {"act": "resume"})
    elif p == "return_to_base": inject("charge", {"act": "go"})
    elif p == "locate":         inject("playSound", {"count": 1, "sid": 30})

GET_AFTER_SET = {"child_lock": "getChildLock", "border_spin": "getBorderSpin", "dnd": "getBlock",
                 "dnd_start": "getBlock", "dnd_end": "getBlock"}   # rilettura dopo set: HA si aggiorna subito
def do_set(key, p):
    p = p.strip()
    if   key == "fan"   and p in FAN_SET:      inject("setSpeed", {"speed": FAN_SET[p]})
    elif key == "water" and p in WATER_SET:    inject("setWaterInfo", {"amount": WATER_SET[p]})
    elif key == "mode"  and p in WORKMODE_SET: inject("setWorkMode", {"mode": WORKMODE_SET[p]})
    elif key == "volume":    inject("setVolume", {"volume": int(float(p))})
    elif key == "count":     inject("setCleanCount", {"count": int(float(p))})
    elif key == "washfreq":  inject("setWashInfo", {"interval": int(float(p))})
    elif key == "continuous":  inject("setBreakPoint", {"enable": 1 if p in ("1","ON","on","true") else 0})
    elif key == "advanced":    inject("setAdvancedMode", {"enable": 1 if p in ("1","ON","on","true") else 0})
    elif key == "carpet":      inject("setCarpertPressure", {"enable": 1 if p in ("1","ON","on","true") else 0})
    elif key == "true_detect": inject("setTrueDetect", {"enable": 1 if p in ("1","ON","on","true") else 0})
    elif key == "clean_pref":  inject("setCleanPreference", {"enable": 1 if p in ("1","ON","on","true") else 0})
    elif key == "multimap":    inject("setMultiMapState", {"enable": 1 if p in ("1","ON","on","true") else 0})
    elif key == "auto_empty":  inject("setAutoEmpty", {"enable": 1 if p in ("1","ON","on","true") else 0})
    elif key == "child_lock":  inject("setChildLock", {"on": 1 if p in ("1","ON","on","true") else 0})
    elif key == "border_spin":
        cur = (_tele().get("getBorderSpin") or {}).get("type", 0)     # conserva il tipo attuale
        inject("setBorderSpin", {"enable": 1 if p in ("1","ON","on","true") else 0, "type": cur})
    elif key in ("dnd", "dnd_start", "dnd_end"):
        b = dict(_tele().get("getBlock") or {"enable": 0, "start": "22:0", "end": "8:0"})
        if key == "dnd": b["enable"] = 1 if p in ("1","ON","on","true") else 0
        else:
            hm = _hm(p)
            if not hm: log("orario non valido: %s" % p); return
            h, m = hm.split(":"); b["start" if key == "dnd_start" else "end"] = "%d:%d" % (int(h), int(m))
        inject("setBlock", {"enable": int(b.get("enable") or 0), "start": b.get("start"), "end": b.get("end")})
    else: return
    threading.Timer(1.5, lambda: inject(GET_AFTER_SET.get(key, ""), {}) if GET_AFTER_SET.get(key) else None).start()

def do_btn(key):
    if   key == "reset_brush":     inject("resetLifeSpan", {"type": "brush"})
    elif key == "reset_filter":    inject("resetLifeSpan", {"type": "heap"})
    elif key == "reset_sidebrush": inject("resetLifeSpan", {"type": "sideBrush"})
    elif key == "relocate":        inject("setRelocationState", {"mode": "manu"})
    elif key == "locate":          inject("playSound", {"count": 1, "sid": 30})
    # --- stazione (payload estratti dall'app ECOVACS HOME 3.17.2) ---
    elif key == "station_empty":    inject("autoEmpty", {"act": "start"})                              # svuota polvere
    elif key == "station_wash":     inject("clean_V2", {"act": "start", "content": {"type": "washing"}})  # lava mop
    elif key == "station_dry":      inject("stationAction", {"act": 1, "type": 2})                     # avvia asciugatura
    elif key == "station_dry_stop": inject("stationAction", {"act": 4, "type": 2})                     # ferma asciugatura
    elif key == "station_selfclean":inject("stationAction", {"act": 1, "type": 1})                     # auto-pulizia stazione

# ================= HA MQTT Discovery =================
def _dev():
    return {"identifiers": [UID], "name": CFG["name"],
            "manufacturer": "Ecovacs", "model": "Deebot T20 Omni (zj2231)"}

def publish_discovery(c):
    dev = _dev(); av = BASE + "/avail"; stt = BASE + "/state"
    def pub(comp, sid, cfg):
        cfg["availability_topic"] = av; cfg["device"] = dev
        c.publish(HADISC + "/" + comp + "/" + UID + "_" + sid + "/config", json.dumps(cfg), retain=True)

    # --- vacuum ---
    c.publish(HADISC + "/vacuum/" + UID + "/config", json.dumps({
        "name": None, "unique_id": UID + "_vac", "schema": "state",
        "supported_features": ["start","stop","pause","return_home","locate","battery","status","fan_speed"],
        "state_topic": stt, "command_topic": BASE + "/cmd",
        "set_fan_speed_topic": BASE + "/set/fan", "fan_speed_list": ["quiet","standard","strong","max"],
        "availability_topic": av, "device": dev}), retain=True)
    pub("sensor", "bat", {"name": "Batteria", "unique_id": UID + "_bat",
        "state_topic": stt, "value_template": "{{ value_json.battery_level }}",
        "device_class": "battery", "unit_of_measurement": "%"})

    # --- select (fan / acqua / modalità) ---
    selects = [
        ("fan",   "Aspirazione",  ["quiet","standard","strong","max"], "fan", "mdi:fan"),
        ("water", "Flusso acqua", ["basso","medio","alto","molto alto"], "water", "mdi:water"),
        ("mode",  "Modalità",     ["aspira+lava","solo aspira","solo lava","aspira poi lava"], "work_mode", "mdi:cog"),
    ]
    for sid, nm, opts, vk, icon in selects:
        pub("select", sid, {"name": nm, "unique_id": UID + "_sel_" + sid,
            "command_topic": BASE + "/set/" + sid, "options": opts,
            "state_topic": stt, "value_template": "{{ value_json." + vk + " }}", "icon": icon})

    # --- number (volume / passaggi / frequenza lavaggio mop) ---
    numbers = [
        ("volume",   "Volume",             0, 10, 1, "volume",      "mdi:volume-high", False),
        ("count",    "Passaggi pulizia",   1,  2, 1, "clean_count", "mdi:counter",     False),
        ("washfreq", "Frequenza lavaggio mop", 0, 60, 1, "wash_freq", "mdi:washing-machine", False),
    ]
    for sid, nm, mn, mx, step, vk, icon, diag in numbers:
        cfg = {"name": nm, "unique_id": UID + "_num_" + sid,
               "command_topic": BASE + "/set/" + sid, "min": mn, "max": mx, "step": step,
               "state_topic": stt, "value_template": "{{ value_json." + vk + " }}", "icon": icon}
        if diag: cfg["entity_category"] = "config"
        pub("number", sid, cfg)

    # --- switch (impostazioni on/off) ---
    switches = [
        ("continuous",  "Pulizia continua",  "continuous",  "mdi:restart"),
        ("advanced",    "Modalità avanzata", "advanced",    "mdi:tune"),
        ("carpet",      "Boost tappeti",     "carpet",      "mdi:rug"),
        ("true_detect", "TrueDetect",        "true_detect", "mdi:radar"),
        ("clean_pref",  "Preferenza pulizia", "clean_pref", "mdi:star-cog"),
        ("multimap",    "Mappe multiple",    "multimap",    "mdi:floor-plan"),
        ("auto_empty",  "Auto-svuotamento",  "auto_empty",  "mdi:delete-empty"),
        ("dnd",         "Non disturbare",    "dnd",         "mdi:sleep"),
        ("child_lock",  "Blocco bambini",    "child_lock",  "mdi:lock"),
        ("border_spin", "Pulizia bordi",     "border_spin", "mdi:border-outside"),
    ]
    for sid, nm, vk, icon in switches:
        pub("switch", sid, {"name": nm, "unique_id": UID + "_sw_" + sid,
            "command_topic": BASE + "/set/" + sid, "payload_on": "1", "payload_off": "0",
            "state_topic": stt, "value_template": "{{ value_json." + vk + " }}",
            "state_on": "ON", "state_off": "OFF", "icon": icon, "entity_category": "config"})

    # --- button (azioni istantanee) ---
    buttons = [
        ("locate",          "Trova robot",            "mdi:map-marker-radius", False),
        ("relocate",        "Rilocalizza",            "mdi:crosshairs-gps",    False),
        ("station_empty",   "Svuota polvere",         "mdi:delete-sweep",      False),
        ("station_wash",    "Lava mop",               "mdi:water-sync",        False),
        ("station_dry",     "Asciuga mop",            "mdi:hair-dryer",        False),
        ("station_dry_stop","Ferma asciugatura",      "mdi:stop-circle",       False),
        ("station_selfclean","Auto-pulizia stazione", "mdi:sync",              False),
        ("reset_brush",     "Reset spazzola princ.",  "mdi:broom",             True),
        ("reset_sidebrush", "Reset spazzola lat.",    "mdi:broom",             True),
        ("reset_filter",    "Reset filtro",           "mdi:air-filter",        True),
    ]
    for sid, nm, icon, cfgcat in buttons:
        cfg = {"name": nm, "unique_id": UID + "_btn_" + sid,
               "command_topic": BASE + "/btn/" + sid, "payload_press": "press", "icon": icon}
        if cfgcat: cfg["entity_category"] = "config"
        pub("button", sid, cfg)

    # --- sensori (diagnostica + operativi) ---
    extra = [
        ("rssi",        "WiFi RSSI",        "signal_strength", "dBm",   None, True),
        ("ssid",        "WiFi SSID",        None, None, "mdi:wifi", True),
        ("ip",          "IP",               None, None, "mdi:ip-network", True),
        ("uptime",      "Uptime",           "duration", "s", None, True),
        ("fw",          "Firmware",         None, None, "mdi:chip", True),
        ("activity",    "Attività",       None, None, "mdi:robot-vacuum", False),
        ("clean_area",  "Area pulita",      None, "m²", "mdi:ruler-square", False),
        ("clean_time",  "Durata pulizia",   "duration", "min", None, False),
        ("clean_type",  "Tipo pulizia",     None, None, "mdi:format-list-bulleted", False),
        ("total_area",  "Area totale",      None, "m²", "mdi:sigma", False),
        ("total_time",  "Tempo totale",     "duration", "h", None, False),
        ("total_count", "Pulizie totali",   None, None, "mdi:counter", False),
        ("mop",         "Mop",              None, None, "mdi:water-outline", False),
        ("station",     "Stato stazione",   None, None, "mdi:home-import-outline", False),
        ("error",       "Errore",           None, None, "mdi:alert-circle", False),
        ("station_error", "Errore stazione", None, None, "mdi:home-alert", False),
        ("life_brush",  "Spazzola principale", None, "%", "mdi:broom", True),
        ("life_sideBrush", "Spazzola laterale", None, "%", "mdi:broom", True),
        ("life_heap",   "Filtro",           None, "%", "mdi:air-filter", True),
        ("map_name",    "Mappa",            None, None, "mdi:map", True),
        ("rooms",       "Stanze (mssid)",   None, None, "mdi:floor-plan", True),
        ("event",       "Ultimo evento",    None, None, "mdi:message-alert-outline", False),
        ("ai_objects",  "Oggetti rilevati", None, None, "mdi:shoe-sneaker", False),
        ("avoid_count", "Ostacoli evitati (pulizia)", None, None, "mdi:debug-step-over", False),
        ("ai_types",    "Oggetti riconosciuti (pulizia)", None, None, "mdi:eye-outline", False),
        ("ai_active",   "Riconoscimento AI attivo", None, None, "mdi:robot-outline", True),
        ("schedules",   "Programmazioni attive", None, None, "mdi:calendar-clock", True),
        ("efficiency",  "Efficienza (valore grezzo)", None, None, "mdi:leaf", True),
        ("sweep_mode",  "Modalità passaggio (valore grezzo)", None, None, "mdi:broom", True),
        ("carpet_mode", "Modalità tappeti (valore grezzo)", None, None, "mdi:rug", True),
        ("custom_area_mode", "Modalità per stanza (valore grezzo)", None, None, "mdi:floor-plan", True),
    ]
    for sid, sname, dclass, unit, icon, diag in extra:
        cfg = {"name": sname, "unique_id": UID + "_" + sid,
               "state_topic": stt, "value_template": "{{ value_json." + sid + " }}"}
        if diag:   cfg["entity_category"] = "diagnostic"
        if dclass: cfg["device_class"] = dclass
        if unit:   cfg["unit_of_measurement"] = unit
        if icon:   cfg["icon"] = icon
        if sid in ("error", "station_error"):          # attributi: codici + elenco {codice, significato}
            grp = "robot" if sid == "error" else "stazione"
            cfg["json_attributes_topic"] = BASE + "/errors/active"
            cfg["json_attributes_template"] = (
                '{{ {"codice": (value_json.' + grp + ' | rejectattr("ignorato") | map(attribute="codice") | join(",")) or "0", '
                '"errori": value_json.' + grp + ' | rejectattr("ignorato") | list, '
                '"ignorati": value_json.' + grp + ' | selectattr("ignorato") | map(attribute="codice") | list} | tojson }}')
        if sid == "event":
            cfg["json_attributes_topic"] = BASE + "/events/last"
        if sid == "ai_objects":
            cfg["json_attributes_topic"] = BASE + "/ai/objects"
            cfg["json_attributes_template"] = '{{ {"oggetti": value_json} | tojson }}'
        if sid == "schedules":
            cfg["json_attributes_topic"] = BASE + "/schedules"
            cfg["json_attributes_template"] = '{{ {"programmazioni": value_json} | tojson }}'
        pub("sensor", sid, cfg)

    # --- text: orari Non disturbare (HH:MM) ---
    for sid, nm in (("dnd_start", "Non disturbare inizio"), ("dnd_end", "Non disturbare fine")):
        pub("text", sid, {"name": nm, "unique_id": UID + "_txt_" + sid, "command_topic": BASE + "/set/" + sid,
            "state_topic": stt, "value_template": "{{ value_json." + sid + " }}",
            "pattern": "^([01]?[0-9]|2[0-3]):[0-5][0-9]$", "min": 4, "max": 5, "icon": "mdi:clock-outline",
            "entity_category": "config"})

    # --- binary_sensor: c'e' un problema (errore robot o stazione non ignorato) ---
    pub("binary_sensor", "problem", {"name": "Problema", "unique_id": UID + "_problem",
        "state_topic": stt, "value_template": "{{ value_json.problem | default('OFF') }}",
        "payload_on": "ON", "payload_off": "OFF", "device_class": "problem"})
    log("HA discovery pubblicata (vacuum + select/number/switch/button + sensori + problema)")

STATE_KEYS = ("rssi","ssid","ip","uptime","fw","activity","clean_area","clean_time","clean_type",
              "total_area","total_time","total_count","water","mop","fan","work_mode","station",
              "volume","error","life_brush","life_sideBrush","life_heap","map_name",
              "continuous","clean_count","clean_pref","advanced","carpet","true_detect","multimap","wash_freq",
              "auto_empty","rooms","error_code","station_error","station_error_code","problem",
              "dnd","dnd_start","dnd_end","child_lock","border_spin","efficiency","sweep_mode","carpet_mode",
              "custom_area_mode","schedules","event","event_code","ai_objects","avoid_count","ai_types","ai_active")
_last_doc = [None]
_last_err = [None]
_last_pub = {}
def publish_state(c):
    s = get_state()
    if s.get("charging"): state = "docked"
    else:                 state = s.get("activity") or "idle"
    doc = {"state": state, "battery_level": s.get("battery") if s.get("battery") is not None else 0}
    if s.get("fan") in FAN_NAMES.values(): doc["fan_speed"] = s.get("fan")
    for k in STATE_KEYS:
        if s.get(k) is not None: doc[k] = s.get(k)
    c.publish(BASE + "/state", json.dumps(doc))
    # errori attivi con codice + significato (retained, solo quando cambiano)
    if s.get("_rc") is not None or s.get("_sc") is not None:
        ign = _ignored()
        act = {"robot": _err_items(s.get("_rc") or [], ign),
               "stazione": _err_items(s.get("_sc") or [], ign),
               "problema": s.get("problem", "OFF")}
        j = json.dumps(act, ensure_ascii=False, sort_keys=True)
        if j != _last_err[0]:
            c.publish(BASE + "/errors/active", j, retain=True); _last_err[0] = j
            log("errori attivi: %s" % j)
    # programmazioni / oggetti AI / ultimo evento: retained, solo quando cambiano
    for key, topic, val in (("sched", "/schedules", s.get("_sched")), ("ai", "/ai/objects", s.get("_ai")),
                            ("evt", "/events/last", {"codice": int(s["event_code"]), "significato": s.get("event")}
                                                    if s.get("event_code") else None)):
        if val is None: continue
        j = json.dumps(val, ensure_ascii=False, sort_keys=True)
        if j != _last_pub.get(key):
            c.publish(BASE + topic, j, retain=True); _last_pub[key] = j
    cmp = dict(doc); cmp.pop("uptime", None); cmp.pop("rssi", None)
    if cmp != _last_doc[0]:
        _last_doc[0] = cmp; log("stato: %s" % doc)

# ================= main =================
def on_message(topic, payload):
    p = payload.decode("utf-8", "ignore").strip()
    log("CMD %s = %s" % (topic, p))
    try:
        if topic == BASE + "/cmd":
            do_vacuum(p)
        elif topic == BASE + "/room":
            do_room(p)
        elif topic.startswith(BASE + "/set/"):
            do_set(topic[len(BASE) + 5:], p)
        elif topic.startswith(BASE + "/btn/"):
            do_btn(topic[len(BASE) + 5:])
    except Exception as e:
        log("on_message err: %s" % e)

def main():
    cid = "t20-" + DID[:8]
    c = MQTT(CFG["host"], int(CFG["port"]), CFG["user"], CFG["pass"], cid)
    c.will = (BASE + "/avail", "offline")      # se cadiamo/stacchiamo, HA marca le entita' non disponibili
    while True:
        try:
            c.connect()
            c.on_msg = on_message
            c.publish(BASE + "/avail", "online", retain=True)
            for sub in ("/cmd", "/set/+", "/btn/+", "/room"):
                c.subscribe(BASE + sub)
            publish_discovery(c)
            publish_error_catalog(c); _last_err[0] = None; _last_pub.clear()   # ripubblica anche gli errori attivi
            t = threading.Thread(target=c.loop); t.daemon = True; t.start()
            # sweep iniziale dei GET (popola subito i sensori), poi periodico
            st0 = threading.Thread(target=sweep_gets); st0.daemon = True; st0.start()
            mt = threading.Thread(target=map_loop, args=(c,)); mt.daemon = True; mt.start()
            last = 0; last_sweep = time.time()
            poll = int(CFG["poll"]); sweep = int(CFG["sweep"])
            while c.alive:
                now = time.time()
                if now - last >= poll:
                    publish_state(c); c.ping(); last = now
                if now - last_sweep >= sweep:
                    sw = threading.Thread(target=sweep_gets); sw.daemon = True; sw.start(); last_sweep = now
                time.sleep(1)
        except Exception as e:
            log("errore: %s (riprovo tra 10s)" % e)
        time.sleep(10)

if __name__ == "__main__":
    if "--daemon" in sys.argv: daemonize()
    main()
