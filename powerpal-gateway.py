#!/usr/bin/env python3
"""powerpal-gateway.py — PowerPal BLE bridge for home-energy-server.

Connects to a Powerpal energy monitor over BLE, authenticates with the
device's pairing code, subscribes to measurement notifications, converts
pulses to kWh + average watts, and posts one reading per notification to
the home-energy-server ingest API.

Each measurement notification carries a unix timestamp plus the pulses
counted during the batch window (1-60 minutes, configurable on the
device and via the Powerpal app). With the default batch window of one
minute the bridge posts exactly one reading per minute:

    POST {INGEST_URL}
    X-API-Key: <INGEST_API_KEY>
    {"time": "2026-10-12T11:00:00+00:00", "pulses": 12,
     "kwh": 0.012, "average_watts": 230.5}

Posting is idempotent server-side (readings are keyed by time), so
retries after network hiccups never create duplicates.

BLE protocol reference: https://github.com/WeekendWarrior1/powerpal_ble
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import struct
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bleak import BleakClient, BleakScanner

# --- PowerPal GATT UUIDs (WeekendWarrior1/powerpal_ble) --------------------
UUID_MEASUREMENT = "59da0001-12f4-25a6-7d4f-55961dce4205"  # notify: ts + pulses
UUID_PAIRING_CODE = "59da0011-12f4-25a6-7d4f-55961dce4205"  # write: auth
UUID_READING_BATCH_SIZE = "59da0013-12f4-25a6-7d4f-55961dce4205"  # write: window

DEVICE_NAME_PREFIX = "powerpal"  # advertisements commonly expose "Powerpal..."

log = logging.getLogger("powerpal-gateway")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
@dataclass
class Settings:
    address: str | None  # BLE MAC of the Powerpal (None = scan by name)
    pairing_code: int  # 6-digit pairing code as int ("024024" -> 24024)
    pulses_per_kwh: float  # meter constant — must match the Powerpal app
    batch_minutes: int  # measurement batch window requested (1-60)
    server_url: str  # ingest endpoint incl. path
    ingest_api_key: str
    stale_seconds: int  # discard readings older than this
    reconnect_min: float = 5.0
    reconnect_max: float = 60.0

    @classmethod
    def from_env(cls) -> "Settings":
        def env(name: str, default: str | None = None, required: bool = False) -> str:
            value = os.environ.get(name, default)
            if value is None and required:
                raise SystemExit(f"Missing required environment variable: {name}")
            return value

        return cls(
            address=os.environ.get("POWERPAL_ADDRESS") or None,
            pairing_code=int(env("POWERPAL_PAIRING_CODE", required=True)),
            pulses_per_kwh=float(env("PULSES_PER_KWH", "1000")),
            batch_minutes=max(1, min(60, int(env("POWERPAL_BATCH_MINUTES", "1")))),
            server_url=env(
                "INGEST_URL", "http://192.168.68.141:8000/api/v1/readings"
            ).rstrip("/"),
            ingest_api_key=env("INGEST_API_KEY", required=True),
            stale_seconds=int(env("STALE_SECONDS", "300")),
        )


# --------------------------------------------------------------------------
# PowerPal payload helpers
# --------------------------------------------------------------------------
def parse_measurement(data: bytearray) -> tuple[int, int] | None:
    """Return (unix_ts, pulses) from a measurement notification.

    Layout (little-endian, per WeekendWarrior1/powerpal_ble):
      bytes 0-3: unix timestamp of the batch window end
      bytes 4-5: pulses counted during the window
    Returns None if the payload is too short to contain both fields.
    """
    if len(data) < 6:
        log.warning("short measurement notification (%d B): %s", len(data), data.hex())
        return None
    ts, pulses = struct.unpack_from("<IH", data, 0)
    return ts, pulses


def pass_key_bytes(pairing_code: int) -> bytes:
    """Pairing code as uint32 little-endian (the device is picky about order)."""
    return struct.pack("<I", pairing_code)


def batch_size_bytes(minutes: int) -> bytes:
    """Reading-batch window as uint32 little-endian."""
    return struct.pack("<I", minutes)


# --------------------------------------------------------------------------
# Ingest client
# --------------------------------------------------------------------------
@dataclass
class IngestClient:
    server_url: str
    api_key: str
    outbox: list[dict] = field(default_factory=list)

    def _post(self, payload: dict) -> bool:
        req = urllib.request.Request(
            self.server_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-API-Key": self.api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                ok = 200 <= resp.status < 300
                if not ok:
                    log.warning("ingest returned HTTP %s", resp.status)
                return ok
        except urllib.error.HTTPError as exc:
            log.warning("ingest HTTP error %s: %s", exc.code, exc.reason[:120])
            return False
        except urllib.error.URLError as exc:
            log.warning("ingest unreachable: %s", exc.reason)
            return False

    def send(self, payload: dict) -> None:
        """Post one reading; on failure, queue it and retry after the next one."""
        if self._post(payload):
            self.flush_outbox()
            return
        self.outbox.append(payload)
        if len(self.outbox) > 720:  # ~12 h of minute data, then drop oldest
            dropped = self.outbox.pop(0)
            log.warning("outbox overflow, dropping reading at %s", dropped["time"])
        log.info("queued reading for retry (%d in outbox)", len(self.outbox))

    def flush_outbox(self) -> None:
        while self.outbox:
            if not self._post(self.outbox[0]):
                break
            self.outbox.pop(0)
        if not self.outbox:
            log.debug("outbox clear")


# --------------------------------------------------------------------------
# Gateway
# --------------------------------------------------------------------------
class Gateway:
    def __init__(self, settings: Settings):
        self.s = settings
        self.ingest = IngestClient(settings.server_url, settings.ingest_api_key)
        self.last_ts: int | None = None
        self.stop = asyncio.Event()

    # -- BLE callbacks / parsing -------------------------------------------
    def handle_measurement(self, _sender: int, data: bytearray) -> None:
        parsed = parse_measurement(data)
        if parsed is None:
            return
        ts, pulses = parsed
        now = int(time.time())

        if ts > now + 120:
            log.info("skipping future-dated reading (ts=%d)", ts)
            return
        if now - ts > self.s.stale_seconds:
            log.info(
                "skipping stale/historic reading (ts=%d, %d s old) — the device "
                "replays history when the batch window is reduced",
                ts,
                now - ts,
            )
            return
        if self.last_ts is not None and ts <= self.last_ts:
            log.debug("skipping duplicate/out-of-order reading (ts=%d)", ts)
            return

        interval_s = max(1, ts - self.last_ts) if self.last_ts else self.s.batch_minutes * 60
        kwh = pulses / self.s.pulses_per_kwh
        avg_watts = kwh * 1000.0 * 3600.0 / interval_s
        reading_time = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

        payload = {
            "time": reading_time,
            "pulses": pulses,
            "kwh": round(kwh, 6),
            "average_watts": round(avg_watts, 3),
        }
        self.last_ts = ts
        log.info("reading %s -> %s", reading_time, payload)
        self.ingest.send(payload)

    # -- connection ----------------------------------------------------------
    async def resolve_address(self) -> str:
        if self.s.address:
            return self.s.address
        log.info("scanning for a Powerpal (name starts with %r)...", DEVICE_NAME_PREFIX)
        devices = await BleakScanner.discover(timeout=10.0)
        for device in devices:
            if (device.name or "").lower().startswith(DEVICE_NAME_PREFIX):
                log.info("found Powerpal: %s (%s)", device.name, device.address)
                return device.address
        raise SystemExit(
            "No Powerpal found by name scan. Set POWERPAL_ADDRESS=<mac> "
            "(on the device sticker / nRF Connect) and retry."
        )

    async def run(self) -> None:
        address = await self.resolve_address()
        backoff = self.s.reconnect_min
        while not self.stop.is_set():
            try:
                async with BleakClient(address) as client:
                    log.info("connected to %s", address)
                    await client.write_gatt_char(
                        UUID_PAIRING_CODE, pass_key_bytes(self.s.pairing_code), response=False
                    )
                    log.info("pairing code written")
                    await asyncio.sleep(0.5)  # let the device unlock its characteristics
                    await client.write_gatt_char(
                        UUID_READING_BATCH_SIZE,
                        batch_size_bytes(self.s.batch_minutes),
                        response=False,
                    )
                    log.info("batch window set to %d min", self.s.batch_minutes)
                    await client.start_notify(UUID_MEASUREMENT, self.handle_measurement)
                    log.info("subscribed to measurement notifications")
                    backoff = self.s.reconnect_min
                    await self.stop.wait()
            except Exception as exc:  # noqa: BLE001 — reconnect on any failure
                log.warning("connection lost/failed: %s", exc)
            if self.stop.is_set():
                break
            log.info("reconnecting in %.0f s", backoff)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 2, self.s.reconnect_max)
        log.info("gateway stopped")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
async def scan_only() -> None:
    devices = await BleakScanner.discover(timeout=10.0)
    found = False
    for device in sorted(devices, key=lambda d: (d.name or "").lower()):
        if DEVICE_NAME_PREFIX in (device.name or "").lower():
            print(f"FOUND  {device.address}  {device.name}")
            found = True
    print(f"({len(devices)} BLE device(s) seen in total)")


async def async_main(settings: Settings) -> None:
    gateway = Gateway(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, gateway.stop.set)
    await gateway.run()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PowerPal BLE bridge: post Powerpal readings to home-energy-server"
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="scan for Powerpal devices for 10 s and exit (config still required)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    if args.scan:
        # Scanning is a pre-config step — no env config required.
        asyncio.run(scan_only())
        return

    settings = Settings.from_env()

    log.info(
        "gateway starting: pulses_per_kwh=%s batch=%dmin stale=%ds server=%s",
        settings.pulses_per_kwh,
        settings.batch_minutes,
        settings.stale_seconds,
        settings.server_url,
    )
    try:
        asyncio.run(async_main(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
