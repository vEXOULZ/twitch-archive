"""Feathers-style error bodies: {name, message, code, className}."""

from __future__ import annotations

from fastapi.responses import JSONResponse

_CLASS = {
    400: ("BadRequest", "bad-request"),
    404: ("NotFound", "not-found"),
    405: ("MethodNotAllowed", "method-not-allowed"),
    429: ("TooManyRequests", "too-many-requests"),
    500: ("GeneralError", "general-error"),
}


class FeathersError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def response(self) -> JSONResponse:
        name, class_name = _CLASS.get(self.code, ("GeneralError", "general-error"))
        body = {"name": name, "message": self.message, "code": self.code, "className": class_name}
        return JSONResponse(body, status_code=self.code)


def legacy_error(status: int, msg: str) -> JSONResponse:
    """Body used by the custom (non-service) routes: {error: true, msg}."""
    return JSONResponse({"error": True, "msg": msg}, status_code=status)


class LegacyError(Exception):
    """Raised by the custom routes; rendered with ``legacy_error``."""

    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status = status
        self.msg = msg
