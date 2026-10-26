"""Evaluation service: query the warehouse and write /output/summary.json.

Every value is computed from the database (not the raw files). Timestamps are
emitted as UTC ISO-8601 with milliseconds. Exits only after summary.json exists.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from .common import config, context, schema
from .common.timeparse import to_iso_ms


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return to_iso_ms(dt)


def _counter(con, name: str) -> int:
    row = con.execute("SELECT value FROM counters WHERE name = ?", [name]).fetchone()
    return int(row[0]) if row else 0


def _tracks(con) -> dict:
    n_read = _counter(con, "tracks_n_rows_read")
    n_dup = _counter(con, "tracks_n_rows_duplicate")
    n_rejected = con.execute(
        "SELECT COUNT(*) FROM rejects WHERE source_type='track' AND reason <> 'duplicate'"
    ).fetchone()[0]
    n_valid = con.execute("SELECT COUNT(*) FROM track_points").fetchone()[0]
    n_missing_alt = con.execute(
        "SELECT COUNT(*) FROM track_points WHERE alt_msl_m IS NULL").fetchone()[0]
    n_tracks = con.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT station_id, track_id FROM track_points)"
    ).fetchone()[0]

    # One class per track (assumed uniform); then tracks per class.
    per_class = con.execute(
        "SELECT class, COUNT(*) FROM ("
        "  SELECT station_id, track_id, MIN(class) AS class FROM track_points "
        "  GROUP BY station_id, track_id) GROUP BY class ORDER BY class"
    ).fetchall()
    max_alt = con.execute("SELECT MAX(alt_msl_m) FROM track_points").fetchone()[0]

    per_station = {}
    for sid, nt, npv, fp, lp in con.execute(
        "SELECT station_id, COUNT(DISTINCT track_id), COUNT(*), MIN(ts_utc), MAX(ts_utc) "
        "FROM track_points GROUP BY station_id ORDER BY station_id"
    ).fetchall():
        per_station[sid] = {
            "n_tracks": nt, "n_points_valid": npv,
            "first_point_utc": _iso(fp), "last_point_utc": _iso(lp),
        }

    sample = []
    for sid, tid in context.load_sample_tracks():
        row = con.execute(
            "SELECT COUNT(*), MIN(ts_utc), MAX(ts_utc) FROM track_points "
            "WHERE station_id = ? AND track_id = ?", [sid, tid]).fetchone()
        sample.append({
            "station_id": sid, "track_id": tid, "n_points_valid": row[0],
            "first_point_utc": _iso(row[1]), "last_point_utc": _iso(row[2]),
        })

    return {
        "n_rows_read": n_read,
        "n_rows_duplicate": n_dup,
        "n_rows_rejected": n_rejected,
        "n_points_valid": n_valid,
        "n_points_missing_altitude": n_missing_alt,
        "n_tracks": n_tracks,
        "tracks_per_class": {c: n for c, n in per_class},
        "max_altitude_msl_m": round(max_alt, 1) if max_alt is not None else None,
        "per_station": per_station,
        "sample_tracks": sample,
        "_invariant_ok": n_read == n_dup + n_rejected + n_valid,
    }


def _images(con) -> dict:
    one = lambda q: con.execute(q).fetchone()[0]
    per_class = con.execute(
        "SELECT class, COUNT(*) FROM image_detections WHERE is_valid "
        "GROUP BY class ORDER BY class").fetchall()
    return {
        "n_image_files": one("SELECT COUNT(*) FROM images"),
        "n_images_valid": one("SELECT COUNT(*) FROM images WHERE is_valid"),
        "n_images_corrupted": one("SELECT COUNT(*) FROM images WHERE NOT is_valid"),
        "n_images_without_metadata": one(
            "SELECT COUNT(*) FROM images WHERE is_valid AND NOT has_metadata"),
        "n_metadata_records": one("SELECT COUNT(*) FROM image_meta_records"),
        "n_metadata_lines_malformed": one(
            "SELECT COUNT(*) FROM rejects WHERE source_type='metadata_line' AND reason='malformed'"),
        "n_metadata_records_missing_image": one(
            "SELECT COUNT(*) FROM image_meta_records WHERE NOT image_exists"),
        "n_faces": one("SELECT COALESCE(SUM(n_faces),0) FROM image_meta_records WHERE image_valid"),
        "n_detections_valid": one("SELECT COUNT(*) FROM image_detections WHERE is_valid"),
        "n_detections_rejected": one("SELECT COUNT(*) FROM image_detections WHERE NOT is_valid"),
        "detections_per_class": {c: n for c, n in per_class},
    }


def _clips(con) -> dict:
    one = lambda q: con.execute(q).fetchone()[0]
    total = con.execute("SELECT COALESCE(SUM(duration_s),0) FROM clips").fetchone()[0]
    return {
        "n_clips": one("SELECT COUNT(*) FROM clips"),
        "total_duration_s": round(total, 1),
        "n_labels_valid": one("SELECT COUNT(*) FROM clip_labels WHERE is_valid"),
        "n_labels_rejected": one("SELECT COUNT(*) FROM clip_labels WHERE NOT is_valid"),
    }


def main() -> None:
    con = schema.connect(fresh=False)
    try:
        summary = {"tracks": _tracks(con), "images": _images(con), "clips": _clips(con)}
    finally:
        con.close()

    inv = summary["tracks"].pop("_invariant_ok")
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.SUMMARY_PATH, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"[evaluate] wrote {config.SUMMARY_PATH}")
    print(f"[evaluate] invariant rows_read == dup+rejected+valid: {'OK' if inv else 'MISMATCH'}")


if __name__ == "__main__":
    main()
