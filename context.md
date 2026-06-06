# AI Router

A FastAPI-based proxy server that routes chat completion requests across multiple API keys for a given AI provider. It provides OpenAI-compatible endpoints with key health tracking (state 0/1), rate-limit failover, and streaming support.

## Project Structure

```
ai_router/
  __init__.py          # Empty package init
  main.py              # FastAPI app entry point (endpoints, lifespan, startup)
  core.py              # AIRouter (proxy logic) + KeyManager + MetricsCollector
  config.py            # Pydantic models for config, provider, models
  dashboard.py         # Streamlit monitoring dashboard
  test_api.py          # Quick integration test script
  example_config.json  # Sample config for Kimchi provider
  requirements.txt     # Python deps
render.yaml            # Render Blueprint deployment config
.gitignore
```

## Architecture

### Entry Point (`main.py`)
- FastAPI app with endpoint groups:
  - On startup, auto-loads config from `CONFIG_PATH` env var and resolves `$VAR` API keys from environment
  - Listens on `$PORT` env var (Render-compatible) or defaults to 8000
  - **Admin endpoints** (`/admin/*`) — manage API keys (add/set/list/remove/activate), config (upload inline or load from file), and metrics (`GET /admin/metrics`)
  - **Proxy endpoint** (`/v1/chat/completions`) — OpenAI-compatible chat completions supporting both streaming and non-streaming
  - **Health check** (`/health`) — reports server status and whether config is loaded

### Core Logic (`core.py`)
- **`MetricsCollector`** — in-memory counters per provider with granular tracking:
  - Request/success/rate-limit/error counts
  - Per-model usage, per-key usage, status code distribution
  - Latency percentiles (p50, p95, p99, min, max, avg)
  - Key deactivations/activations, stream request counts
  - Exposed via `snapshot()` at `GET /admin/metrics`
- **`KeyManager`** — manages API keys with a health state per key (`0` = healthy, `1` = deactivated on failure). `next_key()` returns the first healthy key (no round-robin rotation). Keys are deactivated on rate-limit, 5xx, or network errors.
- **`AIRouter`** — main proxy class. Given a model name, resolves it to a provider via config, then attempts requests across healthy keys:
  - Rate limits (429) and retryable errors (5xx, network/timeout) trigger `deactivate_key()` and automatic failover to the next healthy key
  - Streaming is handled via `_stream_request()` which yields typed dicts (`chunk`, `done`, `rate_limit`, `error`)
  - Uses `httpx.AsyncClient` with a 120s timeout
  - All events are recorded through `self.metrics`

### Configuration (`config.py`)
Pydantic models defining the schema:
- **`RouterConfig`** — top-level config with provider map and model resolution
- **`ProviderConfig`** — per-provider settings (base URL, models)
- **`ModelConfig`** — per-model metadata (ID, tool-call support, reasoning, modality limits)
- **`ModelLimits`** — context/output token limits

Config is loaded from a JSON file matching the opencode schema format via `RouterConfig.from_file()`.

### Example Config (`example_config.json`)
Configures a single provider `"kimchi"` pointing to `https://llm.kimchi.dev/openai/v1` with three models:
- `kimi-k2.6` — 262K context, 32K output, tool-call + image input
- `kimi-k2.5` — same specs
- `minimax-m2.7` — 196K context, 32K output, tool-call

## Key Features
- **Key health tracking** — each key has a state (`0` = active, `1` = deactivated). Same key is reused until it fails.
- **Automatic deactivation** — keys are marked as state `1` on 429, 5xx, or network errors, and skipped on subsequent requests.
- **Manual reactivation** — deactivated keys can be reset via `POST /admin/keys/activate`.
- **Streaming** support (SSE-compatible) with rate-limit/error signals in-band.
- **Admin API** for dynamic key and config management at runtime.
- **Metrics & dashboard** — `MetricsCollector` tracks requests, success rate, latency, errors per provider; viewable via Streamlit dashboard.

## Running

```bash
pip install -r requirements.txt
uvicorn ai_router.main:app --reload
```

## Dashboard

```bash
streamlit run ai_router/dashboard.py
```

Opens a browser dashboard with:
- **Overview cards** — total requests, success rate %, rate limits, deactivations, reactivations
- **Request timeline** — rolling line chart of requests/successes/rate limits over time (last 60 snapshots)
- **Per-provider table** — requests, successes, rate limits, avg latency, errors
- **Per-model bar chart** — request distribution across models
- **Per-key bar chart** — request distribution across individual API keys
- **Status code distribution** — bar chart of HTTP response codes
- **Latency percentiles table** — p50/p95/p99/avg/min/max per provider
- **Key health table** — active/deactivated keys with inline reactivate buttons
- **Error log** — per-provider error type counts

## Testing

```bash
python -m ai_router.test_api
```

## Deploy to Render

### render.yaml (Blueprint)

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
        sync: false   # set manually in dashboard
```

### Manual deploy

1. Push repo to GitHub
2. In Render dashboard → **New Web Service** → connect repo
3. Set:
   - **Runtime**: Python
   - **Build Command**: `pip install -r ai_router/requirements.txt`
   - **Start Command**: `uvicorn ai_router.main:app --host 0.0.0.0 --port $PORT`
   - **Environment Variables**:
     - `KIMCHI_API_KEY` = `key1,key2` (comma-separated for multiple keys)
     - `CONFIG_PATH` = `ai_router/example_config.json`

### Startup behavior

On boot, `main.py` reads `CONFIG_PATH` (default `ai_router/example_config.json`), resolves `$KIMCHI_API_KEY` from the environment, and auto-adds all keys. Supports **comma-separated** values in the env var for multiple keys:

```
KIMCHI_API_KEY=key1,key2,key3
```

No manual API calls needed after deploy.
