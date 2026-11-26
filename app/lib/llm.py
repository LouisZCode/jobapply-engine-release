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

BRAIN-001b adds the APPLICATION brain (which AI fills in the forms on engine runs):
application_options()/application_brain()/set_application_brain() below — a
separate choice from this module's helper brains, saved to app_state and pushed to
the engine as run_config WORKER/WORKER_MODEL switches.
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

# BRAIN-001b: the AI that fills in APPLICATIONS (engine runs), keyed by the option id.
# claude runs through the Claude CLI; every other id runs through pi, whose model string
# is provider/model and goes to `pi --model` verbatim. The saved per-id models live in
# app_state (application_models), the active choice in application_brain/application_model.
APPLICATION_MODELS = {
    "claude": "claude-haiku-5-5",
    "zai": "zai/glm-5.3-flash",
    "zai_cn": "zai-coding-cn/glm-5.3-flash",
    "openrouter": "openrouter/z-ai/glm-5.3-flash",
    "codex": "openai/gpt-5.4-mini",
}
# Which harness an option runs through — every id not listed here runs through pi.
HARNESS = {"claude": "claude"}

_CHAT_URLS = {
    "zai": "https://api.z.ai/api/paas/v4/chat/completions",
    "zai_cn": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
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


# --- BRAIN-001b: which AI runs applications -------------------------------------------

def application_options():
    """Ordered [{id, label, harness, model, available, why_not}] — every way an
    application could currently be filled in, for the Connections page's picker:
    Claude first (needs a connected login AND the CLI), then each stored key provider
    (needs pi). Only key providers with a key hint in the store are listed. `model` is
    the saved model for that id or the default (APPLICATION_MODELS)."""
    st = state.load_state()
    hints = keys.hints()
    saved = st.get("application_models") or {}
    out = []
    for pid in APPLICATION_MODELS:
        model = saved.get(pid) or APPLICATION_MODELS[pid]
        harness = HARNESS.get(pid, "pi")
        if pid == "claude":
            available = bool(st.get("claude_connected")) and state.claude_cli_path() is not None
            if available:
                why_not = ""
            elif not st.get("claude_connected"):
                why_not = "Connect Claude above first."
            else:
                why_not = "Claude is not installed."
            out.append({
                "id": "claude",
                "label": "Claude",
                "harness": harness,
                "model": model,
                "available": available,
                "why_not": why_not,
            })
            continue
        meta = keys.PROVIDERS.get(pid) or {}
        if pid not in hints:
            continue  # only providers whose key is actually stored
        available = state.pi_cli_path() is not None
        out.append({
            "id": pid,
            "label": meta.get("label") or pid,
            "harness": harness,
            "model": model,
            "available": available,
            "why_not": "" if available else
                       "pi is not installed — run `curl -fsSL https://pi.dev/install.sh | sh` once.",
        })
    return out


def application_brain():
    """(id, model, option) for the saved choice — or (None, None, None) when nothing
    is saved or the saved option is no longer available (key removed, CLI gone…).
    The Connections page uses this to pre-check the radio and to warn when the choice
    stopped working."""
    st = state.load_state()
    brain_id = (st.get("application_brain") or "").strip()
    if not brain_id:
        return None, None, None
    model = (st.get("application_model") or "").strip()
    for option in application_options():
        if option["id"] == brain_id and option["available"]:
            return brain_id, model or option["model"], option
    return None, None, None


def set_application_brain(brain_id, model):
    """Validates and saves the application AI, then writes the engine's switches
    (run_config WORKER/WORKER_MODEL — read fresh by every run). Raises ValueError with
    a short message on a bad choice; returns the validated option. actions is imported
    here, not at the top, to stay out of any import cycle.
    """
    brain_id = (brain_id or "").strip()
    model = (model or "").strip()
    option = next((o for o in application_options() if o["id"] == brain_id), None)
    if option is None:
        raise ValueError("Pick one of the options above.")
    if not option["available"]:
        raise ValueError("{} isn't available right now — {}".format(option["label"], option["why_not"]))
    if not model:
        raise ValueError("The model can't be empty.")
    if " " in model:
        raise ValueError("The model can't contain spaces — it's one id, like zai/glm-5.3-flash.")
    if len(model) > 80:
        raise ValueError("The model is too long (80 characters at most).")

    models = dict(state.load_state().get("application_models") or {})
    models[brain_id] = model
    state.update_state(
        application_brain=brain_id,
        application_model=model,
        application_models=models,
    )
    from . import actions
    actions.set_run_config_keys({"WORKER": option["harness"], "WORKER_MODEL": model})
    return option


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
