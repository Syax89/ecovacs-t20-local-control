# The Ecovacs MQTT protocol (as captured on the T20 Omni)

Everything below was reconstructed from a **transparent MITM capture** of the robot talking to the real
Ecovacs cloud (the robot does not validate the broker's certificate, so a LAN proxy can read the stream
in clear text). It matches the protocol implemented by the open-source **deebot-client** library — so
once you understand this, you can reuse that library's command catalogue.

Real identifiers below are replaced with placeholders. Substitute your own robot's `<DID>`, `<SERIAL>`,
class and resource (see the Wiki for how to read them off the device).

## 1. Connection

- Transport: **MQTT over TLS, port 443**, broker `jmq-ngiot-<cc>.area.ww.ecouser.net`
  (`<cc>` = a region code, e.g. `it`). **The robot does NOT validate the server certificate.**
- `CONNECT`:
  - clientID = `<DID>@<class>/<resource>` (e.g. `…@p1jij8/bIsv`)
  - username = the robot's **serial number** `<SERIAL>`

## 2. Telemetry — robot publishes to `iot/atr/on<Event>/<DID>/<class>/<resource>/j`

Spontaneous state events. Payload shape:

```json
{"header":{"pri":"1","ts":<ms>,"ver":"…","fwVer":"1.24.0", …}, "body":{"data":{ … }}}
```

Representative events:

| Topic | Payload (example) |
|---|---|
| `onBattery` | `{"value":100,"isLow":0}` |
| `onChargeState` | `{"isCharging":1,"mode":"slot"}` |
| `onError` | `{"code":[0]}` |
| `onWorkMode` / `onSpeed` | `{"mode":1}` / `{"speed":0}` |
| `onStationState` / `onStationInfo` | Omni station status |
| `onWaterInfo` / `onMopMoisture` / `onSweepMode` | mopping / water / mode |
| `onMajorMap` / `onMinorMap` / `onMapTrace` / `onMapInfo_V2` / `onMapSet_V2` | the map (incremental + full) and the path |
| `onSched_V2` / `onNextSched` | schedules |
| `onFwBuryPoint-*` | **analytics / telemetry** (see [PRIVACY.md](PRIVACY.md)) |

## 3. Commands — `iot/p2p/*` request/response

The robot **subscribes** to:

```
iot/p2p/+/+/+/+/<DID>/<class>/<resource>/+/+/+
```

- **Request** (cloud → robot):
  `iot/p2p/<CMD>/<sender>/ecosys/1234/<DID>/<class>/<resource>/q/<reqid>/j`
- **Response** (robot → cloud):
  `iot/p2p/<CMD>/<DID>/<class>/<resource>/<sender>/ecosys/1234/p/<reqid>/j`

`q` = request, `p` = response; `<reqid>` ties the two together. This split is exactly what the on-robot
bridge keys on: it only treats `parts[9] == "p"` as responses when sniffing telemetry, and injects
commands on the `…/q/<reqid>/j` form with `<sender> = hlprLocal`.

Example — `getBattery`:

```
request : {"header":{"pri":"1","ts":…,"ver":"0.0.50"}}
response: {"header":{…},"body":{"code":0,"msg":"ok","data":{"value":100,"isLow":0}}}
```

### Command catalogue (data payloads only; the bridge wraps `{header, body:{data}}`)

Getters used for state (polled by the agent): `getBattery`, `getChargeState`, `getSpeed`,
`getWaterInfo`, `getWorkMode`, `getVolume`, `getCleanInfo`, `getStats`, `getTotalStats`, `getLifeSpan`,
`getError`, `getStationState`, `getStationInfo`, `getBreakPoint`, `getCleanCount`, `getCleanPreference`,
`getAdvancedMode`, `getTrueDetect`, `getCarpertPressure`, `getAutoEmpty`, `getMapState`,
`getMajorMap`, `getMinorMap`, `getMapSet` (`type:ar` → rooms), `getPos`, `getSched_V2`.

Setters / actions:

| Intent | Command + data |
|---|---|
| Start / stop / pause / resume | `clean {act:start, type:auto}` / `{act:stop}` / `{act:pause}` / `{act:resume}` |
| Return to dock | `charge {act:go}` |
| Clean specific rooms | `clean {act:start, type:spotArea, content:"<mssid,mssid>", count:1}` |
| Fan speed | `setSpeed {speed: 1000/0/1/2}` = quiet/standard/strong/max |
| Water level | `setWaterInfo {amount: 1..4}` |
| Work mode | `setWorkMode {mode: 0/1/2/3}` = vac+mop / vac / mop / mop-after-vac |
| Volume | `setVolume {volume: 0..10}` |
| Locate (beep) | `playSound {count:1, sid:30}` |
| Relocate | `setRelocationState {mode:"manu"}` |
| Continuous / advanced / carpet-boost / true-detect | `setBreakPoint`, `setAdvancedMode`, `setCarpertPressure`, `setTrueDetect` `{enable:0/1}` |
| Mop wash interval | `setWashInfo {interval: 0..60}` |
| Reset consumable | `resetLifeSpan {type: brush/sideBrush/heap}` |

Omni station actions (payloads extracted statically from the official app):

| Intent | Command + data |
|---|---|
| Empty dustbin | `autoEmpty {act:start}` |
| Wash mop | `clean_V2 {act:start, content:{type:washing}}` |
| Dry mop (start / stop) | `stationAction {act:1, type:2}` / `{act:4, type:2}` |
| Station self-clean | `stationAction {act:1, type:1}` |
| Auto-empty on/off | `setAutoEmpty {enable:0/1}` |

## 4. 🔑 The root primitive: `iot/p2p/shell`

The cloud sends the robot:

```
topic  : iot/p2p/shell/<sender>/ecosys/1234/<DID>/<class>/<resource>/q/<reqid>/j
payload: {"script":"<base64 of a /bin/sh script>"}
```

The robot **executes that script as root** and replies `{"ret":"ok"}`. Because we can stand up a broker
the robot will connect to (no cert validation) and we know the topic format, **we can send our own
`shell` command and run arbitrary code as root over the LAN** — no firmware flash, no UART. This is the
entire basis of [SSH-ROOT.md](SSH-ROOT.md).

## 5. The map wire format (summary)

Maps are served locally over the same p2p channel; see [MAP-RENDERING.md](MAP-RENDERING.md) for the full
decode. In short:

- `getMajorMap` → a grid of per-piece CRCs (`cellWidth`, `mid`, `value` = CRC list; empty piece CRC is
  the constant `1295764014`).
- `getMinorMap {pieceIndex, type:"ol"}` → `{pieceValue: <base64 7z/LZMA>}` → **10 000 bytes** per piece
  (a 100×100 cell block). Pixel value `0` = outside/unknown, `>=100` = floor, `1..99` = wall/edge.
- `getPos` → `{deebotPos, chargePos}`; `getMapSet {type:ar}` → room subsets (`mssid`).

Fragment decoding is **byte-identical** to deebot-client's `decompress_base64_data`: base64-decode,
keep bytes `[0:8]`, insert four `0x00` bytes, append bytes `[8:]`, then LZMA-decompress.
