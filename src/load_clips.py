"""Load clip metadata. Clips come in three firmware dialects:

  F1: {file,  start(ISO+offset), fps, frames,      labels:[{frame, object_id, box}]}
  F2: {clip,  start(epoch s),    fps, n_frames,    labels:[{frame, track_id,  bbox}]}
  F3: {clip,  start_ms,          fps, duration_ms, labels:[{t_ms,  track,     box}]}

duration = frames / fps (F1, F2) or duration_ms / 1000 (F3).

A label "falls within its clip" is treated as a *temporal* test: its frame (or
time) lies within the clip's length. The spatial box can't be checked uniformly
because F2/F3 give pixel boxes with no frame dimensions, so we do not reject on
it (documented decision).
"""
from __future__ import annotations

import json
from pathlib import Path

from .common import config, schema
from .common.timeparse import from_epoch, from_iso


def _parse_clip(d: dict):
    """Normalise a clip record to (clip_file, start_dt, fps, frames, duration_s,
    labels) where labels is a list of (object_id, is_valid). Temporal validity
    only (see module docstring)."""
    fps = float(d["fps"])

    if "frames" in d and "file" in d:                       # F1
        clip_file = d.get("file")
        start = from_iso(d["start"]) if d.get("start") is not None else None
        n_frames = int(d["frames"])
        duration_s = n_frames / fps if fps else 0.0
        labels = [(l.get("object_id"), _in_range(l.get("frame"), n_frames))
                  for l in d.get("labels", [])]

    elif "n_frames" in d:                                   # F2
        clip_file = d.get("clip")
        start = from_epoch(d["start"]) if d.get("start") is not None else None
        n_frames = int(d["n_frames"])
        duration_s = n_frames / fps if fps else 0.0
        labels = [(l.get("track_id"), _in_range(l.get("frame"), n_frames))
                  for l in d.get("labels", [])]

    else:                                                   # F3
        clip_file = d.get("clip")
        start = from_epoch(d["start_ms"]) if d.get("start_ms") is not None else None
        duration_ms = float(d["duration_ms"])
        duration_s = duration_ms / 1000.0
        n_frames = int(round(duration_s * fps)) if fps else 0
        labels = [(l.get("track"), _in_range(l.get("t_ms"), duration_ms))
                  for l in d.get("labels", [])]

    return clip_file, start, fps, n_frames, duration_s, labels


def _in_range(pos, limit) -> bool:
    return pos is not None and 0 <= pos < limit


def load(con) -> None:
    clip_rows = []
    label_rows = []
    reject_rows = []

    for station_dir in sorted(config.DATA_DIR.glob("S*")):
        station_id = station_dir.name
        clips_dir = station_dir / "clips"
        if not clips_dir.is_dir():
            continue

        for jsonl in sorted(clips_dir.glob("*")):
            if not jsonl.is_file():
                continue
            rel = str(jsonl.relative_to(config.DATA_DIR))
            with open(jsonl, encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        clip_file, start, fps, n_frames, dur, labels = _parse_clip(json.loads(line))
                    except Exception as exc:
                        reject_rows.append(("clip", station_id, f"{rel}:{lineno}",
                                            "malformed", str(exc)[:200]))
                        continue

                    start_naive = start.replace(tzinfo=None) if start else None
                    clip_rows.append((station_id, clip_file, start_naive, fps, n_frames, dur, rel))
                    for object_id, ok in labels:
                        label_rows.append((station_id, clip_file, None, object_id, ok, rel))
                        if not ok:
                            reject_rows.append(("clip_label", station_id, rel,
                                                "outside_clip", f"{clip_file} {object_id}"))

    schema.bulk_insert(
        con, "clips",
        ["station_id", "clip_file", "start_utc", "fps", "frames", "duration_s", "source_file"],
        clip_rows)
    schema.bulk_insert(
        con, "clip_labels",
        ["station_id", "clip_file", "frame", "object_id", "is_valid", "source_file"],
        label_rows)
    schema.bulk_insert(
        con, "rejects",
        ["source_type", "station_id", "location", "reason", "detail"],
        reject_rows)

    print(f"[clips] clips={len(clip_rows)} labels={len(label_rows)} "
          f"rejected_labels={sum(1 for r in reject_rows if r[3]=='outside_clip')} "
          f"malformed={sum(1 for r in reject_rows if r[3]=='malformed')}")
