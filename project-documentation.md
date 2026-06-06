# AI Router — Project Documentation

A FastAPI-based proxy server that routes OpenAI-compatible chat completion requests across multiple API keys for a given AI provider. Features automatic key health tracking (state 0/1), rate-limit failover with key deactivation, in-memory metrics collection, a Streamlit monitoring dashboard, and seamless Render deployment.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Project Structure](#project-structure)
- [Configuration](#configuration)
- [API Endpoints](#api-endpoints)
- [Core Components](#core-components)
- [Metrics & Dashboard](#metrics--dashboard)
- [Running Locally](#running-locally)
- [Deployment](#deployment)
- [Environment Variables](#environment-variables)
- [API Key Management](#api-key-management)

---

## Architecture Overview

```
Client ──POST /v1/chat/completions──> FastAPI ──httpx──> Upstream LLM API
                                          │
                                     ┌────┴────┐
                                     │ Key     │  (state 0/1 per key)
                                     │ Manager │
                                     └────┬────┘
                                     ┌────┴────┐
                                     │ Metrics │  (requests, latency,
                                     │ Collector│   errors, per-model, etc.)
                                     └─────────┘
```

The router accepts OpenAI-compatible chat completion requests, resolves the requested model to a provider via config, then proxies the request using the first healthy API key. If a key returns a rate-limit or retryable error, it is marked as state `1` (deactivated) and the next healthy key is tried.

### Request flow

1. Client sends `POST /v1/chat/completions` with `model`, `messages`, etc.
2. `RouterConfig.resolve_model()` maps the model name to a provider + model ID
3. `KeyManager.next_key()` returns the first active key (state 0) for that provider
4. Request is proxied via `httpx.AsyncClient` to the upstream provider
5. On 200: success is recorded, response returned to client
6. On 429 / 5xx / network error: key is deactivated (state → 1), next healthy key tried
7. If all keys exhausted: `502` returned to client

---

## Project Structure

```
├── .env                          # Local env vars (gitignored)
├── .gitignore
├── context.md                    # Quick reference
├── project-documentation.md      # This file
├── render.yaml                   # Render Blueprint (infra as code)
│
└── ai_router/
    ├── __init__.py               # Empty package init
    ├── main.py                   # FastAPI app: endpoints, startup, lifespan
    ├── core.py                   # AIRouter, KeyManager, MetricsCollector
    ├── config.py                 # Pydantic models (RouterConfig, ProviderConfig, etc.)
    ├── dashboard.py              # Streamlit monitoring dashboard
    ├── test_api.py               # Integration test script
    ├── example_config.json       # Sample provider config
    └── requirements.txt          # Python dependencies
```

---

## Configuration

### `example_config.json`

Defines providers, their upstream URLs, and available models.

```json
{
  "model": "kimchi/minimax-m2.7",
  "provider": {
    "kimchi": {
      "name": "Kimchi",
      "options": {
        "baseURL": "https://llm.kimchi.dev/openai/v1",
        "apiKey": "$KIMCHI_API_KEY"
      },
      "models": {
        "kimi-k2.6": {
          "id": "kimi-k2.6",
          "tool_call": true,
          "limit": { "context": 262144, "output": 32768 }
        }
      }
    }
  }
}
```

**Key points:**
- `apiKey` supports `$VAR` or `${VAR}` syntax — resolved from environment on startup
- Multiple keys: set env var as comma-separated (e.g. `key1,key2`)
- Config path set via `CONFIG_PATH` env var (default: `ai_router/example_config.json`)

### Pydantic models (`config.py`)

| Model | Fields | Purpose |
|---|---|---|
| `RouterConfig` | `provider`, `model`, `mode` | Top-level config, model resolution |
| `ProviderConfig` | `npm`, `name`, `options`, `models` | Per-provider settings |
| `ProviderOptions` | `baseURL`, `litellmProxy`, `apiKey` | Connection details |
| `ModelConfig` | `id`, `name`, `tool_call`, `reasoning`, `modalities`, `limit` | Per-model metadata |
| `ModelLimits` | `context`, `output` | Token limits |

---

## API Endpoints

### Proxy

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat completions (stream + non-stream) |

Request body:
```json
{
  "model": "minimax-m2.7",
  "messages": [{"role": "user", "content": "Hello!"}],
  "stream": false,
  "max_tokens": 100
}
```

### Admin

| Method | Path | Description |
|---|---|---|
| `POST` | `/admin/keys/add` | Add a single API key |
| `POST` | `/admin/keys/set` | Replace all keys for a provider |
| `GET` | `/admin/keys` | List keys with state (0 = active, 1 = deactivated) |
| `DELETE` | `/admin/keys/remove` | Remove a specific key |
| `POST` | `/admin/keys/activate` | Reactivate a deactivated key |
| `POST` | `/admin/config` | Upload config JSON inline |
| `POST` | `/admin/config/load` | Load config from file path |
| `GET` | `/admin/metrics` | Full metrics snapshot |

### Health

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Server status + config loaded flag |

---

## Core Components

### `KeyManager` (`core.py:121-170`)

Manages API keys with a health state per key.

| Method | Description |
|---|---|
| `add_key(provider, key)` | Adds key with state 0 (active) |
| `remove_key(provider, key)` | Removes key |
| `set_keys(provider, keys)` | Replaces all keys for a provider, resets states |
| `get_keys(provider)` | Returns list of key strings |
| `get_keys_with_state(provider)` | Returns `[{key, state}]` |
| `next_key(provider)` | Returns first key with state 0, or `None` |
| `deactivate_key(provider, key)` | Sets state to 1 (rate-limited/failed) |
| `activate_key(provider, key)` | Resets state to 0 |

**State semantics:**
- `0` — Active (healthy, will be used for requests)
- `1` — Deactivated (was rate-limited or errored, skipped)

### `AIRouter` (`core.py:172-334`)

Main proxy class.

| Method | Description |
|---|---|
| `chat_completions(model_name, messages, stream, **kwargs)` | Proxies request to upstream, handles failover |
| `set_config(config)` | Update runtime config |
| `close()` | Cleanup HTTP client |

**Key behavior:**
- Uses `httpx.AsyncClient` with 120s timeout (10s connect)
- Rate limit detection: status 429, or body contains "rate limit", "too many", "quota"
- Retryable statuses: 429, 500, 502, 503, 504
- On retryable failure: key deactivated, next key tried
- On non-retryable failure: exception raised to caller

### `MetricsCollector` (`core.py:21-118`)

In-memory metrics store. All counters are per-provider.

| Method | Records |
|---|---|
| `record_request(provider, model)` | Total request + per-model usage |
| `record_success(provider, latency)` | Success + latency sample |
| `record_rate_limit(provider)` | Rate limit event |
| `record_error(provider, type)` | Error by type (network, http_*, unknown) |
| `record_key_deactivation(provider)` | Key deactivation event |
| `record_key_activation(provider)` | Key reactivation event |
| `record_stream_request(provider)` | Streaming request |
| `record_key_usage(provider, key)` | Per-key usage (last 12 chars) |
| `record_status_code(provider, code)` | HTTP status code distribution |
| `snapshot()` | Returns full metrics dict |

**Snapshot output structure:**
```json
{
  "providers": {
    "kimchi": {
      "requests": 150,
      "successes": 140,
      "rate_limits": 8,
      "errors": { "network": 2 },
      "key_deactivations": 3,
      "key_activations": 1,
      "stream_requests": 20,
      "latency": { "min": 120, "avg": 450, "max": 3200, "p50": 380, "p95": 1200, "p99": 2800 },
      "models": { "minimax-m2.7": 100, "kimi-k2.6": 50 },
      "key_usage": { "bd414be4": 80, "8eb841b3": 70 },
      "status_codes": { "200": 140, "429": 8, "0": 2 }
    }
  },
  "totals": {
    "requests": 150,
    "successes": 140,
    "rate_limits": 8,
    "key_deactivations": 3,
    "key_activations": 1,
    "stream_requests": 20
  }
}
```

---

## Metrics & Dashboard

### Endpoint: `GET /admin/metrics`

Returns the full `MetricsCollector.snapshot()` as JSON.

### Streamlit Dashboard (`dashboard.py`)

A monitoring dashboard that polls the router's `/admin/metrics` and `/admin/keys` endpoints.

**Start:**
```bash
streamlit run ai_router/dashboard.py
```

**Features:**

| Section | Content |
|---|---|
| Overview Cards | Total requests, success rate %, rate limits, deactivations, reactivations |
| Request Timeline | Rolling line chart (last 60 snapshots) of cumulative requests, successes, rate limits |
| Per-Provider Table | Requests, successes, rate limits, avg latency, stream reqs, deactivations, errors |
| Requests per Model | Bar chart of request distribution across models |
| Requests per Key | Bar chart of individual key usage (last 12 chars) |
| Status Code Distribution | Bar chart of HTTP response codes (200, 429, 5xx, network_err) |
| Latency Percentiles | Table: p50, p95, p99, avg, min, max per provider (ms) |
| Key Health Table | All keys with active/deactivated status, inline reactivate buttons |
| Error Log | Per-provider error type counts |

---

## Running Locally

### Prerequisites

- Python 3.12+
- pip

### Setup

```bash
# Install dependencies
pip install -r ai_router/requirements.txt

# Set environment variables
export KIMCHI_API_KEY="key1,key2"   # comma-separated for multiple keys
export CONFIG_PATH="ai_router/example_config.json"  # optional, has default

# Start the router
uvicorn ai_router.main:app --host 0.0.0.0 --port 8000 --reload
```

### Verify

```bash
curl http://localhost:8000/health
# {"status":"ok","config_loaded":true}

curl http://localhost:8000/admin/keys
# {"provider":"kimchi","keys":[{"key":"...bd414be4","state":0},{"key":"...8eb841b3","state":0}]}

curl http://localhost:8000/admin/metrics
# Full metrics snapshot
```

### Test script

```bash
python -m ai_router.test_api
```

### Dashboard (separate terminal)

```bash
streamlit run ai_router/dashboard.py
```

---

## Deployment

### Render (via render.yaml)

The repo includes `render.yaml` for infrastructure-as-code deployment:

```yaml
services:
  - type: web
    name: ai-router
    runtime: python
    plan: free
    buildCommand: pip install -r ai_router/requirements.txt
    startCommand: uvicorn ai_router.main:app --host 0.0.0.0 --port $PORT
    envVars:
      - key: CONFIG_PATH
        value: ai_router/example_config.json
      - key: KIMCHI_API_KEY
        sync: false
```

**Steps:**
1. Push to GitHub
2. Connect repo in Render dashboard
3. Set `KIMCHI_API_KEY` env var in Render dashboard (comma-separated for multiple keys)
4. Deploy

### Manual (any platform)

```bash
# Set env vars
export KIMCHI_API_KEY="key1,key2"
export PORT=8000

# Start
uvicorn ai_router.main:app --host 0.0.0.0 --port $PORT
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `KIMCHI_API_KEY` | Yes | — | API key(s), comma-separated for multiple |
| `CONFIG_PATH` | No | `ai_router/example_config.json` | Path to provider config JSON |
| `PORT` | No | `8000` | Server port (Render sets this automatically) |

**Note:** `.env` file is supported for local development but is gitignored. On Render, set vars in the dashboard.

---

## API Key Management

### Startup (automatic)

On boot, `main.py`:
1. Reads config from `CONFIG_PATH`
2. Resolves `$KIMCHI_API_KEY` from the environment
3. Splits by comma and adds all keys to `KeyManager` with state 0

### Runtime (admin API)

Keys can also be managed dynamically:

```bash
# Add a key
curl -X POST http://localhost:8000/admin/keys/add \
  -H "Content-Type: application/json" \
  -d '{"provider":"kimchi","api_key":"sk-..."}'

# List keys with states
curl http://localhost:8000/admin/keys?provider=kimchi

# Reactivate a deactivated key
curl -X POST http://localhost:8000/admin/keys/activate \
  -H "Content-Type: application/json" \
  -d '{"provider":"kimchi","api_key":"sk-..."}'

# Set all keys (replaces)
curl -X POST http://localhost:8000/admin/keys/set \
  -H "Content-Type: application/json" \
  -d '{"provider":"kimchi","api_keys":["sk-1","sk-2"]}'

# Remove a key
curl -X DELETE "http://localhost:8000/admin/keys/remove?provider=kimchi&api_key=sk-..."
```

### Key states

| State | Meaning | Trigger |
|---|---|---|
| `0` | Active (healthy) | On add, set, or manual reactivate |
| `1` | Deactivated (failed) | 429 rate limit, 5xx, network/timeout error |

---

## Deployment URLs

| Resource | URL |
|---|---|
| GitHub | https://github.com/aksri648/ai_router |
| Render | https://ai-router-yagv.onrender.com |
