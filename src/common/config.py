"""Runtime configuration resolved from the environment.

The pipeline is driven entirely by two environment variables so the same
code runs locally and inside Docker without changes:

    DATA_DIR    input folder (read-only)      -> /data   in Docker
    OUTPUT_DIR  results folder                 -> /output in Docker

Local defaults point at the repo so you can iterate without Docker.
"""
from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = Path(os.environ.get("DATA_DIR", _REPO_ROOT / "dataset"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", _REPO_ROOT / "output"))

# All artefacts live under OUTPUT_DIR so a single mounted volume carries them.
DB_PATH = OUTPUT_DIR / "warehouse.duckdb"
IMAGES_OUT_DIR = OUTPUT_DIR / "images"
SUMMARY_PATH = OUTPUT_DIR / "summary.json"

# Facts the data cannot tell us (from the challenge brief).
YEAR = 2025
MAX_SPEED_MS = 150.0  # m/s; a step implying faster movement is a measurement error
