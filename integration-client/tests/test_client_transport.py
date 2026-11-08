"""UrllibTransport (design gate #571 v24 §2「模块与公开接口」transport 一行, §7 rows marked 167).

不联网：标准库的 HTTP 服务器在本机回环地址的临时端口上起一个线程。超时用一个不回应的
路径（服务器线程等事件、测试结束时放行），不 sleep。
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from acuven_billing_client import Response, TransportError, UrllibTransport
from acuven_billing_client.transport import MAX_RESPONSE_BYTES

SECRET_HEADER = "sig-0123456789abcdef-must-not-leak"
SECRET_BODY = b'{"event_id":"payload-must-not-leak"}'


class _Server:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.landing_hits = 0
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def _reply(self, status: int, body: bytes, extra: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _route(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if self.path == "/echo":
                    document = {
                        "method": self.command,
                        "path": self.path,
                        "body": body.decode("utf-8"),
                        "x_test": self.headers.get("X-Test"),
                        "content_type": self.headers.get("Content-Type"),
                    }
                    self._reply(200, json.dumps(document).encode())
                elif self.path == "/big":
                    self._reply(200, bytes(range(256)) * (100 * 1024 // 256))
                elif self.path == "/error":
                    self._reply(503, b'{"error":{"code":"X"},"retryable":true}')
                elif self.path == "/redirect":
                    self._reply(302, b"", {"Location": "/landing"})
                elif self.path == "/landing":
                    server.landing_hits += 1
                    self._reply(200, b"landed")
                elif self.path == "/hang":
                    server.release.wait(10)
                else:
                    self._reply(404, b"")

            do_GET = _route
            do_POST = _route

        return Handler

    def __enter__(self) -> _Server:
        self.thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)


@pytest.fixture
def server() -> Iterator[_Server]:
    with _Server() as running:
        yield running


@pytest.fixture
def transport() -> UrllibTransport:
    return UrllibTransport()


def closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_post_sends_method_path_headers_and_body(server: _Server, transport: UrllibTransport):
    response = transport.send(
        "POST",
        server.base + "/echo",
        {"X-Test": "yes", "Content-Type": "application/json"},
        b'{"a":1}',
        5,
    )

    assert isinstance(response, Response)
    assert response.status == 200
    echoed = json.loads(response.body)
    assert echoed == {
        "method": "POST",
        "path": "/echo",
        "body": '{"a":1}',
        "x_test": "yes",
        "content_type": "application/json",
    }


def test_get_without_body(server: _Server, transport: UrllibTransport):
    response = transport.send("GET", server.base + "/echo", {}, b"", 5)

    assert response.status == 200
    assert json.loads(response.body)["method"] == "GET"
    assert json.loads(response.body)["body"] == ""


def test_non_2xx_is_returned_not_raised(server: _Server, transport: UrllibTransport):
    response = transport.send("POST", server.base + "/error", {}, b"{}", 5)

    assert response == Response(status=503, body=b'{"error":{"code":"X"},"retryable":true}')


def test_response_body_is_cut_at_64_kib(server: _Server, transport: UrllibTransport):
    response = transport.send("GET", server.base + "/big", {}, b"", 5)

    assert response.status == 200
    assert MAX_RESPONSE_BYTES == 64 * 1024
    assert len(response.body) == MAX_RESPONSE_BYTES
    assert response.body == (bytes(range(256)) * (100 * 1024 // 256))[:MAX_RESPONSE_BYTES]


def test_redirect_is_not_followed(server: _Server, transport: UrllibTransport):
    with pytest.raises(TransportError):
        transport.send("POST", server.base + "/redirect", {}, b"{}", 5)

    assert server.landing_hits == 0


def test_timeout_becomes_transport_error(server: _Server, transport: UrllibTransport):
    with pytest.raises(TransportError):
        transport.send("POST", server.base + "/hang", {}, b"{}", 0.2)


def test_connection_refused_becomes_transport_error(transport: UrllibTransport):
    url = f"http://127.0.0.1:{closed_port()}/api/v1/integration/usage-events/batch"

    with pytest.raises(TransportError) as raised:
        transport.send("POST", url, {"X-Acuven-Signature": SECRET_HEADER}, SECRET_BODY, 2)

    message = str(raised.value)
    assert SECRET_HEADER not in message
    assert "payload-must-not-leak" not in message
    # 不链接原异常：原异常的消息里也不会带出请求的任何部分。
    assert raised.value.__cause__ is None


def test_timeout_message_has_no_headers_or_body(server: _Server, transport: UrllibTransport):
    with pytest.raises(TransportError) as raised:
        transport.send(
            "POST", server.base + "/hang", {"X-Acuven-Signature": SECRET_HEADER}, SECRET_BODY, 0.2
        )

    assert SECRET_HEADER not in str(raised.value)
    assert "payload-must-not-leak" not in str(raised.value)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://127.0.0.1/x", "not a url"])
def test_only_http_and_https(transport: UrllibTransport, url: str):
    with pytest.raises(TransportError):
        transport.send("GET", url, {}, b"", 1)
