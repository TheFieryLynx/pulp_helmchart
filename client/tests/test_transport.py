"""Contract checks against pulpcore's configured transport."""

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from pulpcore.client.pulpcore import ApiClient, Configuration

from pulpcore.client.pulp_helmchart.api import (
    ContentChartsApi,
    RepositoriesHelmchartApi,
    _Http,
)


@contextmanager
def recording_server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(dict(self.headers))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("credentials", "expected"),
    [
        ({"username": "user", "password": "secret"}, "Basic dXNlcjpzZWNyZXQ="),
        ({"access_token": "token-value"}, "Bearer token-value"),
        (
            {
                "api_key": {"Authorization": "key-value"},
                "api_key_prefix": {"Authorization": "Bearer"},
            },
            "Bearer key-value",
        ),
    ],
)
def test_auth_and_default_headers_use_api_client(credentials, expected):
    with recording_server() as (host, received):
        configuration = Configuration(host=host)
        for key, value in credentials.items():
            setattr(configuration, key, value)
        client = ApiClient(configuration)
        client.default_headers["X-Configured"] = "from-api-client"
        assert _Http(client).request("GET", "/test/") == {"ok": True}
        if "username" not in credentials:
            assert client.default_headers["Authorization"] == expected
        assert received[0]["Authorization"] == expected
        assert received[0]["X-Configured"] == "from-api-client"


def test_cookie_api_key_uses_configured_auth():
    with recording_server() as (host, received):
        configuration = Configuration(host=host)
        configuration.api_key["cookieAuth"] = "session-value"
        assert _Http(ApiClient(configuration)).request("GET", "/test/") == {"ok": True}
        assert received[0]["Cookie"] == "session-value"


def test_calls_supplied_api_client_transport(monkeypatch):
    client = ApiClient(Configuration(host="https://example.test"))
    calls = []

    def call_api(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(status=200, read=lambda: b'{"ok": true}')

    monkeypatch.setattr(client, "call_api", call_api)
    assert _Http(client).request("GET", "/test/") == {"ok": True}
    assert calls[0][0][1] == "https://example.test/test/"
    monkeypatch.setattr(
        client,
        "call_api",
        lambda *args, **kwargs: (
            calls.append((args, kwargs))
            or SimpleNamespace(status=204, read=lambda: b"")
        ),
    )
    RepositoriesHelmchartApi(client).list(_request_timeout=7)
    assert calls[-1][1]["_request_timeout"] == 7


def test_path_upload_uses_bounded_pulp_chunks(tmp_path, monkeypatch):
    chart = tmp_path / "large.tgz"
    chart.write_bytes(b"x" * (9 * 1024 * 1024))
    chunks = []
    deleted = []

    class FakeUploads:
        def __init__(self, client):
            assert client is api_client

        def create(self, upload, **_kwargs):
            assert upload.size == chart.stat().st_size
            return SimpleNamespace(pulp_href="/pulp/api/v3/uploads/one/")

        def update(self, *, content_range, upload_href, file, **_kwargs):
            chunks.append((content_range, upload_href, len(file[1])))

        def delete(self, href, **_kwargs):
            deleted.append(href)

    api_client = ApiClient(Configuration(host="https://example.test"))
    api = ContentChartsApi(api_client)
    monkeypatch.setattr("pulpcore.client.pulp_helmchart.api.UploadsApi", FakeUploads)
    dispatched = []

    def request(*args, **kwargs):
        dispatched.append(kwargs["fields"])
        return "task"

    monkeypatch.setattr(api._http, "request", request)
    assert api.upload(file=chart, repository="/repo/") == "task"
    assert [chunk[2] for chunk in chunks] == [
        4 * 1024 * 1024,
        4 * 1024 * 1024,
        1024 * 1024,
    ]
    assert dispatched[0]["upload"] == "/pulp/api/v3/uploads/one/"
    assert dispatched[0]["relative_path"] == "large.tgz"
    assert "file" not in dispatched[0]
    assert not deleted


def test_failed_dispatch_deletes_unconsumed_upload(tmp_path, monkeypatch):
    chart = tmp_path / "chart.tgz"
    chart.write_bytes(b"bytes")
    deleted = []

    class FakeUploads:
        def __init__(self, _client):
            pass

        def create(self, _upload, **_kwargs):
            return SimpleNamespace(pulp_href="/upload/")

        def update(self, **_kwargs):
            pass

        def delete(self, href, **_kwargs):
            deleted.append(href)

    api = ContentChartsApi(ApiClient(Configuration(host="https://example.test")))
    monkeypatch.setattr("pulpcore.client.pulp_helmchart.api.UploadsApi", FakeUploads)
    monkeypatch.setattr(
        api._http,
        "request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError()),
    )
    with pytest.raises(ValueError):
        api.upload(file=chart)
    assert deleted == ["/upload/"]
