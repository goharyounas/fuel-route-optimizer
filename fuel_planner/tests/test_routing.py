import pytest
import requests

from fuel_planner.services.errors import RouteNotFoundError, UpstreamServiceError
from fuel_planner.services.routing import OsrmClient


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.headers = {}
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append((url, params))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


OK = FakeResponse(
    200, {"code": "Ok", "routes": [{"distance": 1609.344, "duration": 60.0, "geometry": "_p~iF~ps|U"}]}
)


def client(*outcomes):
    session = FakeSession(*outcomes)
    return OsrmClient("https://osrm.test", timeout=5, user_agent="test", session=session), session


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("fuel_planner.services.routing.time.sleep", lambda seconds: None)


def test_parses_a_route_and_requests_lon_lat_order():
    osrm, session = client(OK)
    route, calls = osrm.route((41.0, -87.0), (32.0, -96.0))
    assert (route.distance_miles, route.duration_seconds, calls) == (pytest.approx(1.0), 60.0, 1)
    url, params = session.requests[0]
    assert url.endswith("/route/v1/driving/-87.000000,41.000000;-96.000000,32.000000")
    assert params["overview"] == "full" and params["geometries"] == "polyline6"


def test_retries_once_on_connection_error():
    osrm, _ = client(requests.ConnectionError("reset"), OK)
    assert osrm.route((41, -87), (32, -96))[1] == 2


def test_retries_once_on_server_overload():
    osrm, _ = client(FakeResponse(503, {"code": "Overloaded"}), OK)
    assert osrm.route((41, -87), (32, -96))[1] == 2


def test_gives_up_after_the_retry():
    osrm, _ = client(requests.ConnectionError("down"), requests.ConnectionError("down"))
    with pytest.raises(UpstreamServiceError):
        osrm.route((41, -87), (32, -96))


def test_timeout_is_not_retried():
    osrm, session = client(requests.ReadTimeout("slow"))
    with pytest.raises(UpstreamServiceError):
        osrm.route((41, -87), (32, -96))
    assert len(session.requests) == 1


def test_no_route_is_a_client_error():
    osrm, _ = client(FakeResponse(400, {"code": "NoRoute", "message": "Impossible route"}))
    with pytest.raises(RouteNotFoundError):
        osrm.route((21.3, -157.8), (34.0, -118.2))


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(400, {"code": "InvalidQuery", "message": "bad"}),
        FakeResponse(502, ValueError("not json")),
    ],
)
def test_other_failures_are_upstream_errors(response):
    osrm, _ = client(response, response)
    with pytest.raises(UpstreamServiceError):
        osrm.route((41, -87), (32, -96))


def test_other_transport_errors_are_upstream_errors():
    osrm, _ = client(requests.exceptions.ChunkedEncodingError("cut off"))
    with pytest.raises(UpstreamServiceError):
        osrm.route((41, -87), (32, -96))
