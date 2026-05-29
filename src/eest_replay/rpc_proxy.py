"""A transparent JSON-RPC proxy that records eth_sendRawTransaction payloads.

Placed between EEST `execute` and a real geth: every call is forwarded
verbatim and the real response returned, but the raw signed transaction of
each ``eth_sendRawTransaction`` (including those inside JSON-RPC batches) is
appended, in submission order, to an in-memory list. Because geth still does
the real execution, all of execute's funding / deploy / nonce / inclusion
logic works and the captured sequence is exactly what was broadcast.
"""

from __future__ import annotations

import base64
import contextlib
import json
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Iterator, List, Tuple
from urllib.parse import urlsplit, urlunsplit

# Some hosted RPC endpoints sit behind a WAF that 403s urllib's default
# "Python-urllib/x" User-Agent. Send a conventional one (requests, which
# `execute` uses directly, is not blocked).
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; eest-replay)"


def split_basic_auth(url: str) -> Tuple[str, Dict[str, str]]:
    """
    Split inline ``user:pass@`` credentials out of a URL.

    Returns ``(clean_url, headers)`` where ``headers`` carries an
    ``Authorization: Basic`` entry when the URL embedded credentials (urllib,
    unlike requests, does not apply them automatically). Hosted RPC endpoints
    commonly embed basic-auth creds in the URL.
    """
    parts = urlsplit(url)
    if not parts.username:
        return url, {}
    creds = f"{parts.username}:{parts.password or ''}"
    token = base64.b64encode(creds.encode()).decode()
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    clean = urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )
    return clean, {"Authorization": f"Basic {token}"}


class _Recorder:
    """Thread-safe ordered store of captured raw transactions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.raw_txs: List[str] = []

    def record(self, raw_tx: str) -> None:
        with self._lock:
            self.raw_txs.append(raw_tx)

    def snapshot(self) -> List[str]:
        with self._lock:
            return list(self.raw_txs)


def _extract_raw_txs(payload: object, recorder: _Recorder) -> None:
    """Record raw txs from a single or batched JSON-RPC request body."""
    calls = payload if isinstance(payload, list) else [payload]
    for call in calls:
        if not isinstance(call, dict):
            continue
        if call.get("method") != "eth_sendRawTransaction":
            continue
        params = call.get("params") or []
        if params and isinstance(params[0], str):
            recorder.record(params[0])


def _make_handler(target_url: str, recorder: _Recorder):
    clean_url, auth_headers = split_basic_auth(target_url)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: object) -> None:  # silence
            pass

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)

            req = urllib.request.Request(
                clean_url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": DEFAULT_USER_AGENT,
                    **auth_headers,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = resp.read()
                    status = resp.status
            except urllib.error.HTTPError as e:  # forward error bodies as-is
                data = e.read()
                status = e.code
            except Exception as exc:  # noqa: BLE001 - connection/timeout/DNS
                # geth is unreachable or slow: do NOT record (the tx was never
                # broadcast) and return a clean JSON-RPC error so the client
                # sees a failure instead of a torn keep-alive connection.
                self._send(
                    502,
                    json.dumps(
                        {
                            "jsonrpc": "2.0", "id": None,
                            "error": {
                                "code": -32000,
                                "message": f"proxy forward failed: {exc}",
                            },
                        }
                    ).encode(),
                )
                return

            # Record only after geth actually received the request, so the
            # captured sequence never contains a never-broadcast transaction.
            try:
                _extract_raw_txs(json.loads(body), recorder)
            except (ValueError, TypeError):
                pass  # never let recording break the proxy

            self._send(status, data)

        def _send(self, status: int, data: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def recording_proxy(target_url: str) -> Iterator[tuple[str, _Recorder]]:
    """
    Run a recording proxy in front of ``target_url``.

    Yields ``(proxy_url, recorder)``. The recorder's ``snapshot()`` returns the
    raw transactions captured so far, in submission order.
    """
    recorder = _Recorder()
    port = _free_port()
    server = ThreadingHTTPServer(
        ("127.0.0.1", port), _make_handler(target_url, recorder)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", recorder
    finally:
        server.shutdown()
        server.server_close()
