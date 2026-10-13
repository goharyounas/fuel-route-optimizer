"""Geometry helpers for route processing (numpy only).

Coordinates are WGS84 degrees. Distances are statute miles unless stated otherwise.
"""

from __future__ import annotations

import numpy as np

EARTH_RADIUS_MILES = 3958.7613
METERS_PER_MILE = 1609.344


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles. Accepts scalars or numpy arrays (broadcasting)."""
    lat1, lon1, lat2, lon2 = (np.radians(v) for v in (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def cumulative_miles(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Distance along a polyline from its first vertex to every vertex."""
    if lat.size == 0:
        return np.empty(0)
    segments = haversine_miles(lat[:-1], lon[:-1], lat[1:], lon[1:])
    return np.concatenate(([0.0], np.cumsum(segments)))


def to_unit_vectors(lat, lon) -> np.ndarray:
    """Convert coordinates to 3D unit vectors, so nearest-neighbour search needs no projection."""
    lat_r, lon_r = np.radians(lat), np.radians(lon)
    cos_lat = np.cos(lat_r)
    return np.column_stack((cos_lat * np.cos(lon_r), cos_lat * np.sin(lon_r), np.sin(lat_r)))


def miles_to_chord(miles):
    """Straight-line (chord) length between two unit vectors that are `miles` apart on the sphere."""
    return 2.0 * np.sin(np.asarray(miles) / (2.0 * EARTH_RADIUS_MILES))


def chord_to_miles(chord):
    return 2.0 * EARTH_RADIUS_MILES * np.arcsin(np.clip(np.asarray(chord) / 2.0, 0.0, 1.0))


def decode_polyline(encoded: str, precision: int = 6) -> np.ndarray:
    """Decode a Google encoded polyline into an (N, 2) array of (lat, lon).

    Vectorised with numpy: a cross-country route (~40k points) decodes in a couple of
    milliseconds instead of ~100 ms for a character-by-character loop.
    """
    if not encoded:
        return np.empty((0, 2))
    data = np.frombuffer(encoded.encode("ascii"), dtype=np.uint8).astype(np.int64) - 63
    if data.min() < 0 or data.max() > 63:
        raise ValueError("Polyline contains characters outside the encoding alphabet.")
    is_last = data < 0x20  # a value's final 5-bit chunk has the continuation bit cleared
    if not is_last[-1]:
        raise ValueError("Polyline is truncated.")
    ends = np.flatnonzero(is_last)
    starts = np.concatenate(([0], ends[:-1] + 1))
    group = np.cumsum(is_last) - is_last
    shift = 5 * (np.arange(data.size) - starts[group])
    values = np.add.reduceat((data & 0x1F) << shift, starts)
    values = np.where(values & 1, ~(values >> 1), values >> 1)  # zig-zag decoding
    if values.size % 2:
        raise ValueError("Polyline has an odd number of values.")
    return np.cumsum(values.reshape(-1, 2), axis=0) / 10.0**precision


def encode_polyline(coords, precision: int = 6) -> str:
    """Encode (lat, lon) pairs as a Google polyline (inverse of `decode_polyline`)."""
    factor = 10**precision
    output: list[str] = []
    prev_lat = prev_lon = 0
    for lat, lon in coords:
        ilat, ilon = round(lat * factor), round(lon * factor)
        for delta in (ilat - prev_lat, ilon - prev_lon):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                output.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            output.append(chr(value + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(output)


def resample_polyline(lat: np.ndarray, lon: np.ndarray, spacing_miles: float):
    """Return points spaced every `spacing_miles` along the polyline, plus their distances.

    Long straight highway segments can have vertices miles apart. Resampling keeps the
    nearest-point search accurate (error <= spacing / 2) with a predictable point count.
    """
    cum = cumulative_miles(lat, lon)
    if cum.size == 0:
        return lat, lon, cum
    keep = np.concatenate(([True], np.diff(cum) > 0))  # np.interp needs increasing x values
    cum, lat, lon = cum[keep], lat[keep], lon[keep]
    total = cum[-1]
    if total == 0:
        return lat[:1], lon[:1], np.zeros(1)
    distances = np.linspace(0.0, total, int(np.ceil(total / spacing_miles)) + 1)
    return np.interp(distances, cum, lat), np.interp(distances, cum, lon), distances


def simplify_polyline(lat: np.ndarray, lon: np.ndarray, tolerance_meters: float) -> np.ndarray:
    """Douglas-Peucker simplification; returns the indices of the vertices to keep.

    Processed level by level: every pending segment is split in one vectorised pass,
    instead of one small numpy call per segment (~10x faster on 35k-point routes).
    """
    n = lat.size
    if n <= 2:
        return np.arange(n)
    # Equirectangular projection in metres around the mean latitude: straight lines stay
    # straight, and the scale error is small enough for a tolerance test.
    meters_per_degree = np.pi * EARTH_RADIUS_MILES * METERS_PER_MILE / 180.0
    x = lon * meters_per_degree * np.cos(np.radians(lat.mean()))
    y = lat * meters_per_degree
    keep = np.zeros(n, dtype=bool)
    keep[[0, -1]] = True
    firsts, lasts = np.array([0]), np.array([n - 1])
    while firsts.size:
        counts = lasts - firsts - 1  # interior points of each segment
        pending = counts > 0
        firsts, lasts, counts = firsts[pending], lasts[pending], counts[pending]
        if not firsts.size:
            break
        offsets = np.cumsum(counts) - counts  # where each segment's points start in the flat arrays
        segment = np.repeat(np.arange(firsts.size), counts)
        points = firsts[segment] + 1 + np.arange(counts.sum()) - offsets[segment]
        ax, ay = x[firsts][segment], y[firsts][segment]
        dx, dy = x[lasts][segment] - ax, y[lasts][segment] - ay
        length_sq = dx * dx + dy * dy
        t = np.clip(
            ((x[points] - ax) * dx + (y[points] - ay) * dy) / np.where(length_sq > 0, length_sq, 1.0), 0, 1
        )
        dist = np.hypot(x[points] - (ax + t * dx), y[points] - (ay + t * dy))
        farthest = np.maximum.reduceat(dist, offsets)
        position = np.where(dist == farthest[segment], np.arange(dist.size), dist.size)
        split = farthest > tolerance_meters
        splits = points[np.minimum.reduceat(position, offsets)[split]]
        keep[splits] = True
        firsts = np.concatenate((firsts[split], splits))
        lasts = np.concatenate((splits, lasts[split]))
    return np.flatnonzero(keep)
