"""Loaders for the context/ reference files (stations, class codes, sample tracks)."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List, Tuple

from . import config


def load_stations() -> Dict[str, dict]:
    """station_id -> {latitude, longitude, ground_elevation_m}."""
    path = config.DATA_DIR / "context" / "stations.csv"
    out: Dict[str, dict] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            out[row["station_id"]] = {
                "latitude": float(row["latitude"]),
                "longitude": float(row["longitude"]),
                "ground_elevation_m": float(row["ground_elevation_m"]),
            }
    return out


def load_class_codes() -> Dict[str, str]:
    """Numeric code (as string) -> lowercase class name."""
    path = config.DATA_DIR / "context" / "class_codes.csv"
    out: Dict[str, str] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            out[str(row["code"]).strip()] = row["class"].strip().lower()
    return out


def load_sample_tracks() -> List[Tuple[str, str]]:
    """List of (station_id, track_id) we must report on, in file order."""
    path = config.DATA_DIR / "context" / "sample_tracks.csv"
    out: List[Tuple[str, str]] = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            out.append((row["station_id"].strip(), row["track_id"].strip()))
    return out


def normalise_class(value: str, class_codes: Dict[str, str]) -> str:
    """Return a lowercase class name from either a name or a numeric code."""
    v = str(value).strip()
    if v in class_codes:          # numeric code delivered by firmware C
        return class_codes[v]
    return v.lower()
