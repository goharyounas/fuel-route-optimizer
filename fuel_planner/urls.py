from django.urls import path

from .views import HealthView, RouteMapView, RoutePlanView

app_name = "fuel_planner"

urlpatterns = [
    path("", RouteMapView.as_view(), name="home"),
    path("api/v1/route/", RoutePlanView.as_view(), name="route-plan"),
    path("api/v1/route/map/", RouteMapView.as_view(), name="route-map"),
    path("api/v1/health/", HealthView.as_view(), name="health"),
    # Same endpoints without the trailing slash: Django can't redirect a POST to the
    # slashed URL, so `POST /api/v1/route` would otherwise fail.
    path("api/v1/route", RoutePlanView.as_view()),
    path("api/v1/route/map", RouteMapView.as_view()),
    path("api/v1/health", HealthView.as_view()),
]
