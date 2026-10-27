"""Safe native logging and one diagnostic per completed HTTP request."""

import contextvars
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

request_context = contextvars.ContextVar("request_log_context", default={})


def exception_frames(error, seen=None):
    seen = set() if seen is None else seen
    if id(error) in seen:
        return {"type": type(error).__name__}
    seen.add(id(error))
    frames, tb = [], error.__traceback__
    while tb:
        frames.append(
            {
                "file": Path(tb.tb_frame.f_code.co_filename).name,
                "line": tb.tb_lineno,
                "function": tb.tb_frame.f_code.co_name,
            }
        )
        tb = tb.tb_next
    result = {"type": type(error).__name__, "frames": frames}
    cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    if cause:
        result["cause"] = exception_frames(cause, seen)
    children = getattr(error, "exceptions", ())
    if children:
        result["exceptions"] = [exception_frames(child, seen) for child in children]
    return result


class DiagnosticFormatter(logging.Formatter):
    def format(self, record):
        # Many handler messages contain SQL, provider replies or credentials.
        # A source location and stable event identify the failure without them.
        event = getattr(
            record, "event", "operation_failed" if record.levelno >= logging.WARNING else "operation_progress"
        )
        payload = {
            "logged_at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "logger": record.name,
            "log_level": record.levelname,
            "message": event,
            "event": event,
            "operation": record.funcName,
            "source_file": Path(record.pathname).name,
            "source_line": record.lineno,
            **request_context.get(),
        }
        for key in ("status_code", "duration_ms", "reason"):
            value = getattr(record, key, None)
            if isinstance(value, (str, int, float)):
                payload[key] = value
        if record.exc_info and record.exc_info[1]:
            payload["exception"] = exception_frames(record.exc_info[1])
        return json.dumps(payload, separators=(",", ":"))


class SafeTelemetryFilter(logging.Filter):
    """The OTLP SDK otherwise adds raw exception values independently of format()."""

    def filter(self, record):
        record.msg = DiagnosticFormatter().format(record)
        record.args = ()
        record.exc_info = record.exc_text = record.stack_info = None
        # OTLP exports arbitrary LogRecord extras by default.
        standard = logging.makeLogRecord({}).__dict__
        for key in list(record.__dict__):
            if key not in standard:
                del record.__dict__[key]
        return True


class ServerDiagnosticFilter(logging.Filter):
    def filter(self, record):
        return not (record.exc_info and getattr(record.exc_info[1], "_data_vault_logged", False))


class RequestLoggingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        supplied = dict(scope.get("headers", [])).get(b"x-request-id", b"").decode("latin-1")
        request_id = (
            supplied
            if re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", supplied)
            else str(uuid4())
        )
        method = (
            scope["method"]
            if scope["method"] in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
            else "OTHER"
        )
        token = request_context.set({"request_id": request_id, "method": method})
        status, failed, start = 500, False, monotonic()

        async def send_logged(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {
                    **message,
                    "headers": [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                    + [(b"x-request-id", request_id.encode("ascii"))],
                }
            await send(message)

        try:
            await self.app(scope, receive, send_logged)
        except Exception as error:
            failed = True
            logging.getLogger("mindsdb.http.request").exception(
                "request_failed", extra={"event": "request_failed", "status_code": 500}
            )
            error._data_vault_logged = True
            raise
        finally:
            try:
                if not failed:
                    probe = (
                        method in {"GET", "HEAD"}
                        and scope["path"] in {"/api/util/ping", "/api/status"}
                        and 200 <= status < 300
                    )
                    fields = {"status_code": status, "duration_ms": round((monotonic() - start) * 1000, 2)}
                    if status >= 400:
                        fields.update(
                            event="request_refused" if status < 500 else "request_failed",
                            reason="authentication_required"
                            if status == 401
                            else "permission_denied"
                            if status == 403
                            else "http_error",
                        )
                        logging.getLogger("mindsdb.http.request").log(
                            logging.ERROR if status >= 500 else logging.WARNING, fields["event"], extra=fields
                        )
                    elif not probe and os.getenv("ACCESS_LOG_MODE", "errors") == "all":
                        logging.getLogger("mindsdb.http.access").info(
                            "http_access", extra={**fields, "event": "http_access"}
                        )
            finally:
                request_context.reset(token)
