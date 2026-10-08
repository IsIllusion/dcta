"""Tencent Hunyuan through its official OpenAI-compatible endpoint, used as an INDEPENDENT validator engine.

Running the Validation Agent on a different provider from the Business Agent removes a correlated failure
mode: one model's misreading is less likely to be repeated by a different model family. Same bounded
transport, no-redirect and credential-hygiene rules as the DeepSeek adapter.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from .domain import DomainError, canonical
from .deepseek import DeepSeekCompletion

HUNYUAN_BASE_URL = "https://api.hunyuan.cloud.tencent.com/v1"  # Tencent Cloud documentation, product 1729 / 111007


def load_hunyuan_key(key_file=None):
    if key_file is None:
        value = os.environ.get("HUNYUAN_API_KEY", "").strip()
        if value:
            if not re.fullmatch(r"[A-Za-z0-9_.-]{20,200}", value):
                raise DomainError("PROVIDER_CONFIG", "HUNYUAN_API_KEY has an invalid format.")
            return value
        key_file = os.environ.get("HUNYUAN_KEY_FILE")
    if key_file is None:
        raise DomainError("PROVIDER_CONFIG", "Set HUNYUAN_API_KEY or pass --validator-key-file for the Hunyuan validator.")
    try:
        path = Path(key_file)
        if path.stat().st_size > 4096:
            raise DomainError("PROVIDER_CONFIG", "Credential file is too large.")
        lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    except (OSError, UnicodeError):
        raise DomainError("PROVIDER_CONFIG", "Hunyuan credential file is unavailable.") from None
    if len(lines) != 1 or not re.fullmatch(r"[A-Za-z0-9_.-]{20,200}", lines[0]):
        raise DomainError("PROVIDER_CONFIG", "The Hunyuan credential file must contain exactly one API key line.")
    return lines[0]


@dataclass(frozen=True)
class HunyuanConfig:
    model: str = "hunyuan-turbos-latest"
    base_url: str = HUNYUAN_BASE_URL
    timeout_seconds: float = 20.0
    total_seconds: float = 45.0
    max_attempts: int = 2
    max_tokens: int = 2048

    def __post_init__(self):
        url = urlsplit(self.base_url)
        if (url.scheme != "https" or url.hostname != "api.hunyuan.cloud.tencent.com" or url.port not in (None, 443)
                or url.username or url.password or url.query or url.fragment or url.path.rstrip("/") != "/v1"):
            raise DomainError("PROVIDER_CONFIG", "Only the official HTTPS Hunyuan endpoint is permitted.")
        if not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", self.model):
            raise DomainError("PROVIDER_CONFIG", "Invalid model identifier.")
        if not 0 < self.timeout_seconds <= 60 or not 0 < self.total_seconds <= 120 or self.max_attempts not in (1, 2) or not 128 <= self.max_tokens <= 2048:
            raise DomainError("PROVIDER_CONFIG", "Provider request limits are invalid.")


class HunyuanCompletion(DeepSeekCompletion):
    label = "Hunyuan"

    def __init__(self, config=None, *, key_file=None, api_key=None):
        self.config = config or HunyuanConfig()
        self._api_key = api_key or load_hunyuan_key(key_file)
        self.name = "hunyuan/" + self.config.model
        self.last_metadata = {}
        self._turn_deadline = None

    def _payload(self, system, user, schema):
        # JSON mode is not a documented compatibility parameter, so the host's strict parser is the contract.
        return {"model": self.config.model, "messages": [
            {"role": "system", "content": system + "\nReturn only the JSON object, with no Markdown fence.\nJSON schema (enforced again by host):\n" + canonical(schema)},
            {"role": "user", "content": user}], "temperature": 0, "max_tokens": self.config.max_tokens, "stream": False}

    def _postprocess(self, content):
        # Tolerate exactly one surrounding ```json fence; anything else still goes to strict_json unchanged.
        match = re.fullmatch(r"\s*```(?:json)?\s*(\{.*\})\s*```\s*", content, re.S)
        return match.group(1) if match else content
