import asyncio
import io
import json
import logging

import httpx
from a2wsgi import WSGIMiddleware
from flask import Flask
from starlette.applications import Starlette
from starlette.responses import Response, StreamingResponse
from starlette.routing import Mount, Route

from mindsdb.utilities import log
from mindsdb.utilities.diagnostics import (
    DiagnosticFormatter,
    RequestLoggingMiddleware,
    SafeTelemetryFilter,
    request_context,
)


def test_exception_and_telemetry_privacy():
    try:
        try:
            raise ValueError("private-query SELECT private-value")
        except ValueError as cause:
            raise RuntimeError("private-token") from cause
    except RuntimeError:
        record = logging.getLogger("mindsdb.test").makeRecord(
            "mindsdb.test",
            logging.ERROR,
            __file__,
            12,
            "private-provider-body %s",
            ("private-key",),
            __import__("sys").exc_info(),
            extra={"private_extra": "private-body"},
        )
    output = DiagnosticFormatter().format(record)
    event = json.loads(output)
    assert "private-" not in output
    assert event["exception"]["type"] == "RuntimeError"
    assert event["exception"]["cause"]["type"] == "ValueError"
    assert event["exception"]["frames"][0]["function"] == "test_exception_and_telemetry_privacy"
    SafeTelemetryFilter().filter(record)
    assert "private-" not in str(record.__dict__)
    assert record.exc_info is None
    assert json.loads(record.getMessage())["exception"]["type"] == "RuntimeError"


def test_installed_handlers_are_quiet_and_safe(capsys):
    log.configure_logging()
    logging.getLogger("mindsdb.test").info("ordinary_success")
    assert capsys.readouterr().err == ""
    logging.getLogger("mindsdb.test").warning("private-provider-reply")
    event = json.loads(capsys.readouterr().err)
    assert event["log_level"] == "WARNING"
    assert "private-" not in str(event)
    logging.getLogger("uvicorn.access").info("private-path?key=private-token")
    assert capsys.readouterr().err == ""


def test_http_matrix_and_wsgi_context(monkeypatch):
    async def exercise():
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.setFormatter(DiagnosticFormatter())
        names = ("mindsdb.http.request", "mindsdb.http.access", "mindsdb.flask")
        saved = []
        for name in names:
            logger = logging.getLogger(name)
            saved.append((logger, logger.handlers[:], logger.level, logger.propagate))
            logger.handlers = [handler]
            logger.setLevel(logging.INFO)
            logger.propagate = False

        async def endpoint(request):
            return Response(status_code=int(request.path_params.get("status", request.query_params.get("status", 200))))

        async def stream(request):
            async def body():
                yield b"partial"
                raise RuntimeError("private-stream-body")

            return StreamingResponse(body())

        flask_app = Flask(__name__)

        @flask_app.get("/context")
        def context():
            logging.getLogger("mindsdb.flask").warning("private-provider-body")
            return "ok"

        app = RequestLoggingMiddleware(
            Starlette(
                routes=[
                    Route("/api/util/ping", endpoint, methods=["GET", "POST"]),
                    Route("/api/status", endpoint, methods=["GET", "POST"]),
                    Route("/result/{status}", endpoint),
                    Route("/stream", stream),
                    Mount("/wsgi", WSGIMiddleware(flask_app)),
                ]
            )
        )
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                for mode, count in (("errors", 0), ("all", 1)):
                    monkeypatch.setenv("ACCESS_LOG_MODE", mode)
                    output.seek(0)
                    output.truncate()
                    for path in ("/api/util/ping", "/api/status"):
                        await client.get(f"{path}?secret=private-query")
                        await client.head(path)
                    await client.get("/result/200")
                    assert len(output.getvalue().splitlines()) == count
                output.seek(0)
                output.truncate()
                for path in ("/api/util/ping", "/api/status"):
                    await client.post(path)
                    await client.get(f"{path}?status=503")
                    await client.head(f"{path}?status=503")
                events = [json.loads(line) for line in output.getvalue().splitlines()]
                assert sum(event["event"] == "http_access" for event in events) == 2
                assert sum(event["event"] == "request_failed" and event["status_code"] == 503 for event in events) == 4
                output.seek(0)
                output.truncate()
                ids = ["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"]
                results = await asyncio.gather(
                    *(client.get("/result/403", headers={"x-request-id": rid}) for rid in ids)
                )
                assert [response.headers["x-request-id"] for response in results] == ids
                events = [json.loads(line) for line in output.getvalue().splitlines()]
                assert {event["request_id"] for event in events} == set(ids)
                assert all(event["event"] == "request_refused" for event in events)
                await client.get("/wsgi/context", headers={"x-request-id": ids[0]})
                events = [json.loads(line) for line in output.getvalue().splitlines()]
                assert next(event for event in events if event["logger"] == "mindsdb.flask")["request_id"] == ids[0]
                try:
                    await client.get("/stream")
                except Exception:
                    pass
                else:
                    raise AssertionError("stream failure must still reach the server")
                events = [json.loads(line) for line in output.getvalue().splitlines()]
                assert sum(event["event"] == "request_failed" for event in events) == 1
                assert "private-" not in output.getvalue()
                assert request_context.get() == {}
        finally:
            for logger, handlers, level, propagate in saved:
                logger.handlers = handlers
                logger.setLevel(level)
                logger.propagate = propagate

    asyncio.run(exercise())
