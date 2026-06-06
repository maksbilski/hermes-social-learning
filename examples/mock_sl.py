"""Tiny mock of the social-learning extraction service for local testing.

Run it in a separate terminal, then point the plugin at it via config.yaml:

    social_learning:
      service_url: "http://127.0.0.1:8899"

It accepts POST /v1/social-learning/extract, logs each hit (so you can SEE the
slow clock fire), and returns a 200 with a fake prompt_block (voice card).

    python plugins/social-learning/mock_sl.py            # default port 8899
    PORT=9000 python plugins/social-learning/mock_sl.py  # custom port
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        msgs = body.get("transcript", {}).get("messages", [])
        print(
            f"[mock] POST {self.path}  "
            f"X-API-Key={self.headers.get('X-API-Key')!r}  "
            f"messages={len(msgs)}",
            flush=True,
        )
        card = (
            "<voice_card>Reply casually and mirror the user's tone "
            f"(synthesized from {len(msgs)} messages).</voice_card>"
        )
        out = json.dumps({"profile": {"n": len(msgs)}, "prompt_block": card}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *_args) -> None:  # silence default access logging
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8899"))
    print(f"[mock] social-learning mock listening on http://127.0.0.1:{port}", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
