from urllib.parse import urlencode

from django.urls import reverse
from rest_framework.renderers import TemplateHTMLRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RoutePlanRequestSerializer, location_as_text
from .services.errors import PlannerError
from .services.planner import get_planner
from .services.stations import get_station_index


class RoutePlanView(APIView):
    """Plan the cheapest fuel stops between two US locations.

    GET  /api/v1/route/?start=Chicago, IL&finish=Dallas, TX[&stop_cost=15]
    POST /api/v1/route/  {"start": "Chicago, IL", "finish": {"lat": 32.78, "lng": -96.8}}
    """

    def get(self, request):
        return self._plan(request, request.query_params)

    def post(self, request):
        return self._plan(request, request.data)

    def _plan(self, request, data):
        serializer = RoutePlanRequestSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data
        result = get_planner().plan(params["start"], params["finish"], stop_cost=params.get("stop_cost"))
        payload = result.payload
        map_url = request.build_absolute_uri(_with_query(reverse("fuel_planner:route-map"), params))
        payload["map"] = {"url": map_url, **payload["map"]}
        return Response(payload)


class RouteMapView(APIView):
    """Interactive Leaflet map of a plan (same parameters as the JSON endpoint)."""

    renderer_classes = [TemplateHTMLRenderer]
    template_name = "fuel_planner/route_map.html"

    def get(self, request):
        query = {key: value for key, value in request.query_params.items() if value.strip()}
        context = {"start": query.get("start", ""), "finish": query.get("finish", ""), "stop_cost": ""}
        if "start" not in query and "finish" not in query:
            return Response(context)  # empty form

        serializer = RoutePlanRequestSerializer(data=query)
        if not serializer.is_valid():
            context["error"] = {"message": "Please check the form.", "details": _readable(serializer.errors)}
            return Response(context, status=400)
        params = serializer.validated_data
        context["stop_cost"] = query.get("stop_cost", "")
        try:
            result = get_planner().plan(params["start"], params["finish"], stop_cost=params.get("stop_cost"))
        except PlannerError as exc:
            context["error"] = {"code": exc.code, "message": exc.message, "details": _readable(exc.details)}
            return Response(context, status=exc.http_status)
        context.update(
            plan=result.payload,
            stations=result.stations_on_route,
            api_url=_with_query(reverse("fuel_planner:route-plan"), params),
        )
        return Response(context)


class HealthView(APIView):
    def get(self, request):
        index = get_station_index()
        return Response(
            {
                "status": "ok" if len(index) else "degraded",
                "stations_loaded": len(index),
                "station_data_version": index.version if len(index) else None,
            }
        )


def _readable(details: dict) -> list[str]:
    """Flatten error details into lines a person can read on the map page."""
    lines = []
    for key, value in details.items():
        text = "; ".join(str(item) for item in value) if isinstance(value, list) else str(value)
        lines.append(f"{key.replace('_', ' ')}: {text}")
    return lines


def _with_query(path: str, params: dict) -> str:
    query = {"start": location_as_text(params["start"]), "finish": location_as_text(params["finish"])}
    if params.get("stop_cost") is not None:
        query["stop_cost"] = f"{params['stop_cost']:g}"
    return f"{path}?{urlencode(query)}"
