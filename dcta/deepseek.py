"""DeepSeek's official JSON chat API, with bounded calls and no credential logging."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .domain import DomainError, canonical


def load_api_key(key_file=None):
    # A supplied file takes precedence; do not silently use another account's env key.
    if key_file is None:
        value = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if value:
            if not re.fullmatch(r"sk-[A-Za-z0-9_-]{16,}", value):
                raise DomainError("PROVIDER_CONFIG", "DEEPSEEK_API_KEY has an invalid format.")
            return value
        key_file = os.environ.get("DEEPSEEK_KEY_FILE")
    if key_file is None:
        root = Path(__file__).resolve().parents[2]
        key_file = root / 'keys.txt'
        if not key_file.is_file():
            key_file = root / 'keys' / 'keys.txt'
    try:
        path = Path(key_file)
        if path.stat().st_size > 65536:
            raise DomainError("PROVIDER_CONFIG", "Credential file is too large.")
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise DomainError("PROVIDER_CONFIG", "DeepSeek credential file is unavailable; set DEEPSEEK_API_KEY or provide --key-file.") from None
    candidates = set(re.findall(r"\bsk-[A-Za-z0-9_-]{16,}\b", text))
    if len(candidates) != 1:
        raise DomainError("PROVIDER_CONFIG", "Credential file must contain exactly one unambiguous API key.")
    return candidates.pop()


@dataclass(frozen=True)
class DeepSeekConfig:
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    timeout_seconds: float = 20.0
    total_seconds: float = 45.0
    max_attempts: int = 2
    max_tokens: int = 2048

    def __post_init__(self):
        url = urlsplit(self.base_url)
        if url.scheme != "https" or url.hostname != "api.deepseek.com" or url.port not in (None, 443) or url.username or url.password or url.query or url.fragment or url.path.rstrip("/") not in ("", "/v1"):
            raise DomainError("PROVIDER_CONFIG", "Only the official HTTPS DeepSeek endpoint is permitted.")
        if not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", self.model):
            raise DomainError("PROVIDER_CONFIG", "Invalid model identifier.")
        if not 0 < self.timeout_seconds <= 60 or not 0 < self.total_seconds <= 120 or self.max_attempts not in (1, 2) or not 128 <= self.max_tokens <= 2048:
            raise DomainError("PROVIDER_CONFIG", "Provider request limits are invalid.")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward an Authorization header to a redirect destination.


_workers = threading.BoundedSemaphore(4)


from .observability import observed, emit


class DeepSeekCompletion:
    def __init__(self, config=None, *, key_file=None, api_key=None):
        self.config = config or DeepSeekConfig()
        self._api_key = api_key or load_api_key(key_file)
        self.name = "deepseek/" + self.config.model
        self.last_metadata = {}
        self._turn_deadline = None

    def begin_budget(self):
        # Optional router + business interpretation share one existing 45-second budget.
        self._turn_deadline = time.monotonic() + self.config.total_seconds

    def _request(self, payload, timeout):
        request = Request(self.config.base_url.rstrip("/") + "/chat/completions", data=canonical(payload).encode("utf-8"),
                          headers={"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"}, method="POST")
        try:
            with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                data = response.read(65537)
                if len(data) > 65536:
                    return ("error", "PROVIDER_RESPONSE_SIZE")
                return ("response", data)
        except HTTPError as error:
            # No server body/reason/header is copied to user logs or exceptions.
            status = error.code
            error.close()
            return ("http", status)
        except (URLError, TimeoutError, OSError):
            return ("error", "PROVIDER_NETWORK")

    def _bounded_request(self, payload, timeout):
        if not _workers.acquire(blocking=False):
            return ("error", "PROVIDER_BUSY")
        result = queue.Queue(maxsize=1)
        def run():
            try:
                result.put(self._request(payload, timeout))
            except Exception:
                result.put(("error", "PROVIDER_NETWORK"))
            finally:
                _workers.release()
        threading.Thread(target=run, daemon=True).start()
        try:
            return result.get(timeout=timeout)
        except queue.Empty:
            # Late worker returns are discarded and have no access to draft/ledger state.
            return ("error", "PROVIDER_TIMEOUT")

    @observed('provider', 'deepseek_completion', provider=True)
    def complete(self, system, user, schema):
        started = time.monotonic()
        self.last_metadata = {"model_requested": self.config.model, "attempts": 0, "status": "STARTED", "max_tokens": self.config.max_tokens}
        payload = self._payload(system, user, schema)
        for attempt in range(self.config.max_attempts):
            remaining = self.config.total_seconds - (time.monotonic() - started)
            if self._turn_deadline is not None:
                remaining = min(remaining, self._turn_deadline - time.monotonic())
            if remaining <= 0:
                return self._fail("PROVIDER_TIMEOUT")
            self.last_metadata["attempts"] = attempt + 1
            emit('provider', 'attempt', {'attempt': attempt + 1})
            kind, value = self._bounded_request(payload, min(self.config.timeout_seconds, remaining))
            if kind == "http":
                if value in (429, 500, 502, 503, 504) and attempt + 1 < self.config.max_attempts:
                    time.sleep(min(0.25, max(0, self.config.total_seconds - (time.monotonic() - started))))
                    continue
                return self._fail({401: "PROVIDER_AUTH", 402: "PROVIDER_BALANCE", 429: "PROVIDER_RATE_LIMIT"}.get(value, "PROVIDER_HTTP_ERROR"), status=value)
            if kind == "error":
                return self._fail(value)
            try:
                result = json.loads(value)
                if type(result) is not dict:
                    raise ValueError('Invalid response object')
                choices = result["choices"]
                if type(choices) is not list or any(type(choice) is not dict for choice in choices):
                    raise ValueError('Invalid choices')
                usage = result.get('usage', {})
                self.last_metadata['usage'] = {key: usage[key] for key in ('prompt_tokens','completion_tokens','total_tokens')
                                              if type(usage) is dict and type(usage.get(key)) is int and 0 <= usage[key] <= 100000000}
                self.last_metadata['choice_count'] = len(choices)
                finish = choices[0].get('finish_reason') if len(choices)==1 else None
                self.last_metadata['finish_reason'] = finish if finish in ('stop','length','content_filter','tool_calls','insufficient_system_resource') else 'unknown'
                if len(choices) != 1 or choices[0]["finish_reason"] != "stop":
                    return self._fail("PROVIDER_INCOMPLETE")
                message = choices[0]["message"]
                if message.get("tool_calls"):
                    return self._fail("PROVIDER_UNEXPECTED_TOOL")
                content = message["content"]
                if type(content) is not str or not content.strip() or len(content) > 16000:
                    return self._fail("PROVIDER_EMPTY_OR_OVERSIZE")
                content = self._postprocess(content)
                self.last_metadata.update({"status": "OK", "latency_seconds": round(time.monotonic() - started, 4),
                                           "model_returned": str(result.get("model", ""))[:100],
                                           "system_fingerprint": str(result.get("system_fingerprint", ""))[:100]})
                return content
            except (ValueError, TypeError, KeyError, IndexError):
                return self._fail("PROVIDER_PROTOCOL")
        return self._fail("PROVIDER_UNAVAILABLE")

    label = "DeepSeek"

    def _payload(self, system, user, schema):
        return {"model": self.config.model, "messages": [
            {"role": "system", "content": system + "\nJSON schema (enforced again by host):\n" + canonical(schema)},
            {"role": "user", "content": user}],
            "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"},
            "temperature": 0, "max_tokens": self.config.max_tokens, "stream": False}

    def _postprocess(self, content):
        return content

    def _fail(self, code, status=None):
        self.last_metadata.update({"status": code})
        if status is not None:
            self.last_metadata["http_status"] = status
        messages = {"PROVIDER_AUTH": f"{self.label} authentication failed; check the local credential.",
                    "PROVIDER_BALANCE": f"{self.label} reported insufficient API balance.",
                    "PROVIDER_TIMEOUT": f"{self.label} timed out; no draft change was applied."}
        raise DomainError(code, messages.get(code, f"{self.label} could not produce a valid result; no draft change was applied."))
