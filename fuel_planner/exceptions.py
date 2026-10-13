"""Uniform JSON error bodies: {"error": {"code": ..., "message": ..., "details": {...}}}."""

import logging

from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .services.errors import PlannerError

logger = logging.getLogger(__name__)


def api_exception_handler(exc, context):
    if isinstance(exc, PlannerError):
        body = {"code": exc.code, "message": exc.message, "details": exc.details}
        return Response({"error": body}, status=exc.http_status)

    response = drf_exception_handler(exc, context)
    if response is None:
        logger.exception("Unhandled error while planning a route", exc_info=exc)
        body = {"code": "internal_error", "message": "Something went wrong on our side.", "details": {}}
        return Response({"error": body}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    if isinstance(exc, ValidationError):
        body = {"code": "invalid_request", "message": "Request validation failed.", "details": response.data}
    else:
        detail = (
            response.data.get("detail", response.data) if isinstance(response.data, dict) else response.data
        )
        body = {"code": getattr(exc, "default_code", "error"), "message": str(detail), "details": {}}
    response.data = {"error": body}
    return response
