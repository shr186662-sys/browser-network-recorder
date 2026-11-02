"""Export already-recorded JS/CSS bodies locally. Never replays network requests."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit


def export_assets(session: Path) -> int:
    output = session / "assets"
    manifest = []
    output.mkdir(exist_ok=True)
    with (session / "requests.jsonl").open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            if record.get("redacted") or not record.get("response_body"):
                continue
            url = str(record.get("url") or "")
            kind = record.get("resource_type")
            path = urlsplit(url).path.lower()
            mime = str(record.get("mimeType") or "").lower()
            if kind == "Script" or path.endswith(".js") or "javascript" in mime:
                suffix = ".js"
            elif kind == "Stylesheet" or path.endswith(".css") or "text/css" in mime:
                suffix = ".css"
            else:
                continue
            body = record["response_body"]
            # URL and content hashes avoid collisions and untrusted path traversal.
            name = hashlib.sha256((url + "\n" + body).encode()).hexdigest()[:24] + suffix
            (output / name).write_text(body, encoding="utf-8")
            manifest.append({"file": name, "url": url, "truncated": "…[截断，原长 " in body})
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return len({item["file"] for item in manifest})
