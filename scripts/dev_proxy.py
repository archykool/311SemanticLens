"""Dev-only reverse proxy so the preview browser can adopt the API.

The real service runs in Docker on :8000 (owned by docker's backend, which
preview tooling can't adopt). This forwards an auto-assigned PORT to it.
Not part of the demo stack — `docker compose up` serves the UI directly.
"""

import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "http://localhost:8000"


class Proxy(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            with urllib.request.urlopen(UPSTREAM + self.path, timeout=120) as r:
                body = r.read()
                self.send_response(r.status)
                for k, v in r.headers.items():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as e:
            body = e.read()
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:  # noqa: BLE001
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8090"))
    print(f"proxying :{port} -> {UPSTREAM}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Proxy).serve_forever()
