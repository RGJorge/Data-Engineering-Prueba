"""Load tracks from all stations and all firmwares into `track_points`.

Memory-bounded design (scales to the full 1M-file dataset):
  1. Parse every row (3 firmware dialects), normalise it (UTC time, MSL altitude,
     lowercase class) and *stream* it into an on-disk staging table in batches.
     Unparseable rows and rows outside 2025 are rejected here (facts 1).
  2. Deduplicate in SQL via ROW_NUMBER over (station, track, timestamp) -- fact 6,
     incl. *_resend files. Nothing is held in Python for this.
  3. Speed filter (fact 2): read the deduped points back ordered by
     (station, track, time) and process ONE track at a time through a streaming
     cursor, dropping points implying >150 m/s from the last accepted point.

Only a single track's points live in RAM at any moment, so memory stays flat no
matter how large a station is. Rows read = duplicates + rejected + valid.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterator, List, Optional

from .common import config, context, schema
from .common.geo import implied_speed_ms
from .common.timeparse import from_epoch, from_iso, in_year

# Firmware dialects, keyed by the exact header column set.
FIRMWARES = {
    frozenset(["timestamp", "track_id", "lat", "lon", "alt", "type", "operator"]): dict(
        delim=",", ts="timestamp", ts_kind="epoch", ts_unit="s", tid="track_id",
        lat="lat", lon="lon", alt="alt", alt_ref="agl", cls="type",
        op="operator", decimal_comma=False, name="A",
    ),
    frozenset(["ts_ms", "track", "latitude", "longitude", "alt_msl", "class_id", "operator_email"]): dict(
        delim=",", ts="ts_ms", ts_kind="epoch", ts_unit="ms", tid="track",
        lat="latitude", lon="longitude", alt="alt_msl", alt_ref="msl", cls="class_id",
        op="operator_email", decimal_comma=False, name="C",
    ),
    frozenset(["time", "object_id", "lat", "lon", "alt_m", "class", "operator"]): dict(
        delim=";", ts="time", ts_kind="iso", ts_unit=None, tid="object_id",
        lat="lat", lon="lon", alt="alt_m", alt_ref="agl", cls="class",
        op="operator", decimal_comma=True, name="B",
    ),
}

BATCH = 100_000

STG_COLS = ["station_id", "track_id", "ts_utc", "ts_epoch", "lat", "lon",
            "alt_msl_m", "class", "operator", "source_file"]
REJ_COLS = ["source_type", "station_id", "location", "reason", "detail"]
TP_COLS = ["station_id", "track_id", "ts_utc", "lat", "lon", "alt_msl_m",
           "class", "operator", "source_file"]


def _to_float(raw: Optional[str], decimal_comma: bool) -> Optional[float]:
    if raw is None:
        return None
    s = raw.strip()
    if s == "":
        return None
    if decimal_comma:
        s = s.replace(",", ".")
    return float(s)


def _detect_firmware(header: List[str]) -> Optional[dict]:
    return FIRMWARES.get(frozenset(h.strip() for h in header))


def _iter_rows(path: Path) -> Iterator[dict]:
    """Yield one dict per data row, tagged with the firmware spec."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        first = f.readline()
        if not first:
            return
        delim = ";" if ";" in first and "," not in first.split(";")[0] else ","
        header = next(csv.reader([first], delimiter=delim))
        spec = _detect_firmware(header)
        reader = csv.DictReader(f, fieldnames=[h.strip() for h in header], delimiter=delim)
        for row in reader:
            yield {"spec": spec, "row": row}


def load(con) -> None:
    stations = context.load_stations()
    class_codes = context.load_class_codes()

    con.execute(
        "CREATE OR REPLACE TABLE stg_points ("
        "station_id VARCHAR, track_id VARCHAR, ts_utc TIMESTAMP, ts_epoch DOUBLE, "
        "lat DOUBLE, lon DOUBLE, alt_msl_m DOUBLE, class VARCHAR, operator VARCHAR, source_file VARCHAR)")

    stg_buf: List[tuple] = []
    rej_buf: List[tuple] = []
    n_read = 0

    def flush_stg():
        schema.bulk_insert(con, "stg_points", STG_COLS, stg_buf)
        stg_buf.clear()

    def flush_rej():
        schema.bulk_insert(con, "rejects", REJ_COLS, rej_buf)
        rej_buf.clear()

    # --- Stage 1: parse + basic validation, stream into staging ------------
    for station_dir in sorted(config.DATA_DIR.glob("S*")):
        station_id = station_dir.name
        tracks_dir = station_dir / "tracks"
        if not tracks_dir.is_dir():
            continue
        ground = stations.get(station_id, {}).get("ground_elevation_m", 0.0)

        for csv_path in sorted(tracks_dir.glob("*.csv")):
            rel = str(csv_path.relative_to(config.DATA_DIR))
            for item in _iter_rows(csv_path):
                spec, row = item["spec"], item["row"]
                n_read += 1
                if spec is None:
                    rej_buf.append(("track", station_id, rel, "unknown_firmware", "header not recognised"))
                    if len(rej_buf) >= BATCH:
                        flush_rej()
                    continue
                try:
                    tid = (row.get(spec["tid"]) or "").strip()
                    raw_ts = row.get(spec["ts"])
                    dt = from_epoch(raw_ts, spec["ts_unit"]) if spec["ts_kind"] == "epoch" else from_iso(raw_ts)
                    lat = _to_float(row.get(spec["lat"]), spec["decimal_comma"])
                    lon = _to_float(row.get(spec["lon"]), spec["decimal_comma"])
                    alt_raw = _to_float(row.get(spec["alt"]), spec["decimal_comma"])
                except (ValueError, TypeError, KeyError) as exc:
                    rej_buf.append(("track", station_id, rel, "unparseable", str(exc)[:200]))
                    if len(rej_buf) >= BATCH:
                        flush_rej()
                    continue

                if not tid or lat is None or lon is None:
                    rej_buf.append(("track", station_id, rel, "unparseable", "missing id/lat/lon"))
                    if len(rej_buf) >= BATCH:
                        flush_rej()
                    continue
                if not in_year(dt, config.YEAR):
                    rej_buf.append(("track", station_id, rel, "out_of_2025", dt.isoformat()))
                    if len(rej_buf) >= BATCH:
                        flush_rej()
                    continue

                # Fact 4/5: store altitude as MSL; AGL firmwares add ground elevation.
                if alt_raw is None:
                    alt_msl = None
                elif spec["alt_ref"] == "agl":
                    alt_msl = alt_raw + ground
                else:
                    alt_msl = alt_raw

                stg_buf.append((
                    station_id, tid, dt.replace(tzinfo=None), dt.timestamp(),
                    lat, lon, alt_msl,
                    context.normalise_class(row.get(spec["cls"]), class_codes),
                    (row.get(spec["op"]) or "").strip() or None, rel))
                if len(stg_buf) >= BATCH:
                    flush_stg()
    flush_stg()
    flush_rej()

    # --- Stage 2: dedup in SQL (fact 6) ------------------------------------
    con.execute(
        "CREATE OR REPLACE TABLE stg_ranked AS SELECT *, "
        "ROW_NUMBER() OVER (PARTITION BY station_id, track_id, ts_utc ORDER BY source_file) AS rn "
        "FROM stg_points")
    n_dup = con.execute("SELECT COUNT(*) FROM stg_ranked WHERE rn > 1").fetchone()[0]
    con.execute(
        "INSERT INTO rejects (source_type, station_id, location, reason, detail) "
        "SELECT 'track', station_id, source_file, 'duplicate', track_id FROM stg_ranked WHERE rn > 1")

    # --- Stage 3: speed filter, streaming one track at a time (fact 2) ------
    out_buf: List[tuple] = []

    def flush_out():
        schema.bulk_insert(con, "track_points", TP_COLS, out_buf)
        out_buf.clear()

    def process_track(pts: List[tuple]):
        last = None
        for p in pts:  # p ordered by ts_epoch within the track
            if last is not None:
                v = implied_speed_ms(last[4], last[5], last[3], p[4], p[5], p[3])
                if v > config.MAX_SPEED_MS:
                    rej_buf.append(("track", p[0], p[9], "speed_exceeded", f"{v:.1f} m/s"))
                    continue
            last = p
            out_buf.append((p[0], p[1], p[2], p[4], p[5], p[6], p[7], p[8], p[9]))

    cur = con.cursor()
    cur.execute(
        "SELECT station_id, track_id, ts_utc, ts_epoch, lat, lon, alt_msl_m, class, operator, source_file "
        "FROM stg_ranked WHERE rn = 1 ORDER BY station_id, track_id, ts_epoch")
    cur_key = None
    pts: List[tuple] = []
    while True:
        rows = cur.fetchmany(BATCH)
        if not rows:
            break
        for r in rows:
            key = (r[0], r[1])
            if key != cur_key:
                if pts:
                    process_track(pts)
                    pts = []
                cur_key = key
            pts.append(r)
        if len(out_buf) >= BATCH:
            flush_out()
        if len(rej_buf) >= BATCH:
            flush_rej()
    if pts:
        process_track(pts)
    flush_out()
    flush_rej()
    cur.close()

    con.execute("DROP TABLE stg_ranked")
    con.execute("DROP TABLE stg_points")
    schema.bump(con, "tracks_n_rows_read", n_read)
    schema.bump(con, "tracks_n_rows_duplicate", n_dup)
    print(f"[tracks] rows_read={n_read} duplicate={n_dup} "
          f"valid={con.execute('SELECT COUNT(*) FROM track_points').fetchone()[0]}")
