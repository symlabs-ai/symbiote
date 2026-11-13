"""demo.env — the demo's only configuration file.

A plain ``KEY=VALUE`` file next to this module, ignored by git and written with
mode 0600 because it holds an API key. Values already in the process
environment win over the file, so ``DEMO_MODEL=... python -m demo`` works for
a one-off run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
ENV_PATH = DEMO_DIR / "demo.env"
DATA_DIR = DEMO_DIR / "data"

PROVIDERS = ("anthropic", "openai", "symgateway")
KEY_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    # The gateway key replaces the provider key. The same variable also
    # enables web_search when SYMGATEWAY_BASE_URL is set.
    "symgateway": "SYMGATEWAY_API_KEY",
}
SYMGATEWAY_DEFAULT_URL = "https://symgateway.symlabs.ai"
# (main model, auxiliary model). The auxiliary one runs the background work:
# LLM reflection, harness evolution, semantic tool selection, dream, skills.
DEFAULT_MODELS = {
    "anthropic": ("claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"),
    "openai": ("gpt-4.1", "gpt-4.1-mini"),
    # No defaults: the models a gateway key may use come from GET /v1/models.
    "symgateway": ("", ""),
}


@dataclass
class Settings:
    provider: str = ""
    api_key: str = ""
    model: str = ""
    aux_model: str = ""
    port: int = 8010
    symgateway_api_key: str = ""
    symgateway_base_url: str = ""
    symgateway_project: str = ""

    @property
    def complete(self) -> bool:
        if self.provider == "symgateway" and not self.model:
            return False
        return self.provider in PROVIDERS and bool(self.api_key)

    @property
    def gateway_url(self) -> str:
        return (self.symgateway_base_url or SYMGATEWAY_DEFAULT_URL).rstrip("/")

    @property
    def key_var(self) -> str:
        return KEY_VARS.get(self.provider, "")

    def public(self) -> dict:
        """What the browser may see: never the key itself."""
        return {
            "provider": self.provider,
            "model": self.model,
            "aux_model": self.aux_model,
            "api_key": mask(self.api_key),
            "web_search": bool(self.symgateway_api_key and self.symgateway_base_url),
            "gateway_url": self.gateway_url if self.provider == "symgateway" else "",
            "gateway_project": self.symgateway_project,
        }


def mask(key: str) -> str:
    if not key:
        return ""
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "…"


def read_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load(path: Path = ENV_PATH) -> Settings:
    file_values = read_env_file(path)

    def get(name: str, default: str = "") -> str:
        return os.environ.get(name) or file_values.get(name) or default

    provider = get("DEMO_PROVIDER").lower()
    if provider not in PROVIDERS:
        # A key alone is enough to pick the provider.
        provider = next((p for p in PROVIDERS if get(KEY_VARS[p])), "")
    main, aux = DEFAULT_MODELS.get(provider, ("", ""))
    model = get("DEMO_MODEL", main)
    try:
        port = int(get("DEMO_PORT", "8010"))
    except ValueError:
        port = 8010
    return Settings(
        provider=provider,
        api_key=get(KEY_VARS[provider]) if provider else "",
        model=model,
        # Through the gateway the auxiliary model defaults to the main one.
        aux_model=get("DEMO_AUX_MODEL", aux or model),
        port=port,
        symgateway_api_key=get("SYMGATEWAY_API_KEY"),
        symgateway_base_url=get("SYMGATEWAY_BASE_URL"),
        symgateway_project=get("SYMGATEWAY_PROJECT_SLUG"),
    )


def save(updates: dict[str, str], path: Path = ENV_PATH) -> None:
    """Merge ``updates`` into the file, keeping comments and other keys."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in pending:
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(line)
    out.extend(f"{k}={v}" for k, v in pending.items())
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    path.chmod(0o600)
