"""Smoke test of the API from inside the container: send a scribbled page
image, wait for the job, and print the result PDF's text layer.

Usage: api_roundtrip.py TOKEN [refused|keep]
With "refused" the job must fail with Mistral's error instead; with "keep"
the job is kept and its id printed last.
"""

import io
import random
import sys
import time

import httpx
import pypdfium2
from PIL import Image, ImageDraw

token = sys.argv[1]
refused = sys.argv[2:] == ["refused"]
keep = sys.argv[2:] == ["keep"]
# The API's links are paths from the server root, like /api/v1/jobs/ID.
api = httpx.Client(base_url="http://127.0.0.1:8080", headers={"Authorization": f"Bearer {token}"})

for _ in range(60):
    try:
        if api.get("/api/v1/jobs").status_code == 200:
            break
    except httpx.TransportError:
        pass
    time.sleep(1)
assert httpx.get("http://127.0.0.1:8080/api/v1/jobs").status_code == 401, "no token must mean no access"

# A scribble where a handwritten line would be.
random.seed(1)
image = Image.new("L", (1240, 1754), 255)
draw = ImageDraw.Draw(image)
draw.line([(130 + i * 10, 790 + random.randint(-20, 20)) for i in range(98)], fill=0, width=4)
buffer = io.BytesIO()
image.save(buffer, "PNG")

job = api.post("/api/v1/jobs", params={"filename": "notiz.png"}, content=buffer.getvalue()).json()
for _ in range(180):
    job = api.get(job["links"]["self"]).json()
    if job["status"] in ("done", "failed"):
        break
    time.sleep(1)
if refused:
    assert job["status"] == "failed" and "Mistral OCR failed on page 1" in job["error"], job
    print(job["error"])
    sys.exit(0)
assert job["status"] == "done", job
pdf = api.get(job["links"]["pdf"])
assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
result = api.get(job["links"]["json"]).json()
assert result["pages"][0]["lines_from_mistral"] == 1, result
print(pypdfium2.PdfDocument(pdf.content)[0].get_textpage().get_text_range())
if keep:
    print(job["id"])
    sys.exit(0)
assert api.delete(job["links"]["self"]).status_code == 204
