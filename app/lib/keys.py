"""app/lib/keys.py — Intelligence API-key store (Zai / Codex / OpenRouter).

Storage: one gitignored JSON file, app/keys.json, chmod 600 — the same
file-based-secret pattern the engine already uses for Gmail OAuth tokens
(inbox_reader/token.json). The UI (Connections page) and the tower (env injection
at spawn) both read THIS file; the tower's Electron safeStorage copy is only a
legacy fallback for keys added before this store existed.

Laws: key material is written once here and read for env injection or hint
rendering — never rendered back to a page, never logged, never committed.
Hints are provider + last-4 + length, nothing more.
"""
import datetime
import json
import os

from . import paths

PROVIDERS = {
    "zai": {
        "label": "Zai (GLM)",
        "env": "ZAI_API_KEY",
        "base_env": "ZAI_BASE_URL",
        "base_url": "https://api.z.ai/api/paas/v4",
        "blurb": "Add your Zai key — the engine can run on GLM models via the pi worker.",
    },
    "zai_cn": {
        "label": "Z.ai (China)",
        "env": "ZAI_CODING_CN_API_KEY",
        "base_env": "ZAI_CODING_CN_BASE_URL",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "blurb": "Add your Z.ai (China) key — the engine can run on GLM models via the pi worker.",
    },
    "codex": {
        "label": "Codex / OpenAI",
        "env": "OPENAI_API_KEY",
        "base_env": None,
        "base_url": None,
        "blurb": "Add your OpenAI key — for Codex/OpenAI models.",
    },
    "openrouter": {
        "label": "OpenRouter",
        "env": "OPENROUTER_API_KEY",
        "base_env": None,
        "base_url": None,
        "blurb": "Add your OpenRouter key — one key, many models.",
    },
}

# MULTI-001: under a per-user data root the key store lives at $DATA_ROOT/app/keys.json
# (default unchanged: app/keys.json next to the server). _save() makedirs the parent.
_KEYS_DIR = os.path.join(paths.DATA_ROOT, "app") if paths.DATA_ROOT else paths.APP_DIR
KEYS_FILE = os.path.join(
    _KEYS_DIR,
    "keys.test.json" if os.environ.get("APP_TEST") == "1" else "keys.json",
)
# APP_TEST=1 writes keys.test.json instead — automated curl walkthroughs must NEVER
# clobber a real saved key (that exact mistake deleted Luis's key on 2026-11-08).


def get_key(provider):
    """The stored key for a provider, or "" — used by llm.py's chat() (in-process,
    never rendered). Not for templates: hints() is the render-safe view."""
    return (_load().get(provider) or {}).get("key") or ""


def get_entry(provider):
    """The provider's full stored entry ({key, added_at} or None) — for save-rollback
    when the save-time test rejects a new key (the store only holds tested-good keys)."""
    return _load().get(provider)


def restore_entry(provider, entry):
    """Put back a previous entry (or delete the provider when entry is None) after a
    failed save-test. Same atomic-write path as save/remove."""
    data = _load()
    if entry:
        data[provider] = entry
    else:
        data.pop(provider, None)
    _save(data)


# --- "quickly test it": one cheap authenticated GET per provider -----------------
# GET {base}/models (OpenAI-compatible) or the provider's key-introspection endpoint.
# A 200 means the key authenticates — enough to know the path works. The key only
# ever goes to the provider's own API; never logged, never in a hint.

_CHECK_URLS = {
    "zai": "https://api.z.ai/api/paas/v4/models",
    "zai_cn": "https://open.bigmodel.cn/api/paas/v4/models",
    "codex": "https://api.openai.com/v1/models",
    "openrouter": "https://openrouter.ai/api/v1/key",
}


def test_key(provider, timeout=15):
    """{ok, message} — validates the STORED key (test what's saved). Blocking
    urllib; the route runs it in a threadpool."""
    import urllib.error
    import urllib.request

    meta = PROVIDERS.get(provider)
    key = (_load().get(provider) or {}).get("key") or ""
    url = _CHECK_URLS.get(provider)
    if not meta or not key or not url:
        return {"ok": False, "message": "Save the key first, then test it."}
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(8192)
            n = -1
            try:
                n = len(json.loads(body).get("data", []))
            except Exception:
                pass
            detail = " — {} models visible".format(n) if n >= 0 else ""
            return {"ok": True, "message": "{} accepted the key{}.".format(meta["label"], detail)}
    except urllib.error.HTTPError as exc:
        reason = {401: "rejected the key (unauthorized)", 403: "rejected the key (forbidden)"}.get(
            exc.code, "failed (HTTP {})".format(exc.code))
        return {"ok": False, "message": "{} {}.".format(meta["label"], reason)}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "message": "{} could not be reached — {}.".format(meta["label"], exc)}


def _load():
    try:
        with open(KEYS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(data):
    os.makedirs(os.path.dirname(KEYS_FILE), exist_ok=True)
    tmp = KEYS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, KEYS_FILE)  # atomic — a reader never sees half a file


def save_key(provider, value):
    """Returns (ok, message_or_hint). Validates shape; stores; returns a HINT."""
    meta = PROVIDERS.get(provider)
    value = str(value or "").strip()
    if not meta:
        return False, "Unknown provider."
    if len(value) < 12:
        return False, "That doesn't look like an API key (too short)."
    data = _load()
    existing = data.get(provider) or {}
    data[provider] = {
        "key": value,
        # keep the ORIGINAL added_at when replacing — it's when the user first set
        # this provider up, not when the stored value last changed.
        "added_at": existing.get("added_at")
        or datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    _save(data)
    return True, {"last4": value[-4:], "length": len(value)}


def remove_key(provider):
    if provider not in PROVIDERS:
        return False
    data = _load()
    had = provider in data
    data.pop(provider, None)
    _save(data)
    return had


def hints():
    """{provider: {last4, length, added_at}} for the Connections page — no material."""
    data = _load()
    out = {}
    for provider, entry in data.items():
        key = entry.get("key") or ""
        if provider in PROVIDERS and key:
            out[provider] = {
                "last4": key[-4:],
                "length": len(key),
                "added_at": entry.get("added_at", ""),
            }
    return out


def env_map():
    """{ENV_NAME: value[, BASE_ENV: url]} for spawn-time injection (tower). The only
    other reader of key material besides save/hints."""
    env = {}
    for provider, entry in _load().items():
        meta = PROVIDERS.get(provider)
        key = (entry or {}).get("key") or ""
        if meta and key:
            env[meta["env"]] = key
            if meta["base_env"] and meta["base_url"]:
                env[meta["base_env"]] = meta["base_url"]
    return env
