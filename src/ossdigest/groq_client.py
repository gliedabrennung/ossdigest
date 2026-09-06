from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

logger = structlog.get_logger().bind(component="groq")

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

_BACKOFF_SCHEDULE = (1.0, 4.0, 16.0)


class GroqDailyLimitExceeded(Exception):
    pass


class GroqCallFailed(Exception):
    pass


@dataclass
class GroqResult:
    content: str
    model_used: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    raw_response: dict[str, Any]


def strip_json_fences(text: str) -> str:
    return _JSON_FENCE_RE.sub("", text.strip()).strip()


def _is_exhausted_quota(resp: httpx.Response) -> bool:
    return resp.headers.get("x-ratelimit-remaining-requests") == "0"


class GroqClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.groq.com/openai/v1",
        timeout: float = 90.0,
        max_retries: int = 3,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout)
        self._max_retries = max_retries

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GroqClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _post_once(self, body: dict[str, Any]) -> GroqResult | Exception:
        last_exc: Exception = GroqCallFailed("нет попыток")
        for attempt, wait_s in enumerate((0.0, *_BACKOFF_SCHEDULE[: self._max_retries]), start=1):
            if wait_s:
                await asyncio.sleep(wait_s)
            try:
                resp = await self._client.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning("groq_network_error", attempt=attempt, error=str(exc))
                continue

            if resp.status_code == 429:
                if _is_exhausted_quota(resp):
                    return GroqDailyLimitExceeded(resp.text[:500])
                logger.warning("groq_rate_limited", attempt=attempt)
                last_exc = GroqCallFailed(f"429: {resp.text[:300]}")
                continue
            if resp.status_code >= 500:
                logger.warning("groq_server_error", status=resp.status_code, attempt=attempt)
                last_exc = GroqCallFailed(f"{resp.status_code}: {resp.text[:300]}")
                continue
            if resp.status_code != 200:
                return GroqCallFailed(f"{resp.status_code}: {resp.text[:500]}")

            data = resp.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            usage = data.get("usage", {}) or {}
            return GroqResult(
                content=content,
                model_used=data.get("model", body["model"]),
                tokens_in=usage.get("prompt_tokens", 0),
                tokens_out=usage.get("completion_tokens", 0),
                cost_usd=0.0,
                raw_response=data,
            )
        return last_exc

    async def chat_completion(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        fallback_model: str | None = None,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 700,
        reasoning_effort: str | None = None,
    ) -> GroqResult:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            body["response_format"] = response_format
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort

        result = await self._post_once(body)
        if isinstance(result, GroqDailyLimitExceeded):
            raise result
        if isinstance(result, Exception):
            if fallback_model:
                logger.warning("groq_falling_back", primary=model, fallback=fallback_model, error=str(result))
                fallback_body = {**body, "model": fallback_model}
                fallback_result = await self._post_once(fallback_body)
                if isinstance(fallback_result, GroqDailyLimitExceeded):
                    raise fallback_result
                if isinstance(fallback_result, Exception):
                    raise GroqCallFailed(f"основная и резервная модель провалились: {fallback_result}") from result
                return fallback_result
            raise result
        return result


def parse_structured_json(text: str) -> dict[str, Any]:
    return json.loads(strip_json_fences(text))
