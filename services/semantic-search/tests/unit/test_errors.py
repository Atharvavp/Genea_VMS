"""The error envelope never echoes rejected input."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api.errors import (
    REQUEST_ID_HEADER,
    ApiError,
    InvalidQueryError,
    error_body,
    install_error_handlers,
    resolve_request_id,
    sanitize_validation_errors,
)


class _Payload(BaseModel):
    query: str
    top_k: int


@pytest.fixture()
def client():
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/echo")
    async def echo(payload: _Payload) -> dict[str, str]:
        return {"ok": payload.query}

    @app.get("/boom")
    async def boom() -> None:
        raise InvalidQueryError()

    @app.get("/unhandled")
    async def unhandled() -> None:
        raise RuntimeError("secret path /opt/models/siglip and token hunter2")

    return TestClient(app, raise_server_exceptions=False)


def test_every_error_uses_the_component4_envelope(client):
    response = client.get("/boom")
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "invalid_query"
    assert set(body) == {"code", "message", "details", "request_id"}
    assert body["request_id"].startswith("req_")
    assert response.headers[REQUEST_ID_HEADER] == body["request_id"]


def test_validation_errors_report_the_field_but_never_the_value(client):
    response = client.post(
        "/echo", json={"query": "a-very-secret-search-term", "top_k": "NaN"}
    )
    assert response.status_code == 422
    text = response.text
    assert "a-very-secret-search-term" not in text
    assert "NaN" not in text
    fields = response.json()["error"]["details"]["fields"]
    assert fields[0]["field"] == "top_k"


def test_an_unhandled_exception_leaks_nothing(client):
    response = client.get("/unhandled")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "hunter2" not in response.text
    assert "/opt/models" not in response.text


def test_an_inbound_request_id_is_accepted_only_when_conservative(client):
    response = client.get("/boom", headers={REQUEST_ID_HEADER: "req_abc-123"})
    assert response.json()["error"]["request_id"] == "req_abc-123"
    hostile = client.get("/boom", headers={REQUEST_ID_HEADER: "a b\nc"})
    assert hostile.json()["error"]["request_id"].startswith("req_")


def test_error_helpers_are_pure():
    body = error_body("event_not_found", "gone", "req_1")
    assert body == {
        "error": {
            "code": "event_not_found",
            "message": "gone",
            "details": {},
            "request_id": "req_1",
        }
    }
    assert resolve_request_id(None).startswith("req_")
    sanitized = sanitize_validation_errors(
        [{"loc": ("body", "query"), "msg": "Value error, is too long", "type": "value_error"}]
    )
    assert sanitized == {
        "fields": [{"field": "query", "reason": "is too long", "type": "value_error"}]
    }


def test_api_error_carries_its_own_status_and_code():
    class Custom(ApiError):
        status_code = 418
        code = "teapot"
        message = "no coffee"

    error = Custom()
    assert (error.status_code, error.code, error.detail_message) == (418, "teapot", "no coffee")
