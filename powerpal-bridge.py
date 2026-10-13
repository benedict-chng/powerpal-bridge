#!/usr/bin/env python3
"""
Powerpal BLE Client -- Standalone, Cross-platform (Windows/Linux)
Based on: https://github.com/Sage222/SagePowerPalStandalone

Runs fully automatically -- no prompts. Edit the config block below.
Keeps scanning until the Powerpal is found, then connects and streams.
Auto-reconnects whenever the device drops the connection (~6-8 min by design).

Linux setup (one-time):
  bluetoothctl -> scan on -> pair <MAC> -> trust <MAC> -> quit

Linux run:
  source ~/powerpal-env/bin/activate
  python powerpal_ble_standalone.py
"""

import asyncio
import json
import logging
import os
import struct
import time
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler

from dotenv import load_dotenv
from bleak import BleakScanner, BleakClient

load_dotenv()  # reads .env from the working directory (see .env.example)


# -- Config -- from .env / environment (see .env.example) -----------------------
def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


POWERPAL_MAC     = os.environ.get("POWERPAL_MAC", "D8:3F:AE:A6:FD:45")
PAIRING_CODE     = _env_int("PAIRING_CODE", 411758)
PULSES_PER_KWH   = _env_int("PULSES_PER_KWH", 3200)
INTERVAL_MINUTES = _env_int("INTERVAL_MINUTES", 1)   # batch size in minutes (1-15)
LIVE_PULSE       = _env_bool("LIVE_PULSE", False)    # per-pulse watts (more battery)
RETRY_DELAY      = _env_int("RETRY_DELAY", 15)       # seconds between reconnects

INGEST_URL       = os.environ.get("INGEST_URL", "http://192.168.68.141:8000/api/v1/readings")
INGEST_API_KEY   = os.environ.get("INGEST_API_KEY", "")
STALE_SECONDS    = _env_int("STALE_SECONDS", 300)    # discard readings older than this
# ------------------------------------------------------------------------------

CHAR_MEASUREMENT  = "59DA0001-12F4-25A6-7D4F-55961DCE4205"
CHAR_TIME         = "59DA0004-12F4-25A6-7D4F-55961DCE4205"
CHAR_SERIAL       = "59DA0010-12F4-25A6-7D4F-55961DCE4205"
CHAR_PAIRING_CODE = "59DA0011-12F4-25A6-7D4F-55961DCE4205"
CHAR_MILLIS_PULSE = "59DA0012-12F4-25A6-7D4F-55961DCE4205"
CHAR_BATCH_SIZE   = "59DA0013-12F4-25A6-7D4F-55961DCE4205"
CHAR_TX_POWER     = "59DA0014-12F4-25A6-7D4F-55961DCE4205"
CHAR_FIRMWARE     = "00002A26-0000-1000-8000-00805F9B34FB"
CHAR_BATTERY      = "00002A19-0000-1000-8000-00805F9B34FB"
CHAR_PULSE        = "59DA0003-12F4-25A6-7D4F-55961DCE4205"

# -- Struct helpers ------------------------------------------------------------
def enc_u32(v):  return struct.pack("<I", v)
def dec_u8(d):   return struct.unpack("<B", d[:1])[0]
def dec_u16(d):  return struct.unpack("<H", d[:2])[0]
def dec_u32(d):  return struct.unpack("<I", d[:4])[0]
def dec_i8(d):   return struct.unpack("<b", d[:1])[0]

def setup_logger():
    logger = logging.getLogger("my_logger")
    logger.setLevel(logging.INFO)
    handler = TimedRotatingFileHandler(
        "powerpal-bridge.log", when="midnight", backupCount=7, encoding="utf-8"
    )
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    return logger

logger = setup_logger()

if not INGEST_API_KEY:
    logger.warning("INGEST_API_KEY not set -- readings will only be shown, not posted.")

# -- Ingest POST ----------------------------------------------------------------
_outbox  = []
_last_ts = 0

def post_reading(payload):
    """POST one reading to the ingest API. Returns True on success."""
    req = urllib.request.Request(
        INGEST_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": INGEST_API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = 200 <= resp.status < 300
            if not ok:
                logger.error("[INGEST] HTTP %s", resp.status)
            return ok
    except urllib.error.HTTPError as e:
        logger.error("[INGEST] HTTP error %s: %s", e.code, e.reason)
        return False
    except urllib.error.URLError as e:
        logger.error("[INGEST] server unreachable: %s", e.reason)
        return False
    except Exception as e:
        logger.error("[INGEST] unexpected error: %s", e)
        return False

def send_reading(payload):
    """Post one reading; on failure, queue it for retry after the next one."""
    if post_reading(payload):
        while _outbox:
            if not post_reading(_outbox[0]):
                break
            _outbox.pop(0)
        return
    _outbox.append(payload)
    if len(_outbox) > 720:  # ~12 h of minute data, then drop the oldest
        dropped = _outbox.pop(0)
        logger.warning("[INGEST] outbox overflow, dropping reading at %s", dropped["time"])
    logger.warning("[INGEST] queued for retry (%d in outbox)", len(_outbox))

def ms_to_watts(millis):
    if millis <= 0:
        return 0.0
    return (3_600_000 / millis) * (1000 / PULSES_PER_KWH)

def pulses_to_watts(pulses):
    if pulses <= 0:
        return 0.0
    return (pulses / PULSES_PER_KWH) / (INTERVAL_MINUTES / 60) * 1000

# -- Globals -------------------------------------------------------------------
_meas_count  = 0
_pulse_count = 0

# -- Callbacks -----------------------------------------------------------------
def on_measurement(sender, data):
    global _meas_count, _last_ts
    _meas_count += 1
    ts     = dec_u32(data[0:4])
    pulses = dec_u16(data[4:6]) if len(data) >= 6 else 0

    now = int(time.time())
    if ts > now + 120:
        logger.warning("skipped future-dated reading (ts=%d)", ts)
        return
    if now - ts > STALE_SECONDS:
        logger.warning("skipped stale/historic reading (ts=%d, %ds old)", ts, now - ts)
        return
    if _last_ts and ts <= _last_ts:
        logger.warning("skipped duplicate/out-of-order reading (ts=%d)", ts)
        return
    _last_ts = ts

    kwh    = pulses / PULSES_PER_KWH
    watts  = pulses_to_watts(pulses)
    dt     = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    logger.info("#%4d  device_ts=%s  pulses=%5d  %.4f kWh  %8.1f W avg", _meas_count, dt, pulses, kwh, watts)

    if not INGEST_API_KEY:
        return  # console-only mode
    send_reading({
        "time": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "pulses": pulses,
        "kwh": round(kwh, 6),
        "average_watts": round(watts, 3),
    })

def on_pulse(sender, data):
    global _pulse_count
    _pulse_count += 1
    last = getattr(on_pulse, "_last_ts", 0)
    ts = dec_u32(data[0:4])
    if last > 0:
        interval_ms = (ts - last) * 1000
        watts = ms_to_watts(interval_ms) if interval_ms > 0 else 0
        logger.info("[PULSE] #%4d  %8.1f W instantaneous", _pulse_count, watts)
    on_pulse._last_ts = ts

# -- Scan until found ----------------------------------------------------------
async def scan_until_found():
    attempt = 0
    while True:
        attempt += 1
        logger.info("Scan #%d -- looking for %s...", attempt, POWERPAL_MAC)
        device = await BleakScanner.find_device_by_address(POWERPAL_MAC, timeout=8.0)
        if device is not None:
            logger.info("Found: %s (%s)", device.address, device.name)
            return device
        logger.info("Not found -- retrying in %ds...", RETRY_DELAY)
        await asyncio.sleep(RETRY_DELAY)

# -- Single connection session -------------------------------------------------
async def connect_once(device, reconnect_num):
    pair_before = sys.platform == "win32"
    logger.info("Connecting to %s (%s)...", device.address, device.name)

    async with BleakClient(device, timeout=20.0, pair_before_connect=pair_before, dangerous_use_bleak_cache=True) as client:
        if not client.is_connected:
            raise Exception("connect() returned but is_connected is False")

        logger.info("Connected!")

        if reconnect_num == 0:
            firmware = serial = "unknown"
            battery  = -1
            try:
                firmware = (await client.read_gatt_char(CHAR_FIRMWARE)).decode("utf-8").strip()
            except Exception:
                pass
            try:
                battery = dec_u8(await client.read_gatt_char(CHAR_BATTERY))
            except Exception:
                pass
            try:
                serial = dec_u32(await client.read_gatt_char(CHAR_SERIAL))
            except Exception:
                pass
            logger.info("Device Info -- serial=%s firmware=%s battery=%d%%", serial, firmware, battery)

        await client.write_gatt_char(CHAR_PAIRING_CODE, enc_u32(PAIRING_CODE), response=True)
        logger.info("Authenticated")

        now = int(time.time())
        await client.write_gatt_char(CHAR_TIME, enc_u32(now), response=True)
        logger.info("Time synced: %s", datetime.fromtimestamp(now).strftime("%H:%M:%S"))

        await client.write_gatt_char(CHAR_BATCH_SIZE, enc_u32(INTERVAL_MINUTES), response=True)
        logger.info("Interval: %d min", INTERVAL_MINUTES)

        try:
            ms = dec_u32(await client.read_gatt_char(CHAR_MILLIS_PULSE))
            logger.info("Instantaneous: %.1f W", ms_to_watts(ms))
        except Exception:
            pass

        await client.start_notify(CHAR_MEASUREMENT, on_measurement)

        if LIVE_PULSE:
            try:
                await client.start_notify(CHAR_PULSE, on_pulse)
                logger.info("Per-pulse notifications active")
            except Exception as e:
                logger.error("Pulse subscribe failed: %s", e)

        logger.info("Streaming (every %d min -- Ctrl+C to stop)", INTERVAL_MINUTES)

        while client.is_connected:
            await asyncio.sleep(1)

        logger.info("Device dropped connection (normal) -- reconnecting in %ds...", RETRY_DELAY)

# -- Main loop -----------------------------------------------------------------
async def main():
    logger.info("Powerpal BLE Client (Standalone)")
    logger.info("  MAC            : %s", POWERPAL_MAC)
    logger.info("  Pairing code   : %d", PAIRING_CODE)
    logger.info("  Pulses/kWh     : %d", PULSES_PER_KWH)
    logger.info("  Interval       : %d min", INTERVAL_MINUTES)
    logger.info("  Ingest         : %s", INGEST_URL if INGEST_API_KEY else "disabled (no INGEST_API_KEY)")
    logger.info("  Platform       : %s", "Windows" if sys.platform == "win32" else "Linux")

    reconnect_num = 0
    while True:
        try:
            device = await scan_until_found()
            await connect_once(device, reconnect_num)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception("Error: %s", e)

        reconnect_num += 1
        logger.info("Reconnect attempt #%d in %ds...", reconnect_num, RETRY_DELAY)
        await asyncio.sleep(RETRY_DELAY)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Exited. Total measurements: %d", _meas_count)
