"""Shared LLM utilities for classify_idle.py and review_events.py.

Provides dual-endpoint LLM calling with automatic Anthropic/OpenAI format detection.
"""

import json
import os


def is_anthropic_format(base_url, fmt=None):
    if fmt and fmt.strip():
        return fmt.lower().strip() == "anthropic"
    return "anthropic" in base_url.lower()


def extract_json_from_text(text):
    if not text:
        return None
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        brace = text.find('{', idx)
        if brace == -1:
            break
        try:
            result, _ = decoder.raw_decode(text, brace)
            return result
        except json.JSONDecodeError:
            idx = brace + 1
    return None


def call_llm(base_url, api_key, model, prompt, fmt=None):
    """Call a single LLM endpoint. Returns parsed JSON dict or None."""
    import urllib.request

    is_anthropic = is_anthropic_format(base_url, fmt)

    if is_anthropic:
        url = f"{base_url}/v1/messages"
        payload = json.dumps({
            "model": model,
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    else:
        url = f"{base_url}/chat/completions"
        payload = json.dumps({
            "model": model,
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

    req = urllib.request.Request(url, data=payload, headers=headers)
    resp = urllib.request.urlopen(req, timeout=60)
    data = json.loads(resp.read().decode())

    if is_anthropic:
        text = ""
        for item in data.get("content", []):
            if item.get("type") == "text":
                text = item.get("text", "")
                break
    else:
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")

    return extract_json_from_text(text)


def get_llm_endpoints():
    """Return list of (base_url, api_key, model, fmt) for primary + fallback."""
    endpoints = []
    api_key = os.environ.get("WATCHDOG_LLM_API_KEY", "")
    if api_key:
        endpoints.append((
            os.environ.get("WATCHDOG_LLM_BASE_URL", "https://api.anthropic.com"),
            api_key,
            os.environ.get("WATCHDOG_LLM_MODEL", "claude-haiku-4-5-20251001"),
            os.environ.get("WATCHDOG_LLM_FORMAT", ""),
        ))
    api_key_2 = os.environ.get("WATCHDOG_LLM_API_KEY_2", "")
    if api_key_2:
        endpoints.append((
            os.environ.get("WATCHDOG_LLM_BASE_URL_2", "https://api.anthropic.com"),
            api_key_2,
            os.environ.get("WATCHDOG_LLM_MODEL_2", "claude-haiku-4-5-20251001"),
            os.environ.get("WATCHDOG_LLM_FORMAT_2", ""),
        ))
    return endpoints


def call_llm_with_fallback(prompt):
    """Try all configured LLM endpoints. Returns parsed JSON dict or None."""
    for base_url, api_key, model, fmt in get_llm_endpoints():
        try:
            result = call_llm(base_url, api_key, model, prompt, fmt=fmt)
            if result:
                return result
        except Exception:
            continue
    return None
