"""Validate images, copy the good ones to /output/images, and load detection
metadata into the warehouse.

Images are indexed per station by basename (filenames are UUIDs, unique), which
makes record<->image linkage robust regardless of whether a firmware writes the
date folder into the path (A/B do, C does not).

Box convention (discovered from the data): [x, y, w, h], top-left corner + size.
A/B are in pixels (need the image's own width/height); C is normalised ([0,1]).
A detection is valid if its box overlaps the image at all (partial = valid);
entirely outside = rejected. Detections are only scored for valid images.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Dict, Optional, Tuple

from PIL import Image

from .common import config, context, schema


def _validate_image(path: Path) -> Tuple[bool, Optional[int], Optional[int]]:
    """Return (is_valid, width, height). Invalid = unreadable/corrupt/not an image."""
    try:
        with Image.open(path) as im:
            im.verify()                 # structural integrity
        with Image.open(path) as im:
            w, h = im.size
            im.load()                   # force decode -> catches truncation
        return True, int(w), int(h)
    except Exception:
        return False, None, None


def _box_overlaps(box, img_w: float, img_h: float, normalised: bool) -> bool:
    """True if [x,y,w,h] (top-left+size) overlaps the image rectangle at all."""
    if not box or len(box) < 4:
        return False
    x, y, w, h = box[:4]
    W = 1.0 if normalised else img_w
    H = 1.0 if normalised else img_h
    return (x < W) and (x + w > 0) and (y < H) and (y + h > 0)


def _parse_record(d: dict):
    """Normalise a detection record across firmwares.

    Returns (image_basename, faces_count, detections, box_is_normalised) where
    detections is a list of (class_lower, box).
    """
    if "objects" in d:                                   # firmware A
        fname = d.get("image", "")
        faces = len(d.get("faces") or [])
        dets = [((o.get("label") or "").lower(), o.get("bbox")) for o in d["objects"]]
        return Path(fname).name, faces, dets, False
    if "ts_ms" in d:                                     # firmware B (class key is 'cls')
        fname = d.get("file", "")
        faces = len(d.get("face_boxes") or [])
        dets = [((o.get("cls") or "").lower(), o.get("box")) for o in d.get("detections", [])]
        return Path(fname).name, faces, dets, False
    # firmware C
    fname = d.get("file", "")
    faces = len(d.get("faces") or [])
    dets = [((o.get("class") or "").lower(), o.get("box")) for o in d.get("detections", [])]
    return Path(fname).name, faces, dets, True


def load(con) -> None:
    class_codes = context.load_class_codes()  # not strictly needed; classes arrive as names
    reject_rows = []
    meta_rows = []
    det_rows = []

    for station_dir in sorted(config.DATA_DIR.glob("S*")):
        station_id = station_dir.name
        images_dir = station_dir / "images"
        if not images_dir.is_dir():
            continue

        # --- 1. Index & validate every candidate image file -------------------
        index: Dict[str, dict] = {}   # basename -> {rel, valid, w, h}
        for path in images_dir.rglob("*"):
            if path.is_dir():
                continue
            if path.name.startswith("detections_") and path.suffix == ".jsonl":
                continue  # metadata, not an image
            rel = str(path.relative_to(config.DATA_DIR))
            ok, w, h = _validate_image(path)
            index[path.name] = {"rel": rel, "valid": ok, "w": w, "h": h, "meta": False}
            if not ok:
                reject_rows.append(("image", station_id, rel, "corrupted",
                                    "unreadable / not a valid image"))
            else:
                # Copy valid images to /output/images, preserving the path
                # relative to the dataset root.
                dst = config.IMAGES_OUT_DIR / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dst)

        # --- 2. Read detection metadata (jsonl, three firmwares) --------------
        for jsonl in sorted(images_dir.glob("detections_*.jsonl")):
            rel_j = str(jsonl.relative_to(config.DATA_DIR))
            with open(jsonl, encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        basename, faces, dets, norm = _parse_record(d)
                    except Exception as exc:
                        reject_rows.append(("metadata_line", station_id,
                                            f"{rel_j}:{lineno}", "malformed", str(exc)[:200]))
                        continue

                    img = index.get(basename)
                    image_exists = img is not None
                    image_valid = bool(img and img["valid"])
                    if image_exists:
                        img["meta"] = True
                    else:
                        reject_rows.append(("metadata_line", station_id,
                                            f"{rel_j}:{lineno}", "missing_image", basename))

                    meta_rows.append((station_id, basename, image_exists, image_valid,
                                      faces if image_valid else 0, rel_j))

                    # Score detections only for valid images.
                    if image_valid:
                        for cls, box in dets:
                            ok = _box_overlaps(box, img["w"], img["h"], norm)
                            det_rows.append((station_id, basename, cls, ok, rel_j))
                            if not ok:
                                reject_rows.append(("detection", station_id, rel_j,
                                                    "outside_image", f"{basename} {cls}"))

        # --- 3. Persist the image rows (with has_metadata resolved) -----------
        img_rows = [(station_id, v["rel"], name, v["valid"], v["w"], v["h"], v["meta"])
                    for name, v in index.items()]
        schema.bulk_insert(
            con, "images",
            ["station_id", "rel_path", "filename", "is_valid", "width", "height", "has_metadata"],
            img_rows)

    schema.bulk_insert(
        con, "image_meta_records",
        ["station_id", "filename", "image_exists", "image_valid", "n_faces", "source_file"],
        meta_rows)
    schema.bulk_insert(
        con, "image_detections",
        ["station_id", "filename", "class", "is_valid", "source_file"],
        det_rows)
    schema.bulk_insert(
        con, "rejects",
        ["source_type", "station_id", "location", "reason", "detail"],
        reject_rows)

    n_files = con.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    n_valid = con.execute("SELECT COUNT(*) FROM images WHERE is_valid").fetchone()[0]
    print(f"[images] files={n_files} valid={n_valid} "
          f"meta_records={len(meta_rows)} detections={len(det_rows)}")
