"""A local application-form server for testing the browser layer.

Four forms, because four things can happen and each must be exercised:

    /form/plain     an ordinary application form that can be filled and sent
    /form/captcha   a form behind a CAPTCHA -- must be refused, not solved
    /form/mfa       a form asking for a second factor -- must be refused
    /form/limited   returns HTTP 429 -- must be refused
    /fixture/<name> the real captured ATS markup from tests/fixtures/ats

stdlib http.server rather than Flask: this serves four static pages and one
POST, and adding a dependency to the image for that would be silly.

Run it directly for manual poking:  python -m tests.mock_forms.server 8099
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ats"

SUBMISSIONS: list[dict] = []

_FIELDS = """
  <label>Full name <input name="full_name" required></label>
  <label>Email <input name="email" type="email" required></label>
  <label>Resume <input name="resume" type="file"></label>
  <label>Why this role? <textarea name="why_this_role"></textarea></label>
"""

PAGES = {
    "/form/plain": f"""<!doctype html><meta charset=utf-8>
<title>Apply - Graduate Software Engineer</title>
<h1>Graduate Software Engineer</h1>
<form id="application" method="post" action="/submit">{_FIELDS}
  <button type="submit" id="submit">Submit application</button>
</form>""",

    "/form/captcha": f"""<!doctype html><meta charset=utf-8>
<title>Apply - Graduate Software Engineer</title>
<h1>Graduate Software Engineer</h1>
<script src="https://www.google.com/recaptcha/api.js"></script>
<form id="application" method="post" action="/submit">{_FIELDS}
  <div class="g-recaptcha" data-sitekey="test"></div>
  <p>Please verify you are human before continuing.</p>
  <button type="submit" id="submit">Submit application</button>
</form>""",

    "/form/mfa": """<!doctype html><meta charset=utf-8>
<title>Verify it's you</title>
<h1>Verify it's you</h1>
<p>Enter the one-time code we sent to your phone.</p>
<form method="post" action="/submit">
  <label>Code <input name="otp"></label>
  <button type="submit" id="submit">Continue</button>
</form>""",
}

LIMITED_BODY = ("<!doctype html><meta charset=utf-8><title>Slow down</title>"
                "<h1>Too many requests</h1><p>Try again in 60 seconds.</p>")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):           # quiet under pytest
        pass

    def _send(self, status: int, body: str, ctype="text/html; charset=utf-8"):
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/form/limited":
            return self._send(429, LIMITED_BODY)
        if path.startswith("/fixture/"):
            # The real captured ATS markup, served locally. Lets a real browser
            # drive a real form end to end without a single request reaching an
            # employer -- which is the only way to exercise the whole path
            # repeatedly without being a nuisance to somebody's careers site.
            name = path.rsplit("/", 1)[-1]
            f = FIXTURES / f"{name}.html"
            if not f.is_file() or ".." in name or "/" in name:
                return self._send(404, "<h1>no such fixture</h1>")
            return self._send(200, f.read_text(encoding="utf-8"))
        if path in PAGES:
            return self._send(200, PAGES[path])
        if path == "/submissions":
            return self._send(200, json.dumps(SUBMISSIONS), "application/json")
        self._send(404, "<h1>not found</h1>")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode()
        fields = {k: v[0] for k, v in parse_qs(raw).items()}
        SUBMISSIONS.append(fields)
        ref = f"MOCK-{len(SUBMISSIONS):06d}"
        self._send(200, f"<!doctype html><meta charset=utf-8><title>Received</title>"
                        f"<h1>Application received</h1><p>Reference {ref}</p>")


def serve(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Start on a background thread. Port 0 picks a free one, so tests never
    collide with something already listening."""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}"


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    httpd, base = serve(port)
    print(f"mock forms on {base}/form/plain")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        httpd.shutdown()
