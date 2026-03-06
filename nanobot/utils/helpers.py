"""Utility functions for nanobot."""

import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanobot.config.schema import IdentityConfig


def ensure_dir(path: Path) -> Path:
    """Ensure directory exists, return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_workspace_path(workspace: str | None = None) -> Path:
    """Resolve and ensure workspace path. Defaults to ~/.nanobot/workspace."""
    path = Path(workspace).expanduser() if workspace else Path.home() / ".nanobot" / "workspace"
    return ensure_dir(path)


def timestamp() -> str:
    """Current ISO timestamp."""
    return datetime.now().isoformat()


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*]')


def safe_filename(name: str) -> str:
    """Replace unsafe path characters with underscores."""
    return _UNSAFE_CHARS.sub("_", name).strip()


def sync_workspace_templates(workspace: Path, silent: bool = False) -> list[str]:
    """Sync bundled templates to workspace. Only creates missing files."""
    from importlib.resources import files as pkg_files

    try:
        tpl = pkg_files("nanobot") / "templates"
    except Exception:
        return []
    if not tpl.is_dir():
        return []

    added: list[str] = []

    def _write(src, dest: Path):
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8") if src else "", encoding="utf-8")
        added.append(str(dest.relative_to(workspace)))

    for item in tpl.iterdir():
        if item.name.endswith(".md"):
            _write(item, workspace / item.name)
    _write(tpl / "memory" / "MEMORY.md", workspace / "memory" / "MEMORY.md")
    _write(None, workspace / "memory" / "HISTORY.md")
    (workspace / "skills").mkdir(exist_ok=True)

    if added and not silent:
        from rich.console import Console

        for name in added:
            Console().print(f"  [dim]Created {name}[/dim]")
    return added


# ---------------------------------------------------------------------------
# Discussion principles constants
# ---------------------------------------------------------------------------

_RESEARCH_PRINCIPLES = """\
## Research Before Answering

When answering task-oriented questions, default to searching for external information
before providing conclusions. Output should include a conclusion, a research summary,
and uncertainty notes (if any). Avoid giving only subjective judgments.

### Trigger Conditions (any one triggers research)
- The question involves external facts, recent information, technical comparisons, \
or official documentation.
- There is a knowledge gap not covered by the current conversation or memory.
- The manager explicitly requests evidence or sources.

### Execution Flow
1. When the question exceeds current context or involves factual gaps, request and \
suggest calling `web_search` to supplement external information.
2. Let the model dynamically decide search and `web_fetch` depth based on question \
complexity — do not lock to a fixed number of rounds.
3. Form a bullet-point summary internally before posting a structured reply in the group.

### Output Format
- **Conclusion**: actionable conclusion first.
- **Evidence Summary**: 2–4 key points from research.
- **Next Steps**: suggested actions or items to confirm.

### Exceptions (skip research to avoid delay)
- Pure progress updates (e.g. "I'm working on X, ETA Y").
- Repeated confirmations of information just provided and not requiring external validation.
- Questions answerable from local files or code without external lookup."""

_TOPIC_SELECTION_PRINCIPLES = """\
## Topic Selection Principles

When initiating a new discussion topic, choose questions that satisfy these criteria:

1. **Requires distributed cognition, not solvable by a single agent**: The question \
must need both "research/analysis" and "verification/modeling" thinking to reach a \
conclusion, giving ICs clear division of labor. If one IC can give a final answer in \
30 seconds, the question is too simple.
2. **No single correct answer — trade-off space exists**: The best topics conclude with \
"choose among A/B/C and justify" rather than "right or wrong." Pure math or algorithm \
problems with unique correct answers are unsuitable.
3. **Requires external information retrieval**: The question contains factual gaps that \
ICs cannot answer from internal knowledge alone (e.g. latest data, industry cases, \
regulatory details), forcing use of `web_search` + `web_fetch`.
4. **Naturally supports multi-round iteration**: One IC's intermediate output changes \
the other IC's direction. For example, IC_1's research overturns IC_2's initial \
assumption, requiring IC_2 to revise and cross-validate.
5. **Decomposable into sub-tasks**: After the question is posed, ICs can self-organize \
into "I handle X, you handle Y" mode rather than both doing the same thing.

### Unsuitable Question Types
- Pure math (solving equations, proofs) — single-agent deep reasoning is more efficient.
- Standard algorithm problems (LeetCode, ACM) — linear reasoning chain, no need for \
division of labor or research.
- Simple fact queries ("What is the capital of X") — one search round suffices, no \
discussion needed.
- Subjective chitchat ("What color do you like") — cannot converge, no termination \
condition."""


def generate_identity_md(identity: "IdentityConfig") -> str:
    """Render an IdentityConfig into Markdown for the workspace IDENTITY.md file.

    The generated file is loaded by ContextBuilder via BOOTSTRAP_FILES,
    injecting role identity into the system prompt (spec §3.2).
    """
    lines = ["# Role Identity", ""]

    display = identity.display_name or identity.role_id or "Agent"
    role_id = identity.role_id or "unknown"
    lines.append(f"You are **{display}** (roleId: {role_id}).")
    lines.append("")

    # Organization
    lines.append("## Organization")
    org = identity.org
    if org.subordinates:
        lines.append(f"- Your subordinates: {', '.join(org.subordinates)}")
    if org.manager:
        lines.append(f"- Your manager: {org.manager}")
    else:
        lines.append("- You have no manager.")
    if org.peers:
        lines.append(f"- Your peers: {', '.join(org.peers)}")
    lines.append("")

    # Persona
    persona = identity.persona
    lines.append("## Persona")
    if persona.style:
        lines.append(f"- Style: {persona.style}")
    if persona.goals:
        lines.append(f"- Goals: {', '.join(persona.goals)}")
    if persona.dont:
        lines.append(f"- Don't: {', '.join(persona.dont)}")
    lines.append("")

    # Reply rules
    lines.append("## Reply Rules")
    lines.append("- You see ALL messages in the group chat. Not every message requires your reply.")
    lines.append(
        "- Only reply when: you are explicitly mentioned, "
        "the topic falls within your responsibilities, "
        "or the discussion has stalled and needs your intervention."
    )
    lines.append("- When other team members are having a productive exchange, observe silently.")
    lines.append("")

    # Team Directory
    if identity.team_members:
        lines.append("## Team Directory")
        for rid, dname in identity.team_members.items():
            marker = " ← you" if rid == role_id else ""
            lines.append(f"- {dname} ({rid}){marker}")
        lines.append("")

    # Mention Recognition
    if identity.team_members:
        lines.append("## Mention Recognition")
        lines.append(
            f"Your display name is **{display}**. "
            f'When another participant writes "@{display}" '
            f"or addresses you by name, that counts as being explicitly mentioned."
        )
        lines.append("")

    # Discussion principles (spec §4.5, §4.6)
    dp = identity.discussion_principles
    if dp.research_before_answer:
        lines.append(_RESEARCH_PRINCIPLES)
        lines.append("")
    if dp.topic_selection:
        lines.append(_TOPIC_SELECTION_PRINCIPLES)
        lines.append("")
    if dp.extra_rules:
        lines.append("## Additional Discussion Rules")
        for rule in dp.extra_rules:
            lines.append(f"- {rule}")
        lines.append("")

    return "\n".join(lines)
