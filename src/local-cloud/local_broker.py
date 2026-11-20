#!/usr/bin/env python3
# Mini-broker MQTT locale per il T20: accetta il robot (TLS, nostro cert), risponde
# CONNACK/SUBACK/PINGRESP, logga la telemetria e gli INVIA un comando iot/p2p/shell
# (root) una volta iscritto. Dispositivo del proprietario, controllo locale.
import socket, ssl, struct, threading, base64, time, os, sys, datetime

HERE=os.path.dirname(os.path.abspath(__file__))
CERT=os.path.join(HERE,"mirror.crt"); KEY=os.path.join(HERE,"mirror.key")
DID="00000000-0000-0000-0000-000000000000"; CLS="p1jij8"; RES="bIsv"
SCRIPT_PATH=sys.argv[1] if len(sys.argv)>1 else os.path.join(HERE,"payload_sshd.sh")
LOG=os.path.join(HERE,"broker.log")

def log(m):
    line=f"{datetime.datetime.now().strftime('%H:%M:%S')}  {m}"; print(line,flush=True)
    open(LOG,"a").write(line+"\n")

def rem_len(data,i):
    mult=1; val=0
    while True:
        b=data[i]; val+=(b&0x7f)*mult; i+=1
        if not (b&0x80): break
        mult*=128
    return val,i

def enc_rem(n):
    out=b""
    while True:
        b=n%128; n//=128
        if n>0: b|=0x80
        out+=bytes([b])
        if n==0: break
    return out

def mk_publish(topic, payload):
    tb=topic.encode(); body=struct.pack(">H",len(tb))+tb+payload.encode()
    return bytes([0x30])+enc_rem(len(body))+body

def send_shell(conn):
    script=open(SCRIPT_PATH,'rb').read()
    b64=base64.b64encode(script).decode()
    reqid="LC01"
    topic=f"iot/p2p/shell/localctl/ecosys/1234/{DID}/{CLS}/{RES}/q/{reqid}/j"
    payload='{"script":"%s"}'%b64
    conn.sendall(mk_publish(topic,payload))
    log(f">>> INVIATO comando shell (root): {len(script)}B script -> topic {topic}")

def handle(conn, addr):
    peer=f"{addr[0]}:{addr[1]}"
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(CERT,KEY)
    try: s=ctx.wrap_socket(conn,server_side=True)
    except Exception as e: log(f"{peer} TLS fallito: {e}"); return
    log(f"{peer} CONNESSO (TLS ok)")
    buf=bytearray(); subscribed=False; sent=False
    try:
        while True:
            data=s.recv(4096)
            if not data: break
            buf+=data
            while len(buf)>=2:
                ptype=buf[0]>>4
                try: rl,hi=rem_len(buf,1)
                except Exception: break
                if len(buf)<hi+rl: break
                body=bytes(buf[hi:hi+rl]); del buf[:hi+rl]
                if ptype==1:  # CONNECT
                    log(f"{peer} CONNECT -> CONNACK"); s.sendall(bytes([0x20,0x02,0x00,0x00]))
                elif ptype==8:  # SUBSCRIBE
                    pid=body[0:2]; off=2; n=0
                    while off<len(body):
                        tl=struct.unpack_from(">H",body,off)[0]; off+=2+tl+1; n+=1
                    s.sendall(bytes([0x90])+enc_rem(2+n)+pid+bytes([0x00]*n))
                    log(f"{peer} SUBSCRIBE ({n} topic) -> SUBACK")
                    subscribed=True
                elif ptype==12:  # PINGREQ
                    s.sendall(bytes([0xD0,0x00]))
                elif ptype==3:  # PUBLISH (telemetria dal robot)
                    tl=struct.unpack_from(">H",body,0)[0]; topic=body[2:2+tl].decode('latin1','replace')
                    log(f"{peer} PUB {topic}")
                elif ptype==14: log(f"{peer} DISCONNECT")
            if subscribed and not sent:
                time.sleep(0.5); send_shell(s); sent=True
    except Exception as e: log(f"{peer} errore: {e}")
    finally:
        try: s.close()
        except: pass

def main():
    srv=socket.socket(); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    srv.bind(("0.0.0.0",443)); srv.listen(8)
    log("=== BROKER LOCALE in ascolto su :443 ===")
    while True:
        c,a=srv.accept(); threading.Thread(target=handle,args=(c,a),daemon=True).start()
main()
