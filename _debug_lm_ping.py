"""Быстрый ping LM Studio — проверяем, что модель действительно отвечает текстом."""
from __future__ import annotations

import json
import time
import urllib.request


BASE = "http://192.168.1.157:1234"
MODELS_TO_TRY = [
    "openai/gpt-oss-120b",
    "yandexgpt-5-lite-8b-instruct",
]


def chat(model: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Отвечай одним словом."},
            {"role": "user", "content": "Скажи слово: pong"},
        ],
        "max_tokens": 16,
        "temperature": 0.0,
    }
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    dt = time.time() - t0
    return f"[{dt:.1f}s] {body[:600]}"


if __name__ == "__main__":
    for m in MODELS_TO_TRY:
        try:
            print(f"=== {m} ===")
            print(chat(m))
        except Exception as e:
            print(f"!!! {m}: {e}")
