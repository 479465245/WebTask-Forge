from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from db import NetworkEvent, Session, utcnow

logger = logging.getLogger(__name__)

REQUEST_BODY_LIMIT = 64 * 1024
RESPONSE_BODY_LIMIT = 256 * 1024
SENSITIVE_NAME = re.compile(r"(?:authorization|cookie|token|secret|password|passwd|api[_-]?key|session|credential|signature|access[_-]?key)", re.I)
TEXT_CONTENT = re.compile(r"(?:^text/|json|xml|javascript|x-www-form-urlencoded|graphql)", re.I)


def _safe_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        query = [(key, "[REDACTED]" if SENSITIVE_NAME.search(key) else val)
                 for key, val in parse_qsl(parts.query, keep_blank_values=True)]
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, urlencode(query), ""))[:4096]
    except (ValueError, TypeError):
        return "[invalid URL]"


def _safe_headers(headers: dict) -> dict[str, str]:
    result = {}
    for name, value in headers.items():
        key = str(name)[:200]
        result[key] = "[REDACTED]" if SENSITIVE_NAME.search(key) else str(value)[:8192]
    return result


def _safe_text(value: str, content_type: str) -> str:
    value = value[:RESPONSE_BODY_LIMIT]
    if "json" in content_type.lower():
        try:
            return json.dumps(_redact_json(json.loads(value)), ensure_ascii=False)
        except (ValueError, TypeError):
            pass
    if "x-www-form-urlencoded" in content_type.lower():
        try:
            pairs = [(key, "[REDACTED]" if SENSITIVE_NAME.search(key) else item)
                     for key, item in parse_qsl(value, keep_blank_values=True)]
            return urlencode(pairs)
        except ValueError:
            pass
    return re.sub(r"(?i)([\"']?(?:authorization|cookie|token|secret|password|api[_-]?key)[\"']?\s*[:=]\s*[\"']?)[^\s,;&\"']+",
                  r"\1[REDACTED]", value)


def _redact_json(value):
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if SENSITIVE_NAME.search(str(key)) else _redact_json(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    return value


def _body_text(value: str | None, headers: dict, limit: int) -> tuple[str | None, bool, str | None]:
    if value is None:
        return None, False, None
    content_type = next((str(v) for k, v in headers.items() if k.lower() == "content-type"), "")
    if not TEXT_CONTENT.search(content_type):
        return None, False, "non_text_content"
    encoded = value.encode("utf-8", errors="replace")
    truncated = len(encoded) > limit
    value = encoded[:limit].decode("utf-8", errors="replace")
    return _safe_text(value, content_type), truncated, None


class NetworkCapture:
    """Captures sanitized HTTP traffic for one browser run."""

    def __init__(self, run_id: int):
        self.run_id = run_id
        self._events: list[dict] = []
        self._by_request: dict[int, dict] = {}
        self._insert_tasks: dict[int, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._condition = asyncio.Condition()
        self.revision = 0
        self._closed = False

    def attach(self, page) -> None:
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_request_failed)

    def _spawn(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _on_request(self, request) -> None:
        if self._closed or not request.url.startswith(("http://", "https://")):
            return
        headers = _safe_headers(request.headers)
        request_body, truncated, skipped = _body_text(request.post_data, headers, REQUEST_BODY_LIMIT)
        key = id(getattr(request, "_impl_obj", request))
        record = {"id": None, "capture_index": len(self._events) + 1, "run_id": self.run_id,
                  "url": _safe_url(request.url), "method": str(request.method)[:32],
                  "resource_type": str(request.resource_type)[:80], "started_at": utcnow().isoformat(),
                  "request_headers": headers, "request_body": request_body,
                  "request_body_truncated": truncated, "request_body_skipped": skipped,
                  "status": "pending", "response_status": None, "response_status_text": None,
                  "response_headers": {}, "response_body": None, "response_body_truncated": False,
                  "response_body_skipped": None, "response_body_bytes": None,
                  "failure": None, "finished_at": None}
        self._events.append(record)
        self._by_request[key] = record
        self._insert_tasks[key] = self._spawn(self._insert(key, record))
        self._spawn(self._changed())

    async def _insert(self, key: int, record: dict) -> None:
        try:
            with Session() as session:
                event = NetworkEvent(run_id=self.run_id, capture_index=record["capture_index"],
                    url=record["url"], method=record["method"], resource_type=record["resource_type"],
                    started_at=datetime.fromisoformat(record["started_at"]),
                    request_headers=record["request_headers"], request_body=record["request_body"],
                    request_body_truncated=record["request_body_truncated"],
                    request_body_skipped=record["request_body_skipped"], status="pending")
                session.add(event)
                session.commit()
                record["id"] = event.id
        except Exception as error:
            # Report the failure class only; exception details may contain traffic data.
            logger.warning("network capture insert failed run_id=%s error_type=%s",
                           self.run_id, type(error).__name__)
            record["persistence_error"] = True
        finally:
            await self._changed()

    def _on_response(self, response) -> None:
        key = id(getattr(response.request, "_impl_obj", response.request))
        record = self._by_request.get(key)
        if record:
            self._spawn(self._finish_response(key, record, response))

    async def _finish_response(self, key: int, record: dict, response) -> None:
        headers = _safe_headers(response.headers)
        content_type = next((v for k, v in headers.items() if k.lower() == "content-length"), None)
        expected = int(content_type) if content_type and content_type.isdigit() else None
        record.update(status="completed", response_status=response.status,
                      response_status_text=str(response.status_text)[:200], response_headers=headers,
                      response_body_bytes=expected, finished_at=utcnow().isoformat())
        response_type = next((str(v) for k, v in headers.items() if k.lower() == "content-type"), "")
        if not TEXT_CONTENT.search(response_type):
            record["response_body_skipped"] = "non_text_content"
        elif expected is not None and expected > RESPONSE_BODY_LIMIT:
            record["response_body_skipped"] = "content_length_over_limit"
            record["response_body_truncated"] = True
        else:
            try:
                body = await response.body()
                record["response_body_bytes"] = len(body)
                decoded = body[:RESPONSE_BODY_LIMIT].decode("utf-8", errors="replace")
                record["response_body_truncated"] = len(body) > RESPONSE_BODY_LIMIT
                record["response_body"] = _safe_text(decoded, response_type)
            except Exception:
                record["response_body_skipped"] = "unavailable"
        await self._update_database(key, record)

    def _on_request_failed(self, request) -> None:
        key = id(getattr(request, "_impl_obj", request))
        record = self._by_request.get(key)
        if record:
            record.update(status="failed", failure=_safe_text(str(request.failure or "request failed")[:1000], "text/plain"),
                          finished_at=utcnow().isoformat())
            self._spawn(self._update_database(key, record))

    async def _update_database(self, key: int, record: dict) -> None:
        task = self._insert_tasks.get(key)
        if task:
            await task
        if record.get("id") is None:
            await self._changed()
            return
        try:
            with Session() as session:
                event = session.get(NetworkEvent, record["id"])
                if event:
                    for field in ("status", "response_status", "response_status_text", "response_headers",
                                  "response_body", "response_body_truncated", "response_body_skipped",
                                  "response_body_bytes", "failure", "finished_at"):
                        value = record.get(field)
                        if field == "finished_at" and value:
                            value = datetime.fromisoformat(value)
                        setattr(event, field, value)
                    session.commit()
        except Exception as error:
            logger.warning("network capture update failed run_id=%s error_type=%s",
                           self.run_id, type(error).__name__)
            record["persistence_error"] = True
        await self._changed()

    async def _changed(self) -> None:
        async with self._condition:
            self.revision += 1
            self._condition.notify_all()

    def get_events(self, after_index: int = 0, limit: int = 500) -> list[dict]:
        return json.loads(json.dumps([event for event in self._events
                                      if event["capture_index"] > max(0, after_index)][:max(1, min(limit, 500))]))

    async def wait_for_events(self, after_revision: int, timeout: float = 10) -> int:
        timeout = max(0.1, min(float(timeout), 30))
        try:
            async with self._condition:
                await asyncio.wait_for(self._condition.wait_for(lambda: self.revision > after_revision), timeout)
        except asyncio.TimeoutError:
            pass
        return self.revision

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._by_request.clear()
        self._insert_tasks.clear()
