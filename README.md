# powerpal-bridge

Reads live power readings from a [Powerpal](https://powerpal.net) energy
monitor over Bluetooth LE and posts them to
[home-energy-server](https://github.com/benedict-chng/home-energy-server).

One reading per measurement notification (default: once per minute),
posted as a single JSON object to the server's ingest API:

```json
{"time": "2026-10-12T11:00:00+00:00", "pulses": 12, "kwh": 0.012, "average_watts": 230.5}
```

Posting is idempotent server-side (readings are keyed by time), so retries
after network hiccups never create duplicates. Failed posts are queued in
memory and retried automatically.

## Requirements

- A configured Powerpal (paired in the official Powerpal app at least once)
- A Raspberry Pi / Linux box with Bluetooth, on the same LAN as home-energy-server
- Python 3.9+ and [bleak](https://github.com/hbldh/bleak)

## Powerpal device info you'll need

| What | Where to find it |
|------|------------------|
| BLE MAC address | Device sticker, nRF Connect app, or `--scan` (below) |
| Pairing code | 6 digits you set during Powerpal app setup (also in the app / info pack) |
| Pulses per kWh | Your meter's pulse constant — confirm against the Powerpal app (commonly 1000) |

## Install

```bash
git clone https://github.com/benedict-chng/powerpal-bridge.git
cd powerpal-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Configuration (environment variables)

| Variable | Default | Purpose |
|----------|---------|---------|
| `POWERPAL_ADDRESS` | *(scan by name)* | Powerpal BLE MAC (lowercase, e.g. `df:5c:55:00:00:00`) |
| `POWERPAL_PAIRING_CODE` | *(required)* | 6-digit pairing code (a leading zero is fine: `024024` → 24024) |
| `POWERPAL_BATCH_MINUTES` | `1` | Measurement window requested from the device (1–60) |
| `PULSES_PER_KWH` | `1000` | Meter pulse constant — **must match the Powerpal app** |
| `INGEST_URL` | `http://192.168.68.141:8000/api/v1/readings` | home-energy-server ingest endpoint |
| `INGEST_API_KEY` | *(required)* | The server's ingest API key |
| `STALE_SECONDS` | `300` | Discard readings older than this (historic-replay filter) |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` |

## Run

```bash
INGEST_API_KEY=*** \
POWERPAL_PAIRING_CODE=123456 \
.venv/bin/python powerpal-gateway.py
```

Useful flags:

```bash
# find your Powerpal's MAC address (needs Bluetooth on this machine)
.venv/bin/python powerpal-gateway.py --scan
```

### Run as a systemd service

```ini
# /etc/systemd/system/powerpal-bridge.service
[Unit]
Description=PowerPal BLE bridge -> home-energy-server
After=network.target bluetooth.target

[Service]
User=pi
WorkingDirectory=/home/pi/powerpal-bridge
EnvironmentFile=/home/pi/powerpal-bridge/bridge.env
ExecStart=/home/pi/powerpal-bridge/.venv/bin/python powerpal-gateway.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now powerpal-bridge
journalctl -u powerpal-bridge -f
```

## How it works

1. Connects to the Powerpal over BLE and writes your pairing code to the
   pairing characteristic (`59DA0011`) — the device locks everything else
   until it's authenticated.
2. Writes the batch window (default 1 minute) to `59DA0013`.
3. Subscribes to measurement notifications on `59DA0001`. Each payload is
   `[unix_ts: uint32 LE][pulses: uint16 LE][...]`.
4. Converts pulses to kWh (`pulses / pulses_per_kwh`) and average watts
   (`kWh × 1000 × 3600 / window_seconds`), then POSTs to the server.

If the batch window is ever **reduced**, the device replays its stored
history — the bridge filters anything older than `STALE_SECONDS` so old
datapoints don't pollute current readings.

## Protocol reference

Huge credit to [WeekendWarrior1/powerpal_ble](https://github.com/WeekendWarrior1/powerpal_ble)
for the BLE reverse-engineering, and to
[pento/powerpal-esphome](https://github.com/pento/powerpal-esphome) for a
working reference implementation.
