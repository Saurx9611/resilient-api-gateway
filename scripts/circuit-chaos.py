"""Loopback-only circuit reproduction. At most 14 sequential gateway requests.

Build bin/gateway first and set REDIS_TEST_URL to an isolated loopback Redis.
No remote load target, HTTP fault control endpoint, or extra Python dependency.
"""
import contextlib
import http.server
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def main():
    endpoint = os.environ.get("REDIS_TEST_URL", "redis://127.0.0.1:6379/0")
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in ("redis", "rediss") or not parsed.hostname:
        raise SystemExit("Set REDIS_TEST_URL to isolated loopback Redis")
    try:
        if not ipaddress.ip_address(parsed.hostname).is_loopback:
            raise ValueError()
    except ValueError:
        raise SystemExit("Redis must use a literal loopback IP")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    mode = "failure"
    hits = 0

    class Upstream(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal hits
            hits += 1
            selected = mode
            if selected == "slow":
                time.sleep(6)  # exceeds the gateway's 5s response-header timeout
            self.send_response(500 if selected == "failure" else 200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    binary = root / "bin" / ("gateway.exe" if os.name == "nt" else "gateway")
    origin = f"http://127.0.0.1:{server.server_port}"
    env = os.environ.copy()
    env["REDIS_URL"] = endpoint
    args = [str(binary), "-listen", f"127.0.0.1:{port}",
            "-upstream-one", origin, "-upstream-two", origin,
            "-rate-namespace", "chaos" + uuid.uuid4().hex[:16],
            "-limit-one", "100", "-circuit-failures", "2",
            "-circuit-open", "250ms", "-circuit-recovery", "2"]
    print("Local circuit chaos: threshold=2, open=250ms, probes=1, recovery=2; not a benchmark", flush=True)
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with contextlib.ExitStack() as stack:
        stack.callback(server.server_close)
        stack.callback(server.shutdown)
        process = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, **flags)
        stack.callback(lambda: (process.terminate(), process.wait(timeout=5)))

        def request(path):
            start = time.monotonic()
            try:
                response = opener.open(f"http://127.0.0.1:{port}{path}", timeout=8)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                response.read()
                return response.status, (time.monotonic() - start) * 1000

        # Readiness polling uses sockets, not unbounded HTTP traffic.
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("gateway exited")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.1):
                    break
            except OSError:
                time.sleep(.05)
        else:
            raise RuntimeError("gateway startup timed out")

        def expect(label, expected, no_upstream=False):
            before = hits
            status, elapsed = request("/one")
            print(json.dumps(dict(step=label, status=status, elapsed_ms=round(elapsed, 2), upstream_hits=hits)), flush=True)
            assert status == expected, (label, status, expected)
            if no_upstream:
                assert hits == before, "Open request reached upstream"

        expect("failure 1", 500)
        expect("failure 2 opens", 500)
        for _ in range(3):
            expect("open rejects", 503, no_upstream=True)
        mode = "slow"
        time.sleep(.3)
        expect("half-open slow probe reopens", 504)
        expect("open after timeout", 503, no_upstream=True)
        mode = "healthy"
        time.sleep(.3)
        expect("recovery probe 1", 200)
        expect("recovery probe 2 closes", 200)
        expect("closed healthy", 200)
        print("PASS: 10 requests; failure/open/slow-probe/reopen/recovery. No public target used.", flush=True)


if __name__ == "__main__":
    main()
