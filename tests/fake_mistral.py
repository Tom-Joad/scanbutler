"""Stand-in for Mistral's /ocr endpoint for the smoke test (tests/smoke.sh).

Every page image is answered with one text block over the middle of the
page, as if it held a handwritten line that Tesseract can't read.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

TEXT = "Handgeschriebene Notiz Zimtschnecken"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ok = self.path == "/ocr" and body.get("include_blocks") and body["document"]["type"] == "image_url"
        page = {
            "index": 0,
            "markdown": TEXT,
            "blocks": [
                {"type": "text", "top_left_x": 0.1, "top_left_y": 0.4, "bottom_right_x": 0.9,
                 "bottom_right_y": 0.5, "content": TEXT}
            ],
        }
        data = json.dumps({"pages": [page]} if ok else {"detail": "unexpected request"}).encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", 8999), Handler).serve_forever()
