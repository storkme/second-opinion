"""Register the active provider (OpenRouter OR local llama-server) into pi's models.json.

pi reads provider/model definitions from ~/.pi/agent/models.json. We **merge** our entry
into that file, preserving any other providers the user already configured, so running the
CLI locally doesn't clobber an existing pi setup. Override the path with PI_MODELS_PATH.

Two providers, keyed in pi by `PROVIDER`:
  openrouter -> pi provider "openrouter" (hosted, paid; needs OPENROUTER_API_KEY)
  local      -> pi provider "llama"      (a llama.cpp llama-server; free; needs LLAMA_SERVER_URL)

The OpenRouter key is written here in cleartext, so we chmod the file to 600 — pi reads the
key from this file, not the environment (see run.py run_pass + README Security).
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

# The single source of truth for the OpenRouter default model (carry-forward #5).
# action.yml mirrors this literal as its input default (YAML can't import it).
DEFAULT_MODEL = "z-ai/glm-5.2"


def pi_provider(provider: str) -> str:
    """pi's provider key for our PROVIDER value."""
    return "openrouter" if provider == "openrouter" else "llama"


def _reasoning() -> bool:
    # Honored for BOTH providers (carry-forward #3 — the old daemon hardcoded this true).
    # The default model (GLM 5.2) is a reasoning model; set PI_REASONING=false for a
    # non-reasoning model so pi doesn't send reasoning params it can't use.
    return os.environ.get("PI_REASONING", "true").strip().lower() in ("1", "true", "yes", "on")


def write_models_json(model: str) -> Path:
    """Register `model` for the active PROVIDER into models.json; return the file path."""
    provider = os.environ.get("PROVIDER", "openrouter").strip().lower()
    reasoning = _reasoning()
    max_tokens = int(os.environ.get("PI_MAX_TOKENS", "32768"))

    if provider == "local":
        base = os.environ.get("LLAMA_SERVER_URL", "").strip().rstrip("/")
        if not base:
            raise SystemExit("LLAMA_SERVER_URL is required for PROVIDER=local")
        key = "not-needed"                       # llama-server ignores auth
        ctx = int(os.environ.get("PI_CONTEXT_WINDOW", "65536"))   # local servers run smaller windows
    else:
        base = (os.environ.get("OPENROUTER_BASE_URL", "").strip().rstrip("/")
                or "https://openrouter.ai/api")
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise SystemExit("OPENROUTER_API_KEY is required for PROVIDER=openrouter")
        ctx = int(os.environ.get("PI_CONTEXT_WINDOW", "1048576"))

    entry = {
        "baseUrl": f"{base}/v1",
        "api": "openai-completions",
        "apiKey": key,
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": reasoning},
        "models": [{"id": model, "name": f"{model} ({provider})", "reasoning": reasoning,
                    "contextWindow": ctx, "maxTokens": max_tokens}],
    }

    path = Path(os.environ.get("PI_MODELS_PATH", "").strip()
                or os.path.expanduser("~/.pi/agent/models.json"))
    path.parent.mkdir(parents=True, exist_ok=True)

    # Merge: keep whatever is already there, replace only our provider key.
    cfg: dict = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    providers[pi_provider(provider)] = entry
    cfg["providers"] = providers

    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600 — the apiKey lives here in cleartext
    except OSError:
        pass
    print(f"[providers] registered {provider} model={model} reasoning={reasoning} -> {path}",
          flush=True)
    return path


if __name__ == "__main__":
    write_models_json(os.environ.get("MODEL", "").strip() or DEFAULT_MODEL)
