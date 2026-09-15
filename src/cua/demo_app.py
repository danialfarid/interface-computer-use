from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from urllib.parse import parse_qs, urlparse


# Synthetic values only. The app exists to exercise the UI seam, not to model a
# real financial system or accept real member data.
DEMO_MEMBERS: dict[str, dict[str, str]] = {
    "1001": {"name": "DEMO_MEMBER_1001", "balance": "$1,240.50"},
    "1002": {"name": "DEMO_MEMBER_1002", "balance": "$85.19"},
}


def _page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>{escape(title)}</title>
<style>
body {{ font: 16px Georgia, serif; margin: 2rem; color: #20252b; }}
table {{ border-collapse: collapse; min-width: 28rem; }}
td, th {{ border: 1px solid #9aa3ad; padding: .55rem; text-align: left; }}
th {{ background: #e9edf1; }}
input {{ font: inherit; padding: .35rem; }}
.notice {{ border: 1px solid #9a6b00; padding: .7rem; background: #fff7d6; }}
</style></head>
<body><h1>Member Services Console</h1>{body}</body></html>""".encode()


def home_page() -> bytes:
    return _page(
        "Member Services Console",
        """
        <p>Use the legacy member lookup form.</p>
        <form method="get" action="/member">
          <table>
            <tr><th><label for="member-number">Member ID</label></th>
                <td><input id="member-number" name="member" autocomplete="off"></td></tr>
            <tr><td colspan="2"><input type="submit" value="Search"></td></tr>
          </table>
        </form>
        """,
    )


def member_page(member_id: str) -> bytes:
    member = DEMO_MEMBERS.get(member_id)
    if member is None:
        return _page(
            "Member not found",
            f"""
            <div class="notice"><h2>Member not found</h2>
            <p>No member matched the supplied identifier.</p></div>
            <p><a href="/">Return to lookup</a></p>
            """,
        )
    return _page(
        "Member details",
        f"""
        <h2>Member details</h2>
        <table>
          <tr><th>Member name</th><td>{escape(member['name'])}</td></tr>
          <tr><th>Current savings balance</th><td id="balance-value">{escape(member['balance'])}</td></tr>
        </table>
        <p><a href="/">Return to lookup</a></p>
        """,
    )


class DemoRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        if parsed.path == "/":
            payload = home_page()
            status = 200
        elif parsed.path == "/member":
            member_id = parse_qs(parsed.query).get("member", [""])[0]
            payload = member_page(member_id)
            status = 200
        else:
            payload = _page("Not found", "<h2>Not found</h2>")
            status = 404
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def serve(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Start the demo app in a background thread and return its server."""

    server = ThreadingHTTPServer((host, port), DemoRequestHandler)
    import threading

    threading.Thread(target=server.serve_forever, name="cua-demo-app", daemon=True).start()
    return server


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8765), DemoRequestHandler)
    print("demo app listening on http://127.0.0.1:8765", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
