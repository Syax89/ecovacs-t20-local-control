# `map-renderer/` — map → Home Assistant camera

Runs on an always-on host (ideally your Home Assistant host), Python 3 with **`paho-mqtt`** and
**`Pillow`** (`lzma` is stdlib). Subscribes to the robot's raw map topic, decodes the 7z/LZMA fragments,
draws a PNG and publishes it as an HA **camera** entity via MQTT discovery. Design in
[`../../docs/MAP-RENDERING.md`](../../docs/MAP-RENDERING.md).

## Files

| File | Purpose |
|---|---|
| `t20_map_render.py` | The renderer (decode + draw + publish; `--oneshot` mode works without MQTT). |
| `Dockerfile`, `docker-compose.yml` | Run as a container. |
| `t20-map-render.service`, `install.sh` | Run as a systemd service. |

## Usage

```bash
# one-shot: decode a saved map/raw JSON into a PNG (no MQTT)
python3 t20_map_render.py --oneshot map_raw.json --out map.png

# Docker (edit host/user/pass/did in docker-compose.yml first)
docker compose up -d --build

# systemd (edit the ExecStart line in t20-map-render.service first)
sudo ./install.sh
```

CLI flags: `--host <HA_HOST> --port 1883 --user <u> --pass <p> --did <DID> --name "Deebot T20 Omni"
--scale 4`. All default to placeholders — set them to your values.
