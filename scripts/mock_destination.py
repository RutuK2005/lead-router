"""A tiny stand-in for a CRM / n8n webhook, for watching deliveries locally.

    python scripts/mock_destination.py --port 9000 --fail-first 2

Prints every lead it receives. `--fail-first N` answers HTTP 503 to the first N requests
so you can watch the outbox retry with backoff before succeeding.
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


def make_handler(fail_first: int) -> type[BaseHTTPRequestHandler]:
    state = {"seen": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - name required by BaseHTTPRequestHandler
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state["seen"] += 1
            if state["seen"] <= fail_first:
                print(f"request {state['seen']}: simulating outage -> 503", flush=True)
                self.send_response(503)
                self.end_headers()
                return
            lead = json.loads(body).get("lead", {})
            intent = (lead.get("classification") or {}).get("intent")
            print(
                f"request {state['seen']}: got {lead.get('id')} intent={intent} "
                f"idempotency-key={self.headers.get('Idempotency-Key')}",
                flush=True,
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *args: object) -> None:  # silence default access log
            pass

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock destination webhook")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--fail-first", type=int, default=0)
    args = parser.parse_args()
    server = HTTPServer(("127.0.0.1", args.port), make_handler(args.fail_first))
    print(f"mock destination listening on http://127.0.0.1:{args.port}/hook")
    server.serve_forever()


if __name__ == "__main__":
    main()
