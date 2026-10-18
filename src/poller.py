"""TTC GTFS-Realtime poller.

Every POLL_SECONDS it fetches the vehicle-positions and trip-updates feeds,
flattens them into rows, and every FLUSH_SECONDS writes the buffered rows to
Parquet under data/raw/<feed>/date=YYYY-MM-DD/part-HHMMSS.parquet.

Once a day it also checks the static GTFS timetable and downloads a fresh copy
if the current one is older than STATIC_MAX_AGE_DAYS.
"""

import logging
import os
import signal
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from google.transit import gtfs_realtime_pb2
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------- settings (override with environment variables) ----------
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
FLUSH_SECONDS = int(os.getenv("FLUSH_SECONDS", "300"))
STATIC_MAX_AGE_DAYS = int(os.getenv("STATIC_MAX_AGE_DAYS", "7"))

FEEDS = {
    "vehicles": os.getenv("VEHICLES_URL", "https://bustime.ttc.ca/gtfsrt/vehicles"),
    "trip_updates": os.getenv("TRIPS_URL", "https://bustime.ttc.ca/gtfsrt/trips"),
}
STATIC_URL = os.getenv(
    "STATIC_URL",
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/bd4809dd-e289-4de8-bbde-c5c00dafbf4f"
    "/resource/28514055-d011-4ed7-8bb0-97961dfe2b66/download/SurfaceGTFS.zip",
)
LOCAL_TZ = ZoneInfo("America/Toronto")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("poller")


# ---------- HTTP with automatic retries ----------
def make_session() -> requests.Session:
    retry = Retry(
        total=5,
        backoff_factor=2,  # waits 2s, 4s, 8s, 16s ...
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = "ttc-bus-reliability (portfolio project)"
    return session


# ---------- parsing ----------
def parse_vehicles(feed: gtfs_realtime_pb2.FeedMessage, fetched_at: datetime) -> list[dict]:
    rows = []
    for e in feed.entity:
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        rows.append(
            {
                "fetched_at": fetched_at,
                "feed_timestamp": feed.header.timestamp,
                "vehicle_id": v.vehicle.id,
                "trip_id": v.trip.trip_id,
                "route_id": v.trip.route_id,
                "direction_id": v.trip.direction_id if v.trip.HasField("direction_id") else None,
                "lat": v.position.latitude,
                "lon": v.position.longitude,
                "bearing": v.position.bearing if v.position.HasField("bearing") else None,
                "speed": v.position.speed if v.position.HasField("speed") else None,
                "stop_id": v.stop_id,
                "current_stop_sequence": v.current_stop_sequence if v.HasField("current_stop_sequence") else None,
                "current_status": (
                    gtfs_realtime_pb2.VehiclePosition.VehicleStopStatus.Name(v.current_status)
                    if v.HasField("current_status")
                    else None
                ),
                "vehicle_timestamp": v.timestamp,
            }
        )
    return rows


def parse_trip_updates(feed: gtfs_realtime_pb2.FeedMessage, fetched_at: datetime) -> list[dict]:
    rows = []
    for e in feed.entity:
        if not e.HasField("trip_update"):
            continue
        tu = e.trip_update
        for stu in tu.stop_time_update:
            rows.append(
                {
                    "fetched_at": fetched_at,
                    "feed_timestamp": feed.header.timestamp,
                    "trip_id": tu.trip.trip_id,
                    "route_id": tu.trip.route_id,
                    "vehicle_id": tu.vehicle.id,
                    "stop_id": stu.stop_id,
                    "stop_sequence": stu.stop_sequence if stu.HasField("stop_sequence") else None,
                    "arrival_time": stu.arrival.time if stu.HasField("arrival") and stu.arrival.HasField("time") else None,
                    "arrival_delay": stu.arrival.delay if stu.HasField("arrival") and stu.arrival.HasField("delay") else None,
                    "departure_time": stu.departure.time if stu.HasField("departure") and stu.departure.HasField("time") else None,
                }
            )
    return rows


PARSERS = {"vehicles": parse_vehicles, "trip_updates": parse_trip_updates}


# ---------- writing ----------
def flush(buffers: dict[str, list[dict]]) -> None:
    now_local = datetime.now(LOCAL_TZ)
    for name, rows in buffers.items():
        if not rows:
            continue
        out_dir = DATA_DIR / "raw" / name / f"date={now_local:%Y-%m-%d}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"part-{now_local:%H%M%S}.parquet"
        pd.DataFrame(rows).to_parquet(out_file, index=False)
        log.info("wrote %s rows -> %s", len(rows), out_file)
        rows.clear()


# ---------- static timetable ----------
def refresh_static(session: requests.Session) -> None:
    static_dir = DATA_DIR / "static"
    marker = static_dir / "downloaded_at.txt"
    if marker.exists():
        age_days = (time.time() - marker.stat().st_mtime) / 86400
        if age_days < STATIC_MAX_AGE_DAYS:
            return
    log.info("downloading static GTFS")
    static_dir.mkdir(parents=True, exist_ok=True)
    zip_path = static_dir / "ttc_gtfs.zip"
    resp = session.get(STATIC_URL, timeout=120)
    resp.raise_for_status()
    zip_path.write_bytes(resp.content)
    # keep a dated copy so old analyses can be reproduced
    archive = static_dir / "archive"
    archive.mkdir(exist_ok=True)
    (archive / f"ttc_gtfs_{datetime.now(LOCAL_TZ):%Y%m%d}.zip").write_bytes(resp.content)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(static_dir)
    marker.write_text(datetime.now(timezone.utc).isoformat())
    log.info("static GTFS updated")


# ---------- main loop ----------
running = True


def stop(signum, frame):  # called when Docker stops the container
    global running
    log.info("received signal %s, shutting down", signum)
    running = False


def main() -> None:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    session = make_session()
    buffers = {name: [] for name in FEEDS}
    last_flush = time.monotonic()
    last_static_check = 0.0

    log.info("poller started: every %ss, flush every %ss", POLL_SECONDS, FLUSH_SECONDS)
    while running:
        started = time.monotonic()

        if started - last_static_check > 86400:
            try:
                refresh_static(session)
            except Exception:
                log.exception("static GTFS refresh failed, will retry tomorrow")
            last_static_check = started

        fetched_at = datetime.now(timezone.utc)
        for name, url in FEEDS.items():
            try:
                resp = session.get(url, timeout=15)
                resp.raise_for_status()
                feed = gtfs_realtime_pb2.FeedMessage()
                feed.ParseFromString(resp.content)
                rows = PARSERS[name](feed, fetched_at)
                buffers[name].extend(rows)
                log.info("%s: %s rows", name, len(rows))
            except Exception as exc:  # never let one bad fetch kill the poller
                log.warning("%s fetch failed: %s", name, exc)

        if time.monotonic() - last_flush >= FLUSH_SECONDS:
            flush(buffers)
            last_flush = time.monotonic()

        # sleep until next poll, waking up early if asked to stop
        while running and time.monotonic() - started < POLL_SECONDS:
            time.sleep(1)

    flush(buffers)  # save whatever is still in memory
    log.info("poller stopped")


if __name__ == "__main__":
    main()
