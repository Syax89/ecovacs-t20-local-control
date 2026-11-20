#!/usr/bin/env python3
"""
Proxy MITM trasparente per il robot Ecovacs.
- Ascolta TLS su 443/8883/5223 con il nostro cert (il robot lo accetta).
- Legge la SNI, risolve l'IP REALE del dominio via 8.8.8.8 (bypassa il nostro rewrite),
  apre TLS verso il vero cloud Ecovacs e fa da ponte, LOGGANDO tutto in chiaro.
- Decodifica i pacchetti MQTT (CONNECT/PUBLISH/SUBSCRIBE) con topic e payload.
Scopo: imparare il protocollo di controllo e il flusso OTA. Uso: python3 mitm_proxy.py
"""
import socket, ssl, threading, subprocess, datetime, os

HERE=os.path.dirname(os.path.abspath(__file__))
CERT=os.path.join(HERE,"mirror.crt"); KEY=os.path.join(HERE,"mirror.key")
LOG=os.path.join(HERE,"mitm.log")
MY_IP="192.168.1.20"   # <-- set to THIS machine's LAN IP (the host the robot's DNS is rewritten to)
TLS_PORTS=[443,8883,5223]
PLAIN_PORTS=[80,8007]
_real={}
_lock=threading.Lock()

def log(msg):
    line=f"{datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {msg}"
    print(line,flush=True)
    try:
        with open(LOG,"a") as f: f.write(line+"\n")
    except OSError: pass

def real_ip(domain):
    # Risoluzione via DNS-over-HTTPS a 1.1.1.1 (porta 443): bypassa l'hijack del DNS
    # sulla porta 53 fatto dal router, così otteniamo il VERO IP di Ecovacs.
    with _lock:
        if domain in _real: return _real[domain]
    ip=None
    try:
        import json
        out=subprocess.run(["curl","-s","--max-time","6","-H","accept: application/dns-json",
                            f"https://1.1.1.1/dns-query?name={domain}&type=A"],
                           capture_output=True,text=True,timeout=8).stdout
        j=json.loads(out)
        ips=[a["data"] for a in j.get("Answer",[]) if a.get("type")==1]
        ip=ips[-1] if ips else None
    except Exception as e:
        log(f"[dns] errore DoH risolvendo {domain}: {e}")
    # sicurezza: non inoltrare mai a noi stessi
    if ip in (MY_IP,"127.0.0.1"):
        log(f"[dns] ATTENZIONE: {domain} risolto a {ip} (loop!) scartato"); ip=None
    with _lock: _real[domain]=ip
    log(f"[dns] {domain} -> reale {ip}")
    return ip

def mqtt_decode(buf, direction):
    """Consuma pacchetti MQTT completi da buf (bytearray), logga, ritorna byte residui."""
    TYPES={1:"CONNECT",2:"CONNACK",3:"PUBLISH",4:"PUBACK",8:"SUBSCRIBE",9:"SUBACK",
           10:"UNSUBSCRIBE",12:"PINGREQ",13:"PINGRESP",14:"DISCONNECT"}
    while len(buf)>=2:
        ptype=buf[0]>>4
        # remaining length (varint)
        mult=1; val=0; i=1; ok=False
        while i<len(buf):
            b=buf[i]; val+=(b&0x7f)*mult
            if b&0x80==0: ok=True; i+=1; break
            mult*=128; i+=1
            if mult>128**4: break
        if not ok: break
        total=i+val
        if len(buf)<total: break
        body=bytes(buf[i:total]); name=TYPES.get(ptype,f"?{ptype}")
        info=""
        try:
            if ptype==3:  # PUBLISH
                tl=(body[0]<<8)|body[1]; topic=body[2:2+tl].decode('latin1','replace')
                off=2+tl
                if (buf[0]>>1)&3>0: off+=2  # packet id se QoS>0
                payload=body[off:]
                info=f"topic='{topic}' len={len(payload)} payload={payload[:4000].decode('latin1','replace')!r}"
                try:
                    with open(os.path.join(HERE,"mqtt_full.log"),"a") as _ff:
                        _ff.write(f"{datetime.datetime.now().strftime('%H:%M:%S')} {direction} {topic} len={len(payload)}\n{payload[:4000].decode('latin1','replace')}\n\n")
                except OSError: pass
            elif ptype==8:  # SUBSCRIBE
                off=2; subs=[]
                while off<len(body):
                    tl=(body[off]<<8)|body[off+1]; off+=2; subs.append(body[off:off+tl].decode('latin1','replace')); off+=tl+1
                info="subs="+",".join(subs)
            elif ptype==1:  # CONNECT
                info=f"(connect) {body[:80].decode('latin1','replace')!r}"
        except Exception as e: info=f"(decode err {e}) {body[:60].hex()}"
        log(f"   {direction} MQTT {name} {info}")
        del buf[:total]
    return

def pump(src, dst, direction, is_mqtt):
    buf=bytearray()
    try:
        while True:
            data=src.recv(4096)
            if not data: break
            dst.sendall(data)
            if is_mqtt:
                buf+=data
                try: mqtt_decode(buf,direction)
                except Exception as e: log(f"   {direction} mqtt parse err {e}")
            else:
                # HTTP/altro: logga la prima riga utile
                txt=data[:200].decode('latin1','replace')
                if any(m in txt for m in ("GET ","POST ","HTTP/","Host:")):
                    log(f"   {direction} HTTP {txt.splitlines()[0] if txt.splitlines() else txt[:80]!r}")
    except Exception: pass
    finally:
        for s in (src,dst):
            try: s.close()
            except: pass

def handle(conn, addr, lport):
    peer=f"{addr[0]}:{addr[1]}"
    sni={}
    def sni_cb(sock,name,ctx): sni['n']=name
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(CERT,KEY)
    ctx.sni_callback=sni_cb
    try:
        s=ctx.wrap_socket(conn,server_side=True)
    except ssl.SSLError as e:
        log(f"[:{lport}] {peer} handshake fallito (valida CA): {e}"); conn.close(); return
    except Exception as e:
        conn.close(); return
    domain=sni.get('n')
    if not domain:
        # il client MQTT del robot si connette per IP senza SNI: assumiamo il broker jmq-ngiot
        domain="jmq-ngiot-it.area.ww.ecouser.net"
        log(f"[:{lport}] {peer} nessuna SNI -> assumo broker {domain}")
    rip=real_ip(domain)
    if not rip:
        log(f"[:{lport}] {peer} SNI={domain} ma IP reale ignoto, chiudo"); s.close(); return
    is_mqtt = lport in (443,8883) and domain.startswith("jmq")
    log(f"[:{lport}] {peer}  SNI={domain} -> cloud {rip}:{lport}  ({'MQTT' if is_mqtt else 'HTTP/altro'})")
    try:
        up=socket.create_connection((rip,lport),timeout=10)
        uctx=ssl.create_default_context(); uctx.check_hostname=False; uctx.verify_mode=ssl.CERT_NONE
        ups=uctx.wrap_socket(up,server_hostname=domain)
    except Exception as e:
        log(f"[:{lport}] {peer} upstream {rip}:{lport} fallito: {e}"); s.close(); return
    threading.Thread(target=pump,args=(s,ups,"ROBOT->CLOUD",is_mqtt),daemon=True).start()
    threading.Thread(target=pump,args=(ups,s,"CLOUD->ROBOT",is_mqtt),daemon=True).start()

def handle_plain(conn, addr, lport):
    peer=f"{addr[0]}:{addr[1]}"
    try:
        conn.settimeout(8); first=conn.recv(4096)
    except Exception: conn.close(); return
    # estrai Host header (HTTP)
    host=None
    try:
        for line in first.split(b"\r\n"):
            if line.lower().startswith(b"host:"): host=line.split(b":",1)[1].strip().decode('latin1'); break
    except Exception: pass
    reqline=first.split(b"\r\n",1)[0].decode('latin1','replace')
    if not host:
        log(f"[:{lport} PLAIN] {peer} nessun Host. dati: {first[:80]!r}"); conn.close(); return
    rip=real_ip(host)
    log(f"[:{lport} PLAIN] {peer} Host={host} -> {rip}  req: {reqline!r}")
    if not rip: conn.close(); return
    try:
        up=socket.create_connection((rip,lport),timeout=10); up.sendall(first)
    except Exception as e: log(f"[:{lport}] upstream {rip} fallito: {e}"); conn.close(); return
    threading.Thread(target=pump,args=(conn,up,"ROBOT->CLOUD",False),daemon=True).start()
    threading.Thread(target=pump,args=(up,conn,"CLOUD->ROBOT",False),daemon=True).start()

def serve(port, tls=True):
    srv=socket.socket(socket.AF_INET,socket.SOCK_STREAM); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    try: srv.bind(("0.0.0.0",port)); srv.listen(32)
    except Exception as e: log(f"[:{port}] bind fallito: {e}"); return
    log(f"[:{port}] MITM in ascolto ({'TLS' if tls else 'plain'})")
    while True:
        try:
            conn,addr=srv.accept()
            tgt=handle if tls else handle_plain
            threading.Thread(target=tgt,args=(conn,addr,port),daemon=True).start()
        except Exception as e: log(f"[:{port}] accept err {e}")

if __name__=="__main__":
    log("=== MITM PROXY avviato ===")
    for p in TLS_PORTS: threading.Thread(target=serve,args=(p,True),daemon=True).start()
    for p in PLAIN_PORTS: threading.Thread(target=serve,args=(p,False),daemon=True).start()
    import time
    while True: time.sleep(3600)
