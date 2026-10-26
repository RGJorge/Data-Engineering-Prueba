"""Pipeline entrypoint: build the warehouse from /data into /output.

Rebuilds the DuckDB file from scratch each run (idempotent), then loads tracks,
clips and images. Each loader is defensive at the row level, so one bad file
never aborts the run.
"""
from __future__ import annotations

import time

from . import load_clips, load_images, load_tracks
from .common import config, schema


def main() -> None:
    print(f"[pipeline] DATA_DIR={config.DATA_DIR}  OUTPUT_DIR={config.OUTPUT_DIR}")
    config.IMAGES_OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = schema.connect(fresh=True)
    try:
        for name, mod in (("tracks", load_tracks), ("clips", load_clips), ("images", load_images)):
            t0 = time.time()
            mod.load(con)
            print(f"[pipeline] {name} done in {time.time() - t0:.1f}s")
    finally:
        con.close()
    print("[pipeline] warehouse ready")


if __name__ == "__main__":
    main()
