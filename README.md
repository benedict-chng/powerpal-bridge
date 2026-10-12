# powerpal-bridge

Reads live power readings from a [Powerpal](https://powerpal.net) energy
monitor over Bluetooth LE and posts them to
[home-energy-server](https://github.com/benedict-chng/home-energy-server).

## Requirements

- A configured Powerpal (paired in the official Powerpal app at least once)
- A Raspberry Pi / Linux box or Windows PC with Bluetooth
- Python 3.9+, [bleak](https://github.com/hbldh/bleak) 3.x, python-dotenv

## Powerpal device info you'll need

| What | Where to find it |
|------|------------------|
| BLE MAC address | Device sticker, nRF Connect app, or your bridge logs |
| Pairing code | 6 digits you set during Powerpal app setup (also in the app / info pack) |
| Pulses per kWh | Your meter's pulse constant — confirm against the Powerpal app |

## Install

```bash
git clone https://github.com/benedict-chng/powerpal-bridge.git
cd powerpal-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# edit .env with your device values
```

## Configuration (.env)

All settings come from `.env` (loaded at startup via python-dotenv); real
environment variables still take precedence.

| Variable | Purpose |
|----------|---------|
| `POWERPAL_MAC` | Powerpal BLE MAC, lowercase (e.g. `d8:3f:ae:a6:fd:45`) — set in `.env` |
| `PAIRING_CODE` | 6-digit pairing code (leading zero dropped: `024024` → 24024) — set in `.env` |
| `PULSES_PER_KWH` | Meter pulse constant — must match the Powerpal app (default `3200`) |
| `INTERVAL_MINUTES` | Measurement batch window in minutes, 1–15 (default `1`) |
| `LIVE_PULSE` | `true` = per-pulse instantaneous watts (chews the Powerpal battery) — default `false` |
| `RETRY_DELAY` | Seconds between reconnect attempts (default `15`) |
| `INGEST_URL` | `http://192.168.68.141:8000/api/v1/readings` — home-energy-server ingest endpoint |
| `INGEST_API_KEY` | *(required for posting)* — the server's ingest API key |
| `STALE_SECONDS` | `300` — discard readings older than this (historic-replay filter) |

## Run

```bash
.venv/bin/python powerpal-gateway.py
```

On the first connect you'll get a device info panel (serial, firmware,
battery %), then one line per measurement batch. The device drops its
connection roughly every 6–8 minutes by design — the bridge reconnects
automatically.

### Run as a systemd service

```ini
# /etc/systemd/system/powerpal-bridge.service
[Unit]
Description=PowerPal BLE bridge
After=network.target bluetooth.target

[Service]
User=pi
WorkingDirectory=/home/pi/powerpal-bridge
ExecStart=/home/pi/powerpal-bridge/.venv/bin/python powerpal-gateway.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`WorkingDirectory` matters — that's where `.env` is picked up from.
(Alternatively add `EnvironmentFile=/home/pi/powerpal-bridge/.env`.)

```bash
sudo systemctl enable --now powerpal-bridge
journalctl -u powerpal-bridge -f
```

## How it works

1. Scans until it finds the configured MAC
2. Connects (pairs first on Windows), writes the pairing code to the
   pairing characteristic (`59DA0011`) — everything else stays locked
   until the device is authenticated
3. Syncs the Powerpal clock, sets the batch window via `59DA0013`
4. Subscribes to measurement notifications on `59DA0001` — payload is
   `[unix_ts: uint32 LE][pulses: uint16 LE][...]`
5. `LIVE_PULSE=true` additionally subscribes to per-pulse notifications
   (`59DA0003`) for instantaneous watts
6. Each accepted reading is POSTed to `INGEST_URL` with your
   `INGEST_API_KEY` — the server deduplicates by time, so retries never
   create duplicates. Failed posts queue in memory (~12 h) and retry
   with the next batch. Readings older than `STALE_SECONDS` or
   future-dated are discarded — the device replays stored history when
   its batch window is reduced.

## Protocol reference

Big credit to [WeekendWarrior1/powerpal_ble](https://github.com/WeekendWarrior1/powerpal_ble)
for the BLE reverse-engineering, and
[pento/powerpal-esphome](https://github.com/pento/powerpal-esphome) for a
working reference implementation.
