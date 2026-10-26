"""Geospatial helpers for the speed sanity check (fact 2)."""
from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

_EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in metres."""
    rlat1, rlat2 = radians(lat1), radians(lat2)
    dlat = rlat2 - rlat1
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(rlat1) * cos(rlat2) * sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_M * asin(sqrt(a))


def implied_speed_ms(
    lat1: float, lon1: float, t1: float, lat2: float, lon2: float, t2: float
) -> float:
    """Horizontal speed (m/s) implied by moving between two timestamped points.

    ``t1``/``t2`` are POSIX seconds. A zero or negative time delta returns
    infinity so the point is treated as impossible (can't be in two places at
    once). Horizontal distance only -- altitude noise would make the check
    jittery and the 150 m/s ceiling is about ground movement.
    """
    dt = t2 - t1
    if dt <= 0:
        return float("inf")
    return haversine_m(lat1, lon1, lat2, lon2) / dt
