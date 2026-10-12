#!/usr/bin/env python3
"""
Powerpal BLE Client -- Standalone, Cross-platform (Windows/Linux)
Protocol reference: https://github.com/WeekendWarrior1/powerpal_ble

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
import struct
import time
import sys
from datetime import datetime
from bleak import BleakScanner, BleakClient

# -- Config -- edit these ------------------------------------------------------
POWERPAL_MAC     = "D8:3F:AE:A6:FD:45"
PAIRING_CODE     = 411758
PULSES_PER_KWH   = 3200
INTERVAL_MINUTES = 1      # measurement batch size in minutes (1-15)
LIVE_PULSE       = False  # True = per-pulse instantaneous watts (uses more battery)
RETRY_DELAY      = 15     # seconds between reconnect attempts
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
    global _meas_count
    _meas_count += 1
    ts     = dec_u32(data[0:4])
    pulses = dec_u16(data[4:6]) if len(data) >= 6 else 0
    kwh    = pulses / PULSES_PER_KWH
    watts  = pulses_to_watts(pulses)
    dt     = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    now    = datetime.now().strftime("%H:%M:%S")
    print("  [%s] #%4d  device_ts=%s  pulses=%5d  %.4f kWh  %8.1f W avg" % (now, _meas_count, dt, pulses, kwh, watts))

def on_pulse(sender, data):
    global _pulse_count
    _pulse_count += 1
    last = getattr(on_pulse, "_last_ts", 0)
    ts = dec_u32(data[0:4])
    now = datetime.now().strftime("%H:%M:%S")
    if last > 0:
        interval_ms = (ts - last) * 1000
        watts = ms_to_watts(interval_ms) if interval_ms > 0 else 0
        print("  [PULSE %s] #%4d  %8.1f W instantaneous" % (now, _pulse_count, watts))
    on_pulse._last_ts = ts

# -- Scan until found ----------------------------------------------------------
async def scan_until_found():
    attempt = 0
    while True:
        attempt += 1
        print("Scan #%d -- looking for %s..." % (attempt, POWERPAL_MAC))
        device = await BleakScanner.find_device_by_address(POWERPAL_MAC, timeout=8.0)
        if device is not None:
            print("Found: %s (%s)" % (device.address, device.name))
            print()
            return device
        print("   Not found -- retrying in %ds..." % RETRY_DELAY)
        await asyncio.sleep(RETRY_DELAY)

# -- Single connection session -------------------------------------------------
async def connect_once(device, reconnect_num):
    pair_before = sys.platform == "win32"
    print("Connecting to %s (%s)..." % (device.address, device.name))

    async with BleakClient(device, timeout=20.0, pair_before_connect=pair_before, dangerous_use_bleak_cache=True) as client:
        if not client.is_connected:
            raise Exception("connect() returned but is_connected is False")

        print("Connected!")
        print()

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
            print("Device Info")
            print("   Serial   : %s" % serial)
            print("   Firmware : %s" % firmware)
            print("   Battery  : %d%%" % battery)
            print()

        await client.write_gatt_char(CHAR_PAIRING_CODE, enc_u32(PAIRING_CODE), response=True)
        print("Authenticated")

        now = int(time.time())
        await client.write_gatt_char(CHAR_TIME, enc_u32(now), response=True)
        print("Time synced: %s" % datetime.fromtimestamp(now).strftime("%H:%M:%S"))

        await client.write_gatt_char(CHAR_BATCH_SIZE, enc_u32(INTERVAL_MINUTES), response=True)
        print("Interval: %d min" % INTERVAL_MINUTES)

        try:
            ms = dec_u32(await client.read_gatt_char(CHAR_MILLIS_PULSE))
            print("Instantaneous: %.1f W" % ms_to_watts(ms))
        except Exception:
            pass
        print()

        await client.start_notify(CHAR_MEASUREMENT, on_measurement)

        if LIVE_PULSE:
            try:
                await client.start_notify(CHAR_PULSE, on_pulse)
                print("Per-pulse notifications active")
            except Exception as e:
                print("Pulse subscribe failed: %s" % e)

        print("Streaming (every %d min -- Ctrl+C to stop):" % INTERVAL_MINUTES)
        print("   %10s  %5s  %-22s  %6s  %8s  %10s" % ("Time", "#", "Device TS", "Pulses", "kWh", "Watts avg"))
        print("   " + "-" * 72)

        while client.is_connected:
            await asyncio.sleep(1)

        print()
        print("Device dropped connection (normal) -- reconnecting in %ds..." % RETRY_DELAY)

# -- Main loop -----------------------------------------------------------------
async def main():
    print("=" * 55)
    print("  Powerpal BLE Client  (Standalone)")
    print("=" * 55)
    print("  MAC            : %s" % POWERPAL_MAC)
    print("  Pairing code   : %d" % PAIRING_CODE)
    print("  Pulses/kWh     : %d" % PULSES_PER_KWH)
    print("  Interval       : %d min" % INTERVAL_MINUTES)
    print("  Platform       : %s" % ("Windows" if sys.platform == "win32" else "Linux"))
    print()

    reconnect_num = 0
    while True:
        try:
            device = await scan_until_found()
            await connect_once(device, reconnect_num)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print("Error: %s" % e)

        reconnect_num += 1
        print("Reconnect attempt #%d in %ds..." % (reconnect_num, RETRY_DELAY))
        await asyncio.sleep(RETRY_DELAY)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        print("Exited. Total measurements: %d" % _meas_count)
