"""Read-only server for Gameio add-on files. Put it behind HTTPS (a reverse proxy or tunnel).

Serves shard files at <root>/<addon>/<version>/<xx>.json and the importable manifests as
root-level .json files. Everything else is a 404, so exports and this directory's other
files are never exposed.

    ADDONS_ROOT=./public ADDONS_HOST=127.0.0.1 ADDONS_PORT=3100 python3 tools/serve.py

Layout under ADDONS_ROOT:
    my-addon.json                     the manifest users import
    my-addon/1/00.json ... ff.json    that version's shards
"""

import http.server
import os
import re
from pathlib import Path

ROOT = Path(os.environ.get("ADDONS_ROOT", Path(__file__).resolve().parent / "public"))
# Dots are allowed inside a segment (versions, file names) but "." and ".." never are.
SEGMENT = "(?!\\.{1,2}$)[A-Za-z0-9._-]{1,100}"
SHARD_PATH = re.compile(rf"^(?:/{SEGMENT}){{1,3}}/[0-9a-f]{{2}}\.json$")
MANIFEST_PATH = re.compile(rf"^/{SEGMENT}\.json$")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "gameio-addons"
    sys_version = ""

    def _send(self, head_only: bool) -> None:
        path = self.path.split("?", 1)[0]
        is_manifest = bool(MANIFEST_PATH.match(path))
        if not (is_manifest or SHARD_PATH.match(path)):
            self.send_error(404)
            return
        file = (ROOT / path.lstrip("/")).resolve()
        if not file.is_relative_to(ROOT.resolve()) or not file.is_file():
            self.send_error(404)
            return
        body = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if is_manifest:
            # Always names the newest snapshot, so browsers must not keep an old copy.
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Disposition", f'attachment; filename="{path.lstrip("/")}"')
        else:
            # Versions are immutable: a new snapshot gets a new directory.
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def do_GET(self) -> None:
        self._send(head_only=False)

    def do_HEAD(self) -> None:
        self._send(head_only=True)

    def log_message(self, format: str, *args) -> None:
        pass


if __name__ == "__main__":
    address = (os.environ.get("ADDONS_HOST", "127.0.0.1"), int(os.environ.get("ADDONS_PORT", "3100")))
    http.server.ThreadingHTTPServer(address, Handler).serve_forever()
