from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import httpx

from .config import ProviderConfig, RouterConfig

logger = logging.getLogger(__name__)

RATE_LIMIT_STATUSES = {429}
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class MetricsCollector:
    def __init__(self) -> None:
        self._requests: dict[str, int] = defaultdict(int)
        self._successes: dict[str, int] = defaultdict(int)
        self._rate_limits: dict[str, int] = defaultdict(int)
        self._errors: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._key_deactivations: dict[str, int] = defaultdict(int)
        self._key_activations: dict[str, int] = defaultdict(int)
        self._latencies: dict[str, list[float]] = defaultdict(list)
        self._stream_requests: dict[str, int] = defaultdict(int)
        self._last_rate_limit: dict[str, float] = {}
        self._models: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._key_usage: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._status_codes: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))

    def record_request(self, provider: str, model: str = "") -> None:
        self._requests[provider] += 1
        if model:
            self._models[provider][model] += 1

    def record_success(self, provider: str, latency: float) -> None:
        self._successes[provider] += 1
        self._latencies[provider].append(latency)

    def record_rate_limit(self, provider: str) -> None:
        self._rate_limits[provider] += 1
        self._last_rate_limit[provider] = time.time()

    def record_error(self, provider: str, error_type: str) -> None:
        self._errors[provider][error_type] += 1

    def record_key_deactivation(self, provider: str) -> None:
        self._key_deactivations[provider] += 1

    def record_key_activation(self, provider: str) -> None:
        self._key_activations[provider] += 1

    def record_stream_request(self, provider: str) -> None:
        self._stream_requests[provider] += 1

    def record_key_usage(self, provider: str, key: str) -> None:
        self._key_usage[provider][key[-12:]] += 1

    def record_status_code(self, provider: str, code: int) -> None:
        self._status_codes[provider][code] += 1

    def _compute_percentiles(self, latencies: list[float]) -> dict[str, float]:
        if not latencies:
            return {"min": 0, "avg": 0, "max": 0, "p50": 0, "p95": 0, "p99": 0}
        s = sorted(latencies)
        n = len(s)
        avg = sum(s) / n
        return {
            "min": round(s[0] * 1000, 1),
            "avg": round(avg * 1000, 1),
            "max": round(s[-1] * 1000, 1),
            "p50": round(s[n // 2] * 1000, 1),
            "p95": round(s[int(n * 0.95)] * 1000, 1),
            "p99": round(s[int(n * 0.99)] * 1000, 1),
        }

    def snapshot(self) -> dict:
        providers = set()
        for d in (self._requests, self._successes, self._rate_limits, self._errors,
                  self._key_deactivations, self._key_activations, self._stream_requests,
                  self._models, self._status_codes):
            providers.update(d.keys())

        provider_data = {}
        for p in sorted(providers):
            latencies = self._latencies.get(p, [])
            reqs = self._requests.get(p, 0)
            succ = self._successes.get(p, 0)
            provider_data[p] = {
                "requests": reqs,
                "successes": succ,
                "rate_limits": self._rate_limits.get(p, 0),
                "errors": dict(self._errors.get(p, {})),
                "key_deactivations": self._key_deactivations.get(p, 0),
                "key_activations": self._key_activations.get(p, 0),
                "stream_requests": self._stream_requests.get(p, 0),
                "latency": self._compute_percentiles(latencies),
                "models": dict(self._models.get(p, {})),
                "key_usage": dict(self._key_usage.get(p, {})),
                "status_codes": {str(k): v for k, v in self._status_codes.get(p, {}).items()},
            }

        return {
            "providers": provider_data,
            "totals": {
                "requests": sum(self._requests.values()),
                "successes": sum(self._successes.values()),
                "rate_limits": sum(self._rate_limits.values()),
                "key_deactivations": sum(self._key_deactivations.values()),
                "key_activations": sum(self._key_activations.values()),
                "stream_requests": sum(self._stream_requests.values()),
            },
        }


class KeyManager:
    def __init__(self) -> None:
        self._keys: dict[str, list[str]] = defaultdict(list)
        self._state: dict[str, dict[str, int]] = defaultdict(dict)

    def add_key(self, provider: str, key: str) -> None:
        if key not in self._keys[provider]:
            self._keys[provider].append(key)
            self._state[provider][key] = 0
        logger.info("Added key for provider '%s' (total: %d)", provider, len(self._keys[provider]))

    def remove_key(self, provider: str, key: str) -> bool:
        try:
            self._keys[provider].remove(key)
            self._state[provider].pop(key, None)
            return True
        except ValueError:
            return False

    def set_keys(self, provider: str, keys: list[str]) -> None:
        self._keys[provider] = list(keys)
        self._state[provider] = {k: 0 for k in keys}

    def get_keys(self, provider: str) -> list[str]:
        return list(self._keys[provider])

    def get_keys_with_state(self, provider: str) -> list[dict[str, int | str]]:
        return [
            {"key": k, "state": self._state[provider].get(k, 0)}
            for k in self._keys[provider]
        ]

    def next_key(self, provider: str) -> str | None:
        for key in self._keys[provider]:
            if self._state[provider].get(key, 0) == 0:
                return key
        return None

    def deactivate_key(self, provider: str, key: str) -> None:
        if key in self._state[provider]:
            self._state[provider][key] = 1
            logger.warning("Deactivated key %s for provider '%s'", key[-8:], provider)

    def activate_key(self, provider: str, key: str) -> bool:
        if key in self._state[provider]:
            self._state[provider][key] = 0
            logger.info("Activated key %s for provider '%s'", key[-8:], provider)
            return True
        return False


class AIRouter:
    def __init__(self, config: RouterConfig | None = None) -> None:
        self.config = config
        self.keys = KeyManager()
        self.metrics = MetricsCollector()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))

    async def close(self) -> None:
        await self._client.aclose()

    def set_config(self, config: RouterConfig) -> None:
        self.config = config

    def _get_provider_base_url(self, provider: ProviderConfig) -> str:
        return provider.options.baseURL.rstrip("/")

    def _build_url(self, base_url: str, path: str) -> str:
        return f"{base_url}/{path.lstrip('/')}"

    def _is_rate_limit(self, status: int, body: dict | str | None) -> bool:
        if status in RATE_LIMIT_STATUSES:
            return True
        if isinstance(body, dict):
            err = body.get("error", {}) if isinstance(body.get("error"), dict) else {}
            msg = str(err.get("message", "")).lower() if isinstance(err, dict) else str(body.get("error", "")).lower()
            if any(p in msg for p in ("rate limit", "rate_limit", "too many", "quota", "429")):
                return True
        return False

    async def chat_completions(
        self,
        model_name: str,
        messages: list[dict],
        stream: bool = False,
        **kwargs: Any,
    ) -> dict | AsyncGenerator[dict, None]:
        if not self.config:
            raise RuntimeError("No config loaded")

        provider_name, model_id, provider = self.config.resolve_model(model_name)
        base_url = self._get_provider_base_url(provider)
        url = self._build_url(base_url, "/chat/completions")

        body = {
            "model": model_id,
            "messages": messages,
            "stream": stream,
            **kwargs,
        }

        keys = self.keys.get_keys(provider_name)
        if not keys:
            raise RuntimeError(f"No API keys configured for provider '{provider_name}'")

        self.metrics.record_request(provider_name, model_name)

        last_error: Exception | None = None
        attempted_keys: set[str] = set()

        for attempt in range(len(keys)):
            api_key = self.keys.next_key(provider_name)
            if api_key is None or api_key in attempted_keys:
                break
            attempted_keys.add(api_key)

            try:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }

                if stream:
                    self.metrics.record_stream_request(provider_name)
                    self.metrics.record_key_usage(provider_name, api_key)
                    return self._stream_request(url, headers, body, provider_name, api_key)

                t0 = time.time()
                resp = await self._client.post(url, json=body, headers=headers)
                latency = time.time() - t0
                self.metrics.record_status_code(provider_name, resp.status_code)

                if resp.status_code == 200:
                    self.metrics.record_key_usage(provider_name, api_key)
                    self.metrics.record_success(provider_name, latency)
                    return resp.json()

                resp_body = self._parse_body(resp)

                if self._is_rate_limit(resp.status_code, resp_body):
                    self.keys.deactivate_key(provider_name, api_key)
                    self.metrics.record_rate_limit(provider_name)
                    self.metrics.record_key_deactivation(provider_name)
                    logger.warning(
                        "Rate limited on key %s for provider '%s', trying next key (attempt %d/%d)",
                        api_key[-8:], provider_name, attempt + 1, len(keys),
                    )
                    continue

                resp.raise_for_status()

            except (httpx.TimeoutException, httpx.NetworkError) as e:
                self.keys.deactivate_key(provider_name, api_key)
                self.metrics.record_error(provider_name, "network")
                self.metrics.record_key_deactivation(provider_name)
                self.metrics.record_status_code(provider_name, 0)
                logger.warning("Network error with key %s: %s", api_key[-8:], e)
                last_error = e
                continue
            except httpx.HTTPStatusError as e:
                if e.response.status_code in RETRYABLE_STATUSES:
                    self.keys.deactivate_key(provider_name, api_key)
                    self.metrics.record_key_deactivation(provider_name)
                    logger.warning("HTTP %d with key %s, trying next", e.response.status_code, api_key[-8:])
                    last_error = e
                    continue
                self.metrics.record_error(provider_name, f"http_{e.response.status_code}")
                raise
            except Exception as e:
                self.metrics.record_error(provider_name, "unknown")
                last_error = e
                continue

        raise RuntimeError(
            f"All API keys exhausted for provider '{provider_name}'"
        ) from last_error

    async def _stream_request(
        self,
        url: str,
        headers: dict,
        body: dict,
        provider_name: str,
        api_key: str,
    ) -> AsyncGenerator[dict, None]:
        body["stream"] = True
        async with self._client.stream("POST", url, json=body, headers=headers) as resp:
            self.metrics.record_status_code(provider_name, resp.status_code)
            if resp.status_code == 200:
                self.metrics.record_key_usage(provider_name, api_key)
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            yield {"type": "done"}
                            return
                        yield {"type": "chunk", "data": json.loads(data_str)}
            elif self._is_rate_limit(resp.status_code, None):
                self.keys.deactivate_key(provider_name, api_key)
                self.metrics.record_rate_limit(provider_name)
                self.metrics.record_key_deactivation(provider_name)
                yield {"type": "rate_limit", "provider": provider_name}
            else:
                self.metrics.record_error(provider_name, f"http_{resp.status_code}")
                yield {"type": "error", "status": resp.status_code}

    def _parse_body(self, resp: httpx.Response) -> dict | str | None:
        try:
            return resp.json()
        except Exception:
            try:
                return resp.text
            except Exception:
                return None
