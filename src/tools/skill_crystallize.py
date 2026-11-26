"""Skill crystallization: distill a successful tool run into a reusable skill draft."""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.tools.ai_sessions import _clean as _clean_secrets

_FAIL_PREFIXES = ("error:", "stopped after")
_SKIP_TOOLS = frozenset({"search_tools", "load_mcp_tools", "remember", "add_skill", "update_skill", "crystallize_skill"})


@dataclass
class SkillDraft:
    name: str
    description: str
    instructions: str


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", (text or "").lower()).strip("-_")
    return (slug[:40].strip("-_") or "learned-workflow")


def _is_failed(output: str) -> bool:
    low = (output or "").strip().lower()
    if low.startswith(_FAIL_PREFIXES):
        return True
    if low.startswith("exit code ") and not low.startswith("exit code 0"):
        return True
    return False


def draft_skill_from_run(
    goal: str,
    ran: list[tuple[str, str]],
    reply: str = "",
    name: str = "",
    description: str = "",
) -> SkillDraft:
    """Turn a completed run's successful steps into a clean, secret-scrubbed Markdown skill."""
    clean_goal = " ".join(_clean_secrets(goal or "").split())[:200] or "Reusable workflow"
    skill_name = _slugify(name or clean_goal)
    skill_desc = " ".join(_clean_secrets(description or clean_goal).split())[:140]

    steps: list[str] = []
    seen: set[str] = set()
    verifications: list[str] = []
    for call_note, output in ran:
        tool_name = call_note.split("(", 1)[0].strip()
        if tool_name in _SKIP_TOOLS or _is_failed(output):
            continue
        cleaned_call = _clean_secrets(call_note).strip()
        if not cleaned_call or cleaned_call in seen:
            continue
        seen.add(cleaned_call)
        if tool_name in ("run_command", "run_powershell", "run_python", "layout_check"):
            verifications.append(cleaned_call)
        steps.append(f"- `{cleaned_call}`")

    lines = [
        f"## Goal\n{clean_goal}",
        "\n## Steps\n" + ("\n".join(steps[:15]) if steps else "- Follow the user's verified workflow."),
    ]
    if verifications:
        lines.append("\n## Verification\n" + "\n".join(f"- `{v}`" for v in verifications[:5]))
    clean_reply = " ".join(_clean_secrets(reply or "").split())
    if clean_reply:
        lines.append(f"\n## Notes\n{clean_reply[:300]}")
    return SkillDraft(name=skill_name, description=skill_desc, instructions="\n".join(lines).strip())
