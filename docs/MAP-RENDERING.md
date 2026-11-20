# Rendering the map locally

The robot stores its map **locally** (`/data/offmap_<mid>.json`) and serves it over the same p2p channel
the app uses. We decode the fragments and draw a PNG that Home Assistant shows as a **camera** entity —
no cloud, no app.

## Pipeline

```
robot agent ──MQTT──▶ t20/<did8>/map/raw   (JSON: fragments + path + positions + rooms, retained)
                               │
                   t20_map_render.py on <HA_HOST>  (python3 + Pillow; lzma is stdlib)
                               │
                        ──MQTT──▶ t20/<did8>/map    (PNG, retained)  ──▶ HA camera (MQTT discovery)
```

The on-robot agent collects `getMajorMap` + `getMinorMap` pieces + `getPos` + `getMapSet` and publishes
them as one retained JSON every few minutes. The bridge was patched to **accumulate minor-map pieces by
index** (a naïve single-key cache with a 3 s throttle only kept 5–6 of 9 pieces; now all pieces survive).

## Fragment decoding (7z / LZMA)

Each piece is base64 then a headerless LZMA stream in the Ecovacs/deebot convention. The decode is
**byte-identical** to deebot-client's `decompress_base64_data`:

```python
import base64, lzma
def decompress_7z_b64(data):
    d = base64.b64decode(data)
    final = bytearray(d[0:8])      # props(1) + dict-size(4) + first 3 bytes of the size field
    final += b"\x00\x00\x00\x00"   # pad the 8-byte uncompressed-size field (the deebot/sucks trick)
    final += d[8:]
    return lzma.LZMADecompressor().decompress(bytes(final))
```

Each decoded piece is **10 000 bytes** = a 100×100 cell block. Pixel values: `0` = outside/unknown,
`>=100` = floor, `1..99` = wall/edge.

## Geometry (the part that's easy to get wrong)

- Pieces are laid out **x-major** (per column): fragment `idx` → column `idx // rows`, row `idx % rows`;
  inside a fragment, `byte[a*P + b]` is cell `(x=a, y=b)`, with **y pointing up (north)**.
- Grid centre (for mm→cell) is `cols*P/2`; one cell = `cell_mm` (50 mm on the T20).
- The path (`getMapTrace`) is 5-byte points: `x,y` int16 LE (unit = 10 mm) + a flag whose bit 7 marks a
  pen-up (discontinuity).

Verified against a real run: 100% of path points fall on floor cells with the x-major reading (only 64%
with a naïve row-major reading), which is how the layout was confirmed.

The renderer also plots AI-recognised objects, the cleaning path, and the robot/charger positions, then
crops to the bounding box of everything known.

## Running it

[`src/map-renderer/`](../src/map-renderer/) ships three ways to run it on your always-on HA host:

```bash
# one-shot (no MQTT): decode a saved map/raw JSON into a PNG
python3 t20_map_render.py --oneshot map_raw.json --out map.png

# Docker
docker compose up -d --build        # edit host/user/pass/did in docker-compose.yml first

# systemd
sudo ./install.sh                   # edit the ExecStart line in the .service first
```

All of host/user/pass/did default to placeholders — set them to your values.
