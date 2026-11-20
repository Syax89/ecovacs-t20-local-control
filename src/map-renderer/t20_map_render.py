#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Renderer mappa T20 -> immagine PNG per Home Assistant, via MQTT. Gira su un nodo con python3
# (idealmente l'host di Home Assistant). Dipendenze: solo `paho-mqtt` e `Pillow` (lzma e' stdlib).
#
#   robot (agente) --MQTT--> t20/<did8>/map/raw  (JSON: frammenti b64 + posizione + stanze)
#        |                                   this script (decodifica + disegna)
#        +-------------------> t20/<did8>/map      (PNG)  -> HA entita' camera (MQTT discovery)
#
# Uso:
#   servizio:   python3 t20_map_render.py --host 192.168.1.10 --user CHANGEME --pass CHANGEME
#   one-shot:   python3 t20_map_render.py --oneshot map_raw.json --out mappa.png   (nessun MQTT)
#
# I valori pixel della mappa "ol": 0 = sconosciuto/esterno; >=100 = pavimento; 1..99 = muro/bordo.
import sys, os, json, base64, lzma, argparse, io, struct

# ---------- decodifica frammenti (formato 7z/LZMA Ecovacs) ----------
def decompress_7z_b64(data):
    d = base64.b64decode(data)
    final = bytearray(d[0:8])          # props(1)+dictsize(4)+primi 3 byte size
    final += b"\x00\x00\x00\x00"       # completa il campo size a 8 byte (trick deebot/sucks)
    final += d[8:]
    return lzma.LZMADecompressor().decompress(bytes(final))

# ---------- percorso (getMapTrace): punti da 5 byte = x,y int16 LE + flag (bit7=1 -> stacco) ----------
TRACE_MM = 10.0   # 1 unita' del percorso = 10 mm (stesso riferimento delle posizioni, y verso l'alto)
def decode_trace(trace):
    pts = []
    for ch in (trace or {}).get("chunks") or []:
        try: b = decompress_7z_b64(ch)
        except Exception: continue
        for i in range(0, len(b) - 4, 5):
            x, y = struct.unpack("<hh", b[i:i + 4])
            pts.append((x * TRACE_MM, y * TRACE_MM, ((b[i + 4] >> 7) & 1) == 0))
    return pts

AI_COLORS = {0: (149, 165, 166), 1: (142, 68, 173), 2: (39, 174, 96), 3: (230, 126, 34),
             4: (211, 84, 0), 5: (41, 128, 185), 6: (192, 57, 43)}
AI_LETTER = {0: "?", 1: "C", 2: "B", 3: "T", 4: "M", 5: "S", 6: "F"}  # cestino/base/tappeto/moquette/scarpa/filo

# ---------- rendering ----------
def render_png(doc, scale=4, floor=(210,224,245), wall=(70,90,120),
               bg=(0,0,0,0), robot=(231,76,60), charger=(39,174,96)):
    from PIL import Image, ImageDraw
    cols = doc.get("cols", 8); rows = doc.get("rows", 8); P = doc.get("piece", 100)
    W, H = cols * P, rows * P
    grid = bytearray(W * H)            # 0 = vuoto
    for key, pv in (doc.get("pieces") or {}).items():
        idx = int(key)
        try: px = decompress_7z_b64(pv)
        except Exception: continue
        # Formato Ecovacs "per colonna" (x-major): frammento idx -> colonna idx//rows, riga idx%rows;
        # dentro il frammento byte[a*P+b] = cella (x=a, y=b), con y verso l'alto (nord).
        # Verificato sul percorso reale: 100% dei punti cade sul pavimento (lettura per riga: 64%).
        X0 = (idx // rows) * P; Y0 = (idx % rows) * P
        for a in range(P):
            colv = px[a * P:(a + 1) * P]; X = X0 + a
            for b in range(P):
                v = colv[b]
                if v: grid[(H - 1 - (Y0 + b)) * W + X] = v
    # bounding box dei pixel noti (ritaglio)
    minx = miny = 10**9; maxx = maxy = -1
    for y in range(H):
        b = y * W
        for x in range(W):
            if grid[b + x]:
                if x < minx: minx = x
                if x > maxx: maxx = x
                if y < miny: miny = y
                if y > maxy: maxy = y
    # estende il riquadro a percorso e oggetti AI (escono dai pixel noti, es. soglie/porte)
    cell0 = doc.get("cell_mm", 50); c0x, c0y = W / 2.0, H / 2.0
    extra = [(x, y) for (x, y, _c) in decode_trace(doc.get("trace"))] + \
            [(o.get("x"), o.get("y")) for o in (doc.get("ai") or []) if o.get("x") is not None]
    for (xm, ym) in extra:
        gx = int(c0x + float(xm) / cell0); gy = int((H - 1) - (c0y + float(ym) / cell0))
        if 0 <= gx < W and 0 <= gy < H:
            minx = min(minx, gx); maxx = max(maxx, gx); miny = min(miny, gy); maxy = max(maxy, gy)
    if maxx < 0:
        img = Image.new("RGBA", (10, 10), bg);
        return _to_png(img)
    pad = 4
    minx = max(0, minx - pad); miny = max(0, miny - pad)
    maxx = min(W - 1, maxx + pad); maxy = min(H - 1, maxy + pad)
    cw, ch = maxx - minx + 1, maxy - miny + 1
    img = Image.new("RGBA", (cw, ch), bg)
    pix = img.load()
    for y in range(miny, maxy + 1):
        b = y * W
        for x in range(minx, maxx + 1):
            v = grid[b + x]
            if not v: continue
            pix[x - minx, y - miny] = (floor + (255,)) if v >= 100 else (wall + (255,))
    img = img.resize((cw * scale, ch * scale), Image.NEAREST)
    draw = ImageDraw.Draw(img)
    # posizione robot / base (mm -> cella: centro griglia = cols*P/2, cella = cell_mm)
    cell = doc.get("cell_mm", 50)
    cx0, cy0 = W / 2.0, H / 2.0
    def to_img(xmm, ymm):
        gx = cx0 + float(xmm) / cell
        gy = (H - 1) - (cy0 + float(ymm) / cell)
        return ((gx - minx) * scale, (gy - miny) * scale)
    # percorso di pulizia
    pts = decode_trace(doc.get("trace"))
    prev = None
    for (xm, ym, conn) in pts:
        p = to_img(xm, ym)
        if prev is not None and conn:
            draw.line([prev, p], fill=(46, 204, 113, 255), width=max(1, scale // 2))
        prev = p
    # oggetti riconosciuti dall'AI
    for o in doc.get("ai") or []:
        try:
            x, y = to_img(o["x"], o["y"]); r = 6; ty = o.get("type")
            draw.ellipse([x-r, y-r, x+r, y+r], fill=AI_COLORS.get(ty, (127, 140, 141)), outline=(255, 255, 255))
            draw.text((x - 3, y - 6), AI_LETTER.get(ty, "?"), fill=(255, 255, 255))
        except Exception: pass
    pos = doc.get("pos") or {}
    try:
        cp = pos.get("chargePos")
        if isinstance(cp, list) and cp: cp = cp[0]
        if cp:
            x, y = to_img(cp["x"], cp["y"]); r = 5
            draw.ellipse([x-r, y-r, x+r, y+r], fill=charger, outline=(255,255,255))
    except Exception: pass
    try:
        dp = pos.get("deebotPos")
        if isinstance(dp, list) and dp: dp = dp[0]
        if dp:
            x, y = to_img(dp["x"], dp["y"]); r = 7
            draw.ellipse([x-r, y-r, x+r, y+r], fill=robot, outline=(255,255,255))
    except Exception: pass
    return _to_png(img)

def _to_png(img):
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()

# ---------- MQTT ----------
def run_mqtt(a):
    import paho.mqtt.client as mqtt
    did8 = a.did[:8]
    base = "t20/%s" % did8
    raw_topic = base + "/map/raw"; img_topic = base + "/map"
    disc_topic = "homeassistant/camera/t20_%s_map/config" % did8
    disc = {"name": "Mappa", "unique_id": "t20_%s_map" % did8,
            "topic": img_topic, "availability_topic": base + "/avail",
            "device": {"identifiers": ["t20_%s" % did8], "name": a.name,
                       "manufacturer": "Ecovacs", "model": "Deebot T20 Omni (zj2231)"}}
    def on_connect(c, u, f, rc, *x):
        print("MQTT connesso rc=%s" % rc); c.subscribe(raw_topic)
        c.publish(disc_topic, json.dumps(disc), retain=True)
        print("discovery camera pubblicata; in ascolto su %s" % raw_topic)
    def on_message(c, u, m):
        try:
            doc = json.loads(m.payload.decode("utf-8"))
            png = render_png(doc, scale=a.scale)
            c.publish(img_topic, png, retain=True)
            print("mappa resa e pubblicata su %s (%d byte, %d frammenti)" %
                  (img_topic, len(png), len(doc.get("pieces") or {})))
        except Exception as e:
            print("render err: %s" % e)
    try: c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except Exception: c = mqtt.Client()
    if a.user: c.username_pw_set(a.user, getattr(a, "password"))
    c.on_connect = on_connect; c.on_message = on_message
    c.connect(a.host, a.port, 60); c.loop_forever()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.10"); ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--user", default="CHANGEME"); ap.add_argument("--pass", dest="password", default="CHANGEME")
    ap.add_argument("--did", default="00000000-0000-0000-0000-000000000000")
    ap.add_argument("--name", default="Deebot T20 Omni"); ap.add_argument("--scale", type=int, default=4)
    ap.add_argument("--oneshot", help="file JSON map/raw -> PNG, senza MQTT")
    ap.add_argument("--out", default="mappa.png")
    a = ap.parse_args()
    if a.oneshot:
        doc = json.load(open(a.oneshot)); open(a.out, "wb").write(render_png(doc, scale=a.scale))
        print("scritto %s" % a.out); return
    run_mqtt(a)

if __name__ == "__main__":
    main()
