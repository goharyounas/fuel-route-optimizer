import numpy as np
import pytest

from fuel_planner.services.geo import (
    EARTH_RADIUS_MILES,
    METERS_PER_MILE,
    chord_to_miles,
    cumulative_miles,
    decode_polyline,
    encode_polyline,
    haversine_miles,
    miles_to_chord,
    resample_polyline,
    simplify_polyline,
)


def test_haversine_known_distance():
    # Chicago to Dallas is ~803 miles as the crow flies.
    assert haversine_miles(41.8781, -87.6298, 32.7767, -96.7970) == pytest.approx(803, abs=5)
    assert haversine_miles(10.0, 20.0, 10.0, 20.0) == 0


def test_decodes_googles_reference_polyline():
    coords = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
    assert np.allclose(coords, [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]])


def test_encode_decode_round_trip():
    rng = np.random.default_rng(1)
    points = np.column_stack((rng.uniform(25, 49, 500), rng.uniform(-124, -67, 500))).round(6)
    assert np.allclose(decode_polyline(encode_polyline(points)), points, atol=1e-6)


@pytest.mark.parametrize("bad", ["_p~iF~ps|U_", "abc\x01", "_p~iF"])
def test_decode_rejects_malformed_polylines(bad):
    with pytest.raises(ValueError):
        decode_polyline(bad)


def test_decode_empty_polyline():
    assert decode_polyline("").shape == (0, 2)


def test_resample_spacing_and_endpoints():
    lat = np.array([40.0, 40.0, 41.0])
    lon = np.array([-100.0, -99.0, -99.0])
    lat_s, lon_s, dist = resample_polyline(lat, lon, 0.5)
    total = cumulative_miles(lat, lon)[-1]
    assert dist[-1] == pytest.approx(total)
    assert np.diff(dist).max() <= 0.5 + 1e-9
    assert (lat_s[0], lon_s[0], lat_s[-1], lon_s[-1]) == pytest.approx((40.0, -100.0, 41.0, -99.0))


def test_resample_ignores_repeated_vertices():
    lat = np.array([40.0, 40.0, 40.0, 40.5])
    lon = np.array([-100.0, -100.0, -100.0, -100.0])
    _, _, dist = resample_polyline(lat, lon, 1.0)
    assert np.all(np.diff(dist) > 0)


def test_chord_conversion_round_trip():
    assert chord_to_miles(miles_to_chord(5.0)) == pytest.approx(5.0)


def reference_douglas_peucker(lat, lon, tolerance):
    """Straightforward recursive Douglas-Peucker used as the oracle."""
    mpd = np.pi * EARTH_RADIUS_MILES * METERS_PER_MILE / 180.0
    x, y = lon * mpd * np.cos(np.radians(lat.mean())), lat * mpd
    keep = np.zeros(lat.size, bool)
    keep[[0, -1]] = True
    stack = [(0, lat.size - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        px, py = x[first + 1 : last], y[first + 1 : last]
        dx, dy = x[last] - x[first], y[last] - y[first]
        length_sq = dx * dx + dy * dy
        t = np.clip(((px - x[first]) * dx + (py - y[first]) * dy) / (length_sq or 1), 0, 1)
        dist = np.hypot(px - (x[first] + t * dx), py - (y[first] + t * dy))
        i = int(np.argmax(dist))
        if dist[i] > tolerance:
            keep[first + 1 + i] = True
            stack += [(first, first + 1 + i), (first + 1 + i, last)]
    return np.flatnonzero(keep)


@pytest.mark.parametrize("seed", range(5))
def test_simplify_matches_reference_douglas_peucker(seed):
    walk = np.cumsum(np.random.default_rng(seed).normal(size=(3000, 2)) * 1e-4, axis=0) + [35, -95]
    lat, lon = walk[:, 0], walk[:, 1]
    assert np.array_equal(simplify_polyline(lat, lon, 25.0), reference_douglas_peucker(lat, lon, 25.0))


def test_simplify_straight_line_keeps_only_endpoints():
    lat = np.linspace(40, 41, 100)
    lon = np.full(100, -100.0)
    assert simplify_polyline(lat, lon, 10.0).tolist() == [0, 99]
