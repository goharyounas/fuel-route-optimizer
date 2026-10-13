"""Domain errors raised by the planning services.

Each carries a stable machine-readable ``code`` and the HTTP status the API should
answer with, so the API exception handler and the HTML map view can report them
consistently.
"""

from __future__ import annotations


class PlannerError(Exception):
    code = "planner_error"
    http_status = 500

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class LocationError(PlannerError):
    """The start or finish could not be turned into a US location."""

    http_status = 400

    def __init__(self, message: str, *, code: str = "invalid_location", details: dict | None = None):
        super().__init__(message, details=details)
        self.code = code


class RouteNotFoundError(PlannerError):
    code = "route_not_found"
    http_status = 422


class NoFeasiblePlanError(PlannerError):
    code = "no_feasible_fuel_plan"
    http_status = 422


class UpstreamServiceError(PlannerError):
    """A free third-party service (routing or geocoding) failed or timed out."""

    code = "upstream_unavailable"
    http_status = 503


class StationDataMissingError(PlannerError):
    code = "station_data_missing"
    http_status = 503
