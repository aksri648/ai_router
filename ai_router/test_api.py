"""Quick test script for the AI Router"""
import httpx
import json

BASE = "http://localhost:8000"

def test():
    client = httpx.Client(base_url=BASE, timeout=30)

    # 1. Health check
    r = client.get("/health")
    print("Health:", r.json())

    # 2. Load config
    r = client.post("/admin/config/load", params={"path": "example_config.json"})
    print("Config loaded:", r.json())

    # 3. Add multiple API keys
    keys = [
        "sk-kimchi-key-1",
        "sk-kimchi-key-2",
        "sk-kimchi-key-3",
    ]
    for key in keys:
        r = client.post("/admin/keys/add", json={"provider": "kimchi", "api_key": key})
        print(f"Added key: {r.json()}")

    # 4. List keys
    r = client.get("/admin/keys", params={"provider": "kimchi"})
    print("Keys:", r.json())

    # 5. Test chat completion (dry-run — will fail without real keys)
    payload = {
        "model": "minimax-m2.7",
        "messages": [{"role": "user", "content": "Hello!"}],
        "stream": False,
        "max_tokens": 100,
    }
    r = client.post("/v1/chat/completions", json=payload)
    print("Chat response status:", r.status_code)
    if r.status_code == 200:
        print("Response:", json.dumps(r.json(), indent=2)[:500])
    else:
        print("Error:", r.text[:500])

    client.close()

if __name__ == "__main__":
    test()
