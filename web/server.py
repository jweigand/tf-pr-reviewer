"""The review queue: one page and one JSON endpoint, both reading Postgres.

Standard library http.server on purpose. There is no routing, no templating and no
framework to explain, which matters because every line here has to be defensible. The
whole thing is two routes and one query.

Read-only. Nothing here writes, so the page cannot corrupt a review.
"""

import datetime
import decimal
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg

log = logging.getLogger("web")

DSN = os.environ.get(
    "POSTGRES_DSN",
    "postgres://{u}:{p}@postgres:5432/{d}".format(
        u=os.environ.get("POSTGRES_USER", "tfpr"),
        p=os.environ.get("POSTGRES_PASSWORD", "tfpr"),
        d=os.environ.get("POSTGRES_DB", "tfpr"),
    ),
)
PORT = int(os.environ.get("WEB_PORT", "8080"))
INDEX = Path(__file__).parent / "index.html"

# worst_rank is a stored generated column: 0 red, 1 yellow, 2 green, 3 not reviewed.
# Sorting on it is what makes this a queue rather than a list.
QUERY = """
    SELECT repo, pr_number, title, author, html_url, head_sha,
           status, reason, impact_color, security_color, worst_rank,
           impact, security, model, model_seconds, retried, prompt_tokens,
           reviewed_at
    FROM reviews
    ORDER BY worst_rank, reviewed_at DESC
"""


def json_safe(value):
    """Postgres types the standard json module does not know.

    NUMERIC comes back as Decimal and TIMESTAMPTZ as datetime. Handled here in one
    place rather than per column, so adding a numeric or timestamp column later does
    not silently break the endpoint.
    """
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    raise TypeError(f"cannot serialise {type(value).__name__}")


def rows() -> list[dict]:
    with psycopg.connect(DSN, connect_timeout=5) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(QUERY)
            return cur.fetchall()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        try:
            if path == "/api/reviews":
                body = json.dumps(rows(), default=json_safe).encode()
                self._send(200, body, "application/json")
            elif path in ("/", "/index.html"):
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            elif path == "/health":
                self._send(200, b"ok", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as exc:
            # A database that is not up yet is the common case on a cold start, and the
            # page should say so rather than showing a blank screen.
            log.exception("request failed: %s", path)
            self._send(
                503,
                json.dumps({"error": str(exc)}).encode(),
                "application/json",
            )

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
        stream=sys.stdout,
    )
    log.info("listening on :%s", PORT)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
