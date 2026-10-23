from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import config


class ProviderError(Exception):
    def __init__(self, provider: str, message: str, status: int | None = None):
        super().__init__(message)
        self.provider = provider
        self.message = message
        self.status = status

    def __str__(self) -> str:
        suffix = f" (HTTP {self.status})" if self.status else ""
        return f"{self.provider}: {self.message}{suffix}"


@dataclass
class TextCall:
    model: str
    content: str
    reasoning: str | None
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DecisionsCall:
    model: str
    answers: dict[str, Any]
    provider: str | None
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float
    raw: dict[str, Any] = field(default_factory=dict)


def _error_message(response: httpx.Response, provider: str) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:500] or "empty error body"
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error)
    if error:
        return str(error)
    return response.text[:500]


class NamClient:
    """OpenAI-compatible chat completions through the Nam gateway."""

    provider = "Nam"

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or config.NAN_BASE_URL).rstrip("/")
        self.api_key = api_key or config.NAN_API_KEY

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ProviderError(self.provider, "NAN_API_KEY is not set")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def complete(
        self,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int = 1600,
        temperature: float = 0.0,
        timeout: float = 180.0,
    ) -> TextCall:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise ProviderError(self.provider, f"request failed: {exc}") from exc
        latency_ms = (time.perf_counter() - started) * 1000
        if response.status_code >= 400:
            raise ProviderError(
                self.provider, _error_message(response, self.provider), response.status_code
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(self.provider, "response is not JSON") from exc
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(self.provider, "response has no choices")
        message = choices[0].get("message") or {}
        usage = data.get("usage") or {}
        return TextCall(
            model=data.get("model", model),
            content=message.get("content") or "",
            reasoning=message.get("reasoning_content")
            or (message.get("provider_specific_fields") or {}).get("reasoning_content"),
            latency_ms=latency_ms,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            cost=float(usage.get("cost") or 0.0),
            raw=data,
        )


class OpenRouterDecisionsClient:
    """OpenRouter Decisions alpha endpoint."""

    provider = "OpenRouter"

    def __init__(self, api_key: str | None = None, url: str | None = None):
        self.api_key = api_key or config.OPENROUTER_API_KEY
        self.url = url or config.OPENROUTER_DECISIONS_URL

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ProviderError(self.provider, "OPENROUTER_API_KEY is not set")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Title": "Wu Card Game Race Control",
        }

    async def decide(
        self,
        payload: dict[str, Any],
        timeout: float = 180.0,
    ) -> DecisionsCall:
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
                response = await client.post(
                    self.url,
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise ProviderError(self.provider, f"request failed: {exc}") from exc
        latency_ms = (time.perf_counter() - started) * 1000
        if response.status_code >= 400:
            raise ProviderError(
                self.provider, _error_message(response, self.provider), response.status_code
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(self.provider, "response is not JSON") from exc
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise ProviderError(self.provider, "response has no answers object")
        usage = data.get("usage") or {}
        return DecisionsCall(
            model=data.get("model", payload.get("model", "")),
            answers=answers,
            provider=data.get("provider"),
            latency_ms=latency_ms,
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            total_tokens=int(usage.get("input_tokens") or 0)
            + int(usage.get("output_tokens") or 0),
            cost=float(usage.get("cost") or 0.0),
            raw=data,
        )


async def list_nam_models(timeout: float = 20.0) -> list[dict[str, Any]]:
    if not config.NAN_API_KEY:
        return []
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            f"{config.NAN_BASE_URL}/models",
            headers={"Authorization": f"Bearer {config.NAN_API_KEY}"},
        )
    if response.status_code >= 400:
        raise ProviderError("Nam", _error_message(response, "Nam"), response.status_code)
    data = response.json()
    models = []
    for item in data.get("data", []):
        model_id = item.get("id")
        if not model_id:
            continue
        if any(marker in model_id.lower() for marker in config.NON_CHAT_MARKERS):
            continue
        models.append({"id": model_id, "owned_by": item.get("owned_by", "")})
    return sorted(models, key=lambda item: item["id"])


FALLBACK_DECISION_MODELS = [
    "baseten/mammoth-1:free",
    "baseten/elephant-1:free",
    "typesafe/jev-1.14",
    "perplexity/pplx-decider-v1.2",
    "perplexity/pplx-decider-flash-v1.2",
    "nace-ai/drex-max",
    "respan/span-02",
    "liquid/d1-ultra",
]


async def list_decision_models(timeout: float = 20.0) -> tuple[list[dict[str, Any]], str | None]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{config.OPENROUTER_BASE_URL}/models",
                params={"output_modalities": "decisions"},
            )
        if response.status_code >= 400:
            raise ProviderError("OpenRouter", _error_message(response, "OpenRouter"), response.status_code)
        data = response.json()
        models = []
        for item in data.get("data", []):
            models.append(
                {
                    "id": item["id"],
                    "name": item.get("name", item["id"]),
                    "pricing": item.get("pricing", {}),
                    "context_length": item.get("context_length"),
                }
            )
        return models, None
    except (httpx.HTTPError, ProviderError, ValueError) as exc:
        fallback = [{"id": model, "name": model, "pricing": {}, "context_length": None} for model in FALLBACK_DECISION_MODELS]
        return fallback, str(exc)


async def provider_status(timeout: float = 15.0) -> dict[str, dict[str, Any]]:
    status: dict[str, dict[str, Any]] = {
        "nam": {"ok": False, "detail": "not checked"},
        "openrouter": {"ok": False, "detail": "not checked"},
    }
    if not config.NAN_API_KEY:
        status["nam"] = {"ok": False, "detail": "NAN_API_KEY is not set"}
    else:
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    f"{config.NAN_BASE_URL}/models",
                    headers={"Authorization": f"Bearer {config.NAN_API_KEY}"},
                )
            elapsed = round((time.perf_counter() - started) * 1000)
            if response.status_code < 400:
                status["nam"] = {"ok": True, "detail": f"authenticated in {elapsed} ms"}
            else:
                status["nam"] = {"ok": False, "detail": _error_message(response, "Nam")}
        except httpx.HTTPError as exc:
            status["nam"] = {"ok": False, "detail": f"request failed: {exc}"}

    if not config.OPENROUTER_API_KEY:
        status["openrouter"] = {"ok": False, "detail": "OPENROUTER_API_KEY is not set"}
    else:
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    f"{config.OPENROUTER_BASE_URL}/key",
                    headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
                )
            elapsed = round((time.perf_counter() - started) * 1000)
            if response.status_code < 400:
                data = response.json().get("data", {})
                status["openrouter"] = {
                    "ok": True,
                    "detail": f"authenticated in {elapsed} ms",
                    "label": data.get("label"),
                    "limit": data.get("limit"),
                    "usage": data.get("usage"),
                }
            else:
                status["openrouter"] = {
                    "ok": False,
                    "detail": _error_message(response, "OpenRouter"),
                }
        except httpx.HTTPError as exc:
            status["openrouter"] = {"ok": False, "detail": f"request failed: {exc}"}
    return status
