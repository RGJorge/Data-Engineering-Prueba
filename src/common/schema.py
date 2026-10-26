"""Database schema and connection helper (DuckDB).

Design intent
-------------
The three raw sources arrive in three firmware dialects each. Everything is
*flattened* on ingest into a small set of uniform tables so that, from here on,
any station / day / firmware is queried the same way. One extra `rejects` table
makes every discarded row traceable (what, where, why), and `counters` holds the
few totals that are not naturally one-row-per-thing (e.g. raw rows read).

Each table carries `station_id` and `source_file` so results stay auditable and
can be sliced per station and per day without reopening the raw files.
"""
from __future__ import annotations

from typing import List, Sequence

import duckdb
import pandas as pd

from . import config

# --- DDL -------------------------------------------------------------------

DDL = """
-- Valid track points (one row per accepted observation, already deduplicated).
CREATE TABLE track_points (
    station_id   VARCHAR   NOT NULL,
    track_id     VARCHAR   NOT NULL,   -- stored as delivered; unique within a station (fact 3)
    ts_utc       TIMESTAMP NOT NULL,   -- normalised to UTC, millisecond precision (fact 1)
    lat          DOUBLE,
    lon          DOUBLE,
    alt_msl_m    DOUBLE,               -- always above mean sea level; NULL = unknown (facts 4 & 5)
    class        VARCHAR,              -- lowercase class name
    operator     VARCHAR,
    source_file  VARCHAR
);

-- Short video clips (stable format across firmwares).
CREATE TABLE clips (
    station_id   VARCHAR NOT NULL,
    clip_file    VARCHAR,
    start_utc    TIMESTAMP,
    fps          DOUBLE,
    frames       BIGINT,
    duration_s   DOUBLE,               -- frames / fps
    source_file  VARCHAR
);

-- Labelled objects inside clips.
CREATE TABLE clip_labels (
    station_id   VARCHAR,
    clip_file    VARCHAR,
    frame        BIGINT,
    object_id    VARCHAR,
    is_valid     BOOLEAN,              -- falls within its clip (frame range + box in frame)
    source_file  VARCHAR
);

-- Every candidate image file found under images/ (valid or not).
CREATE TABLE images (
    station_id   VARCHAR,
    rel_path     VARCHAR,              -- path relative to DATA_DIR (preserved on output)
    filename     VARCHAR,              -- basename, used to join detection records
    is_valid     BOOLEAN,              -- complete, readable image
    width        INTEGER,
    height       INTEGER,
    has_metadata BOOLEAN               -- at least one detection record points at it
);

-- One row per readable detection (metadata) record = one image's detections.
CREATE TABLE image_meta_records (
    station_id   VARCHAR,
    filename     VARCHAR,              -- image the record refers to (resolved basename)
    image_exists BOOLEAN,
    image_valid  BOOLEAN,
    n_faces      INTEGER,
    source_file  VARCHAR
);

-- Individual detections, only for records whose image is a valid image.
CREATE TABLE image_detections (
    station_id   VARCHAR,
    filename     VARCHAR,
    class        VARCHAR,              -- lowercase
    is_valid     BOOLEAN,              -- inside image (partially outside still counts as valid)
    source_file  VARCHAR
);

-- Universal traceability: anything rejected, anywhere, with a reason.
CREATE TABLE rejects (
    source_type  VARCHAR,             -- 'track' | 'metadata_line' | 'detection' | 'clip_label' | 'image'
    station_id   VARCHAR,
    location     VARCHAR,             -- source file (and line/record when known)
    reason       VARCHAR,             -- machine-friendly: 'duplicate','out_of_2025','speed_exceeded',...
    detail       VARCHAR              -- human-readable context
);

-- Totals that are not one-row-per-entity (kept so invariants can be asserted).
CREATE TABLE counters (
    name   VARCHAR,
    value  BIGINT
);
"""


def connect(fresh: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the warehouse. With ``fresh=True`` the file is recreated from scratch
    so the pipeline is idempotent: re-running yields the same result."""
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if fresh and config.DB_PATH.exists():
        config.DB_PATH.unlink()
    con = duckdb.connect(str(config.DB_PATH))
    if fresh:
        con.execute(DDL)
    return con


def bulk_insert(
    con: duckdb.DuckDBPyConnection,
    table: str,
    columns: Sequence[str],
    rows: List[tuple],
) -> None:
    """Bulk-load rows via a registered DataFrame.

    DuckDB is columnar: row-by-row ``executemany`` INSERTs are pathologically
    slow (seconds per thousand rows). Handing it a whole DataFrame and letting
    it ``INSERT ... SELECT`` is vectorised and orders of magnitude faster -- the
    difference between minutes and seconds on the sample, and the only way this
    scales to the full dataset.
    """
    if not rows:
        return
    df = pd.DataFrame(rows, columns=list(columns))
    # Use a dedicated cursor so registering the DataFrame never collides with an
    # unfinished result left open on the main connection by a prior SELECT.
    cur = con.cursor()
    try:
        cur.register("_bulk", df)
        col_list = ", ".join(columns)
        cur.execute(f"INSERT INTO {table} ({col_list}) SELECT * FROM _bulk")
        cur.fetchall()  # drain the INSERT result
    finally:
        cur.close()


def bump(con: duckdb.DuckDBPyConnection, name: str, by: int) -> None:
    """Increment a named counter (insert-or-add)."""
    row = con.execute("SELECT value FROM counters WHERE name = ?", [name]).fetchone()
    if row is None:
        con.execute("INSERT INTO counters VALUES (?, ?)", [name, by])
    else:
        con.execute("UPDATE counters SET value = value + ? WHERE name = ?", [by, name])
