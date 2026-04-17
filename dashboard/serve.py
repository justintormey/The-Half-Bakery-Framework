#!/usr/bin/env python3
"""Half Bakery Dashboard — lightweight API server.

Serves the dashboard HTML and proxies /api/* routes to local data files.
Zero external dependencies (Python stdlib only).
"""

import json
import os
import re
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Path resolution (mirrors dispatcher.py constants)
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
CONFIG_DIR = REPO_DIR / "config"
STATE_DIR = Path.home() / ".half-bakery"
PROJECTS_CONTEXT = Path.home() / "PROJECTS_CONTEXT.md"


class DashboardHandler(BaseHTTPRequestHandler):
    """Routes: / serves HTML, /api/* reads data files."""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = parse_qs(parsed.query)

        if path == "" or path == "/":
            self._serve_file(SCRIPT_DIR / "index.html", "text/html")
        elif path == "/api/state":
            self._serve_file(STATE_DIR / "state.json", "application/json")
        elif path == "/api/log":
            try:
                tail = int(query.get("tail", ["100"])[0])
            except (ValueError, IndexError):
                tail = 100
            tail = max(1, min(tail, 10000))
            self._serve_log_tail(STATE_DIR / "logs" / "dispatcher.log", tail)
        elif path == "/api/projects":
            self._serve_file(PROJECTS_CONTEXT, "text/plain; charset=utf-8")
        elif path == "/api/config":
            self._serve_merged_config()
        elif path == "/api/fields":
            self._serve_file(STATE_DIR / "cache" / "project-fields.json", "application/json")
        elif path == "/api/usage":
            self._serve_usage()
        elif path.startswith("/api/output/"):
            safe = path[len("/api/output/"):]
            if re.match(r'^[a-zA-Z0-9][a-zA-Z0-9\-]*$', safe):
                self._serve_log_tail(STATE_DIR / "output" / f"{safe}.log", 200)
            else:
                self._send_error(400, "Invalid output ID")
        else:
            self._send_error(404, "Not found")

    def _serve_file(self, filepath, content_type):
        try:
            data = filepath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self._send_error(404, f"File not found: {filepath.name}")

    def _serve_log_tail(self, filepath, num_lines):
        try:
            size = filepath.stat().st_size
            with open(filepath, "rb") as f:
                # Read last 64KB to avoid loading huge files
                offset = max(0, size - 65536)
                f.seek(offset)
                chunk = f.read().decode("utf-8", errors="replace")
            lines = chunk.splitlines()
            if offset > 0:
                # First line is likely partial — drop it
                lines = lines[1:]
            tail = "\n".join(lines[-num_lines:])
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", len(tail.encode()))
            self.end_headers()
            self.wfile.write(tail.encode())
        except FileNotFoundError:
            self._send_error(404, f"File not found: {filepath.name}")

    def _serve_merged_config(self):
        try:
            with open(CONFIG_DIR / "dispatcher.json") as f:
                dispatcher = json.load(f)
            with open(CONFIG_DIR / "column-routes.json") as f:
                routes = json.load(f)
            merged = {**dispatcher, **routes}
            data = json.dumps(merged).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            self._send_error(500, f"Config file missing: {e}")

    def _serve_usage(self):
        """Serve current usage status from the usage tracker."""
        try:
            # Import from scripts directory
            scripts_dir = REPO_DIR / "scripts"
            sys.path.insert(0, str(scripts_dir))
            from usage_tracker import get_usage_status, get_weekly_summary
            status = get_usage_status()
            status["weekly"] = get_weekly_summary()
            data = json.dumps(status).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._send_error(500, f"Usage tracker error: {e}")

    def _send_error(self, code, message):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(message.encode())

    def log_message(self, format, *args):
        # Suppress default stderr logging — too noisy for a dashboard
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8484
    server = HTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"Dashboard server running at http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
