from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class ModelLimits(BaseModel):
    context: int = 4096
    output: int = 4096


class ModelConfig(BaseModel):
    id: str | None = None
    name: str | None = None
    tool_call: bool = False
    reasoning: bool = False
    modalities: dict[str, list[str]] | None = None
    limit: ModelLimits = ModelLimits()


class ProviderOptions(BaseModel):
    baseURL: str
    litellmProxy: bool = False
    apiKey: str = ""


class ProviderConfig(BaseModel):
    npm: str = ""
    name: str = ""
    options: ProviderOptions
    models: dict[str, ModelConfig]


class RouterConfig(BaseModel):
    model: str | None = None
    mode: dict[str, Any] | None = None
    provider: dict[str, ProviderConfig]

    @classmethod
    def from_file(cls, path: str | Path) -> RouterConfig:
        data = json.loads(Path(path).read_text())
        return cls.model_validate(data)

    def resolve_model(self, model_name: str) -> tuple[str, str, ProviderConfig]:
        for provider_name, provider in self.provider.items():
            if model_name in provider.models:
                model_id = provider.models[model_name].id or model_name
                return provider_name, model_id, provider
        raise ValueError(f"Model '{model_name}' not found in any provider")
