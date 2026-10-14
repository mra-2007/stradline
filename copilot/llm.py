"""A small adapter around the language model, and the cache in front of it.

Everything provider-specific is here, behind `complete(prompt)`. The provider
and the model name are configuration values in `strandline/config.py`
(Rule 3.7), the temperature is zero, and every response is cached by a hash of
the exact prompt - so an evaluation re-run neither pays for the network nor
depends on it, and two runs of the copilot over the same questions produce
byte-identical answers (Rule 5).

Without an API key, or on any error the back-off does not clear, `complete`
returns None and the caller falls back to the offline templates. That is a
degradation in WORDING only: the offline path answers the same question from
the same evidence, through the same verifier, with the same numbers.

The key is read from `.env`, which is in .gitignore and is never committed.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from strandline.config import CopilotParams

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

# Statuses that are worth another try: a rate limit, or the provider having a
# bad minute. A 400 or a 403 is our fault or our key's, and retrying it just
# wastes the quota.
RETRYABLE_STATUS = (408, 429, 500, 502, 503, 504)


@dataclass
class LlmResult:
    text: str | None
    source: str              # "cache" | "api" | "unavailable"
    model: str
    reason: str | None = None
    attempts: int = 0


def load_env(path: Path | None = None) -> dict:
    """Read KEY=VALUE lines from .env. No dependency, no surprises."""
    path = ENV_FILE if path is None else path
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def api_key(params: CopilotParams | None = None, env_file: Path | None = None) -> str | None:
    """The key from the process environment first, then from `.env`."""
    params = params or CopilotParams()
    return os.environ.get(params.api_key_env) or load_env(env_file).get(params.api_key_env)


def prompt_hash(prompt: str, params: CopilotParams) -> str:
    """Identity of a request: the prompt AND everything that shapes the reply.

    The model name and temperature are in the hash on purpose - a cached
    answer from another model is not this model's answer.
    """
    payload = json.dumps({"prompt": prompt, "provider": params.provider,
                          "model": params.model, "temperature": params.temperature,
                          "max_output_tokens": params.max_output_tokens}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def cache_path(prompt: str, params: CopilotParams, cache_dir: Path | None = None) -> Path:
    base = Path(cache_dir) if cache_dir is not None else ROOT / params.cache_dir
    return base / f"{prompt_hash(prompt, params)}.json"


def read_cache(prompt: str, params: CopilotParams, cache_dir: Path | None = None) -> str | None:
    path = cache_path(prompt, params, cache_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))["text"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def write_cache(prompt: str, text: str, params: CopilotParams,
                cache_dir: Path | None = None) -> None:
    path = cache_path(prompt, params, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model": params.model, "temperature": params.temperature,
                                "prompt": prompt, "text": text}, ensure_ascii=False,
                               indent=1), encoding="utf-8")


def _post(url: str, body: dict, timeout: float) -> tuple:
    """(status, text). Network and HTTP problems come back as a status, never
    as an exception: a copilot that crashes because an API blinked is worse
    than one that answers from its own templates."""
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", errors="replace")
    except Exception as err:                       # DNS, TLS, timeout, offline
        return 0, f"{type(err).__name__}: {err}"


def _gemini_text(payload: str) -> str | None:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    for candidate in data.get("candidates") or []:
        parts = ((candidate.get("content") or {}).get("parts")) or []
        text = "".join(part.get("text", "") for part in parts)
        if text.strip():
            return text
    return None


def complete(prompt: str, params: CopilotParams | None = None, *,
             cache_dir: Path | None = None, env_file: Path | None = None,
             sleep=time.sleep, post=_post) -> LlmResult:
    """One completion. Cache, then provider, then give up cleanly."""
    params = params or CopilotParams()
    cached = read_cache(prompt, params, cache_dir)
    if cached is not None:
        return LlmResult(text=cached, source="cache", model=params.model)
    if params.provider == "offline":
        return LlmResult(text=None, source="unavailable", model=params.model,
                         reason="the provider is configured as 'offline'")
    key = api_key(params, env_file)
    if not key:
        return LlmResult(text=None, source="unavailable", model=params.model,
                         reason=f"no {params.api_key_env} in the environment or .env")

    url = params.endpoint.format(model=params.model) + f"?key={key}"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": params.temperature,
                             "maxOutputTokens": params.max_output_tokens,
                             "candidateCount": 1},
    }
    last = "no attempt was made"
    for attempt in range(len(params.retry_backoff_s) + 1):
        status, payload = post(url, body, params.timeout_s)
        if status == 200:
            text = _gemini_text(payload)
            if text is not None:
                write_cache(prompt, text, params, cache_dir)
                return LlmResult(text=text, source="api", model=params.model,
                                 attempts=attempt + 1)
            last = "the provider returned no usable text"
        else:
            last = f"HTTP {status}: {payload[:200]}"
            if status not in RETRYABLE_STATUS and status != 0:
                break
        if attempt < len(params.retry_backoff_s):
            sleep(params.retry_backoff_s[attempt])
    return LlmResult(text=None, source="unavailable", model=params.model, reason=last,
                     attempts=len(params.retry_backoff_s) + 1)
