from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .config import RouterConfig
from .core import AIRouter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

router = AIRouter()


def _resolve_env_vars(value: str) -> str:
    return re.sub(r"\$(\w+)|\$\{(\w+)\}", lambda m: os.environ.get(m.group(1) or m.group(2), m.group(0)), value)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config_path = os.environ.get("CONFIG_PATH", "ai_router/example_config.json")
    try:
        config = RouterConfig.from_file(config_path)
        router.set_config(config)
        logger.info("Loaded config from %s", config_path)
        for provider_name, provider in config.provider.items():
            raw_key = provider.options.apiKey
            if raw_key:
                resolved = _resolve_env_vars(raw_key)
                if resolved and "$" not in resolved:
                    keys = [k.strip() for k in resolved.split(",") if k.strip()]
                    for key in keys:
                        router.keys.add_key(provider_name, key)
                    logger.info("Added %d API key(s) for provider '%s'", len(keys), provider_name)
                else:
                    logger.warning("API key for '%s' not resolved (missing env var?)", provider_name)
    except Exception as e:
        logger.warning("Could not load config on startup: %s", e)
    yield
    await router.close()


app = FastAPI(title="AI Router", version="0.1.0", lifespan=lifespan)


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None


class KeyAddRequest(BaseModel):
    provider: str
    api_key: str


class KeySetRequest(BaseModel):
    provider: str
    api_keys: list[str]


class KeyActivateRequest(BaseModel):
    provider: str
    api_key: str


class ConfigUpdateRequest(BaseModel):
    config: dict[str, Any]


# ---- Admin endpoints ----

@app.post("/admin/keys/add")
async def add_key(req: KeyAddRequest):
    router.keys.add_key(req.provider, req.api_key)
    return {"status": "ok", "provider": req.provider, "total_keys": len(router.keys.get_keys(req.provider))}


@app.post("/admin/keys/set")
async def set_keys(req: KeySetRequest):
    router.keys.set_keys(req.provider, req.api_keys)
    return {"status": "ok", "provider": req.provider, "total_keys": len(req.api_keys)}


@app.get("/admin/keys")
async def list_keys(provider: str | None = None):
    if provider:
        return {"provider": provider, "keys": router.keys.get_keys_with_state(provider)}
    providers = list(router.config.provider.keys()) if router.config else ["kimchi"]
    return {"providers": {p: router.keys.get_keys_with_state(p) for p in providers}}


@app.delete("/admin/keys/remove")
async def remove_key(provider: str, api_key: str):
    ok = router.keys.remove_key(provider, api_key)
    if not ok:
        raise HTTPException(404, "Key not found")
    return {"status": "removed", "provider": provider}


@app.post("/admin/keys/activate")
async def activate_key(req: KeyActivateRequest):
    ok = router.keys.activate_key(req.provider, req.api_key)
    if not ok:
        raise HTTPException(404, "Key not found")
    router.metrics.record_key_activation(req.provider)
    return {"status": "activated", "provider": req.provider}


@app.post("/admin/config")
async def set_config(req: ConfigUpdateRequest):
    try:
        config = RouterConfig.model_validate(req.config)
        router.set_config(config)
        return {"status": "ok", "providers": list(config.provider.keys())}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/admin/config/load")
async def load_config(path: str):
    try:
        config = RouterConfig.from_file(path)
        router.set_config(config)
        return {"status": "ok", "providers": list(config.provider.keys())}
    except Exception as e:
        raise HTTPException(400, str(e))


# ---- OpenAI-compatible endpoints ----

@app.get("/v1/models")
async def list_models():
    if not router.config:
        return {"object": "list", "data": []}
    models = []
    for provider_name, provider in router.config.provider.items():
        for model_key, model_cfg in provider.models.items():
            models.append({
                "id": model_key,
                "object": "model",
                "created": 0,
                "owned_by": provider_name,
            })
    return {"object": "list", "data": models}


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request):
    kwargs: dict[str, Any] = {}
    if body.temperature is not None:
        kwargs["temperature"] = body.temperature
    if body.max_tokens is not None:
        kwargs["max_tokens"] = body.max_tokens
    if body.top_p is not None:
        kwargs["top_p"] = body.top_p

    try:
        result = await router.chat_completions(
            model_name=body.model,
            messages=[m for m in body.messages],
            stream=body.stream,
            **kwargs,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    if body.stream:

        async def event_stream():
            async for chunk in result:
                if chunk["type"] == "chunk":
                    yield f"data: {json.dumps(chunk['data'])}\n\n"
                elif chunk["type"] == "done":
                    yield "data: [DONE]\n\n"
                elif chunk["type"] == "rate_limit":
                    yield f"data: {json.dumps({'error': 'rate_limit', 'provider': chunk['provider']})}\n\n"
                elif chunk["type"] == "error":
                    yield f"data: {json.dumps({'error': 'upstream_error', 'status': chunk['status']})}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return JSONResponse(content=result)


@app.get("/admin/metrics")
async def get_metrics():
    return router.metrics.snapshot()


@app.get("/health")
async def health():
    return {"status": "ok", "config_loaded": router.config is not None}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("ai_router.main:app", host="0.0.0.0", port=port)
