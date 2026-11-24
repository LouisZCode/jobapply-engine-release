"""llm.py — the app's own "brains": Claude CLI or any stored provider API key.

The CV pre-fill (and later other app-side helper tasks) should run with WHATEVER
intelligence the user connected, not hard-require the Claude CLI. A brain is:

  claude   the `claude` binary, probed+connected on Connections (existing path)
  zai/codex/openrouter   an API key stored in app/keys.json (lib/keys.py)

Key-brain calls are plain OpenAI-compatible chat completions (all three providers
speak that protocol) — the key stays in this process, only ever sent to the
provider's own API. No CLI to install, no env injection, no restart needed after
saving a key (unlike engine runs, which inject env at spawn).

Model ids: per-provider, user-editable. Zai defaults to glm-5.3-flash (Luis,
2026-11-08); codex/openrouter have no safe default — the user sets one when
picking those brains.
"""

import json
import os
import urllib.error
import urllib.request

from . import keys, state

DEFAULT_MODELS = {"zai": "glm-5.3-flash", "codex": None, "openrouter": None}
# SINGLE SOURCE OF TRUTH for "which model does this provider run": the Profile brain
# picker pre-fills from here, cv_extract uses model_for(), and the engine's WORKER port
# (run_config WORKER_MODEL) must default from this same table — Luis, 2026-11-08:
# pre-fill and launch must always name the SAME model per provider. These models are
# confirmed strong enough for PDF→profile extraction (glm-5.3-flash proved it live).

_CHAT_URLS = {
    "zai": "https://api.z.ai/api/paas/v4/chat/completions",
    "codex": "https://api.openai.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}


def brains():
    """Ordered [{id, label, kind}] — Claude first when actually connected (not merely
    installed: the binary can exist while the user has no Claude account at all —
    Luis, 2026-11-08), then stored keys. Empty list = nothing can think."""
    out = []
    if state.load_state().get("claude_connected") and state.claude_cli_path() is not None:
        out.append({"id": "claude", "label": "Claude", "kind": "cli"})
    for pid, meta in keys.PROVIDERS.items():
        if (keys.hints().get(pid)):
            out.append({"id": pid, "label": meta["label"], "kind": "key"})
    return out


def model_for(provider, override=None):
    """Effective model id for a key-brain: explicit override > saved choice > default."""
    if override:
        return override.strip()
    saved = (state.load_state().get("llm_models") or {}).get(provider)
    return saved or DEFAULT_MODELS.get(provider)


def remember_model(provider, model):
    models = state.load_state().get("llm_models") or {}
    models[provider] = model.strip()
    state.update_state(llm_models=models)


def chat(provider, system, user, model, timeout=120):
    """{ok, text|message} — one OpenAI-compatible completion with the stored key."""
    url = _CHAT_URLS.get(provider)
    # the real key only exists in the store — hints are sanitized on purpose
    key = keys.get_key(provider)
    if not url or not key:
        return {"ok": False, "message": "No key stored for {}.".format(provider)}
    if not model:
        return {"ok": False, "message": "Set a model id for {} first.".format(provider)}
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if not text.strip():
            return {"ok": False, "message": "{} returned an empty answer.".format(provider)}
        return {"ok": True, "text": text}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "message": "{} rejected the call (HTTP {}).".format(provider, exc.code)}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "message": "{} could not be reached — {}.".format(provider, exc)}
