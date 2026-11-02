"""Best-effort redaction for local capture files; not an anonymization guarantee."""
from __future__ import annotations
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

HIDDEN = "[REDACTED]"
SENSITIVE = re.compile(r"password|passwd|secret|token|authorization|cookie|session|credential|api.?key|private.?key|phone|mobile|email|identity|id.?card|验证码|密码|手机|姓名", re.I)
SAFE_HEADERS = {"content-type", "content-length", "accept", "accept-encoding", "cache-control", "expires", "date", "connection", "pragma", "vary", "user-agent"}


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.netloc.rsplit("@", 1)[-1]
        # Query values and fragments may contain credentials under arbitrary names.
        query = urlencode([(key, HIDDEN) for key, _ in parse_qsl(parts.query, keep_blank_values=True)])
        return urlunsplit((parts.scheme, host, parts.path, query, HIDDEN if parts.fragment else ""))
    except ValueError:
        return HIDDEN


def redact_json(value):
    if isinstance(value, dict):
        return {key: HIDDEN if SENSITIVE.search(str(key)) else redact_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    return value


def redact_body(body: str, content_type: str) -> str:
    if not body:
        return body
    try:
        value = json.loads(body)
        if isinstance(value, (dict, list)):
            return json.dumps(redact_json(value), ensure_ascii=False)
    except (ValueError, TypeError):
        pass
    if "application/x-www-form-urlencoded" in content_type.lower():
        return urlencode([(key, HIDDEN) for key, _ in parse_qsl(body, keep_blank_values=True)])
    return "[OMITTED: non-JSON body in redacted mode]"


def redact_record(record: dict) -> dict:
    result = dict(record)
    result["url"] = redact_url(str(record.get("url") or ""))
    for side in ("request", "response"):
        headers = record.get(f"{side}_headers") or {}
        content_type = next((str(v) for k, v in headers.items() if k.lower() == "content-type"), "")
        result[f"{side}_headers"] = {k: v if k.lower() in SAFE_HEADERS else HIDDEN for k, v in headers.items()}
        key = f"{side}_body"
        if key in result:
            result[key] = redact_body(str(record[key]), content_type)
    for key in ("payload_base64", "payload_utf8_preview", "payload_text", "response_body_error", "error_message"):
        if key in result:
            result[key] = HIDDEN
    result["redacted"] = True
    return result
