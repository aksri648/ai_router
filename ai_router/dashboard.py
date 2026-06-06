from __future__ import annotations

import os
import time
from typing import Any

import httpx
import streamlit as st
from streamlit_autorefresh import st_autorefresh

st.set_page_config(
    page_title="AI Router Dashboard",
    page_icon="📡",
    layout="wide",
)

DEFAULT_API = os.environ.get("ROUTER_URL", "http://localhost:8000")
API_BASE = st.sidebar.text_input("Router URL", value=DEFAULT_API)
REFRESH_INTERVAL = st.sidebar.select_slider(
    "Auto-refresh (s)", options=[5, 10, 15, 30, 60], value=10
)

st_autorefresh(interval=REFRESH_INTERVAL * 1000, key="autorefresh")

# ---- Session state for time-series ----

if "history" not in st.session_state:
    st.session_state.history = []  # list of (timestamp, totals_dict)
MAX_HISTORY = 60


def fetch_json(path: str) -> dict[str, Any] | None:
    try:
        r = httpx.get(f"{API_BASE}{path}", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"Failed to fetch {path}: {e}")
        return None


# ---- Fetch data ----

health = fetch_json("/health")
metrics_data = fetch_json("/admin/metrics")
keys_data = fetch_json("/admin/keys")

# ---- Title bar ----

col1, col2 = st.columns([3, 1])
with col1:
    st.title("📡 AI Router Dashboard")
with col2:
    if health:
        ok = health.get("status") == "ok"
        config_ok = health.get("config_loaded", False)
        st.markdown(
            f"""
            **Status:** {'🟢 Online' if ok else '🔴 Offline'}  
            **Config:** {'✅ Loaded' if config_ok else '❌ Not loaded'}
            """
        )
    else:
        st.markdown("**Status:** 🔴 Cannot reach router")

if not metrics_data:
    st.warning("Waiting for metrics data...")
    st.stop()

totals = metrics_data.get("totals", {})
providers = metrics_data.get("providers", {})

# ---- Append to time-series history ----

ts = time.time()
st.session_state.history.append((ts, dict(totals)))
if len(st.session_state.history) > MAX_HISTORY:
    st.session_state.history = st.session_state.history[-MAX_HISTORY:]

# ---- Overview cards ----

st.subheader("Overview")

total_reqs = totals.get("requests", 0)
total_success = totals.get("successes", 0)
total_rl = totals.get("rate_limits", 0)
total_deact = totals.get("key_deactivations", 0)
total_act = totals.get("key_activations", 0)
success_rate = (total_success / total_reqs * 100) if total_reqs else 0

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total Requests", total_reqs)
c2.metric("Success Rate", f"{success_rate:.1f}%")
c3.metric("Rate Limits", total_rl)
c4.metric("Keys Deactivated", total_deact)
c5.metric("Keys Reactivated", total_act)

# ---- Request Timeline ----

if len(st.session_state.history) >= 2:
    st.subheader("Request Timeline")
    hist = st.session_state.history
    timeline_data = {
        "Requests": [h[1].get("requests", 0) for h in hist],
        "Successes": [h[1].get("successes", 0) for h in hist],
        "Rate Limits": [h[1].get("rate_limits", 0) for h in hist],
    }
    st.line_chart(timeline_data)

# ---- Per-provider breakdown table ----

st.subheader("Per-Provider Metrics")

provider_rows = []
for p, data in providers.items():
    reqs = data.get("requests", 0)
    succ = data.get("successes", 0)
    rl = data.get("rate_limits", 0)
    sr = (succ / reqs * 100) if reqs else 0
    lat = data.get("latency", {})
    lat_str = f'{lat.get("avg", 0)}ms' if lat.get("avg") else "—"
    errs = data.get("errors", {})
    err_str = ", ".join(f"{k}: {v}" for k, v in errs.items()) if errs else "—"
    provider_rows.append(
        {
            "Provider": p,
            "Requests": reqs,
            "Successes": succ,
            "Success Rate": f"{sr:.1f}%",
            "Rate Limits": rl,
            "Avg Latency": lat_str,
            "Stream Reqs": data.get("stream_requests", 0),
            "Deactivations": data.get("key_deactivations", 0),
            "Errors": err_str,
        }
    )

if provider_rows:
    st.dataframe(provider_rows, use_container_width=True, hide_index=True)
else:
    st.info("No provider metrics yet.")

# ---- Charts row 1: Per-model + Per-key ----

st.subheader("API-Specific Metrics")

charts_row1 = st.columns(2)

with charts_row1[0]:
    st.markdown("**Requests per Model**")
    model_data: dict[str, int] = {}
    for p, data in providers.items():
        for m, c in data.get("models", {}).items():
            short_m = m.split("/")[-1] if "/" in m else m
            model_data[short_m] = model_data.get(short_m, 0) + c
    if model_data:
        st.bar_chart(model_data, x_label="Model")
    else:
        st.info("No model data yet.")

with charts_row1[1]:
    st.markdown("**Requests per Key**")
    key_data: dict[str, int] = {}
    for p, data in providers.items():
        for k, c in data.get("key_usage", {}).items():
            key_data[k] = key_data.get(k, 0) + c
    if key_data:
        st.bar_chart(key_data, x_label="Key (last 12 chars)")
    else:
        st.info("No key usage data yet.")

# ---- Charts row 2: Status codes + Latency histogram ----

charts_row2 = st.columns(2)

with charts_row2[0]:
    st.markdown("**Status Code Distribution**")
    sc_data: dict[str, int] = {}
    for p, data in providers.items():
        for code, count in data.get("status_codes", {}).items():
            label = f"{code}" if code != "0" else "network_err"
            sc_data[label] = sc_data.get(label, 0) + count
    if sc_data:
        st.bar_chart(sc_data, x_label="Status Code")
    else:
        st.info("No status codes yet.")

with charts_row2[1]:
    st.markdown("**Latency Percentiles (ms)**")
    lat_rows = []
    for p, data in providers.items():
        lat = data.get("latency", {})
        if lat.get("avg", 0) > 0:
            lat_rows.append(
                {
                    "Provider": p,
                    "p50": lat.get("p50", 0),
                    "p95": lat.get("p95", 0),
                    "p99": lat.get("p99", 0),
                    "avg": lat.get("avg", 0),
                    "min": lat.get("min", 0),
                    "max": lat.get("max", 0),
                }
            )
    if lat_rows:
        st.dataframe(lat_rows, use_container_width=True, hide_index=True)
    else:
        st.info("No latency data yet.")

# ---- Key Health ----

st.subheader("API Key Health")

if keys_data:
    all_keys: list[dict[str, Any]] = []
    if "providers" in keys_data:
        for prov, klist in keys_data["providers"].items():
            for k in klist:
                k["provider"] = prov
                all_keys.append(k)
    elif "provider" in keys_data:
        for k in keys_data["keys"]:
            k["provider"] = keys_data["provider"]
            all_keys.append(k)

    if all_keys:
        rows = []
        for entry in all_keys:
            key_str: str = entry.get("key", "")
            state: int = entry.get("state", 0)
            short = f"...{key_str[-12:]}" if len(key_str) > 12 else key_str
            rows.append(
                {
                    "Provider": entry.get("provider", "?"),
                    "Key": short,
                    "State": "🟢 Active" if state == 0 else "🔴 Deactivated",
                }
            )

        st.dataframe(rows, use_container_width=True, hide_index=True)

        deactivated = [e for e in all_keys if e.get("state") == 1]
        if deactivated:
            st.markdown("### Reactivate a Key")
            for entry in deactivated:
                p = entry.get("provider", "")
                k = entry.get("key", "")
                short = f"...{k[-12:]}" if len(k) > 12 else k
                if st.button(f"Reactivate {short} ({p})", key=f"react_{p}_{k}"):
                    r = httpx.post(
                        f"{API_BASE}/admin/keys/activate",
                        json={"provider": p, "api_key": k},
                        timeout=5,
                    )
                    if r.ok:
                        st.success(f"Reactivated key for {p}")
                        st.rerun()
        else:
            st.info("All keys are active.")
    else:
        st.info("No keys configured.")
else:
    st.info("No key data available.")

# ---- Errors ----

st.subheader("Errors")
has_errors = False
for p, data in providers.items():
    errs = data.get("errors", {})
    if errs:
        has_errors = True
        for etype, count in errs.items():
            st.write(f"- **{p}**: `{etype}` × {count}")
if not has_errors:
    st.success("No errors recorded.")
