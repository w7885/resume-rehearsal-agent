from __future__ import annotations

from pathlib import Path

PROMPT_VERSION = "2026-10-21.1"
_DIR = Path(__file__).parent


def _read(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8")


def build_system_prompt() -> str:
    return _read("system")


def build_decide_prompt(tools_text: str) -> str:
    return _read("decide").replace("{{TOOLS}}", tools_text)


def build_route_prompt() -> str:
    return _read("route")
