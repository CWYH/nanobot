"""Tests for generate_identity_md() in nanobot.utils.helpers."""

from nanobot.config.schema import (
    DiscussionPrinciplesConfig,
    IdentityConfig,
    OrgConfig,
    PersonaConfig,
)
from nanobot.utils.helpers import generate_identity_md

# ---------------------------------------------------------------------------
# Manager identity
# ---------------------------------------------------------------------------


def test_manager_identity_md():
    identity = IdentityConfig(
        role_id="manager",
        display_name="Manager",
        persona=PersonaConfig(
            style="structured, push-style",
            goals=["break down problems", "assign tasks", "track progress"],
            dont=["do all the work for subordinates"],
        ),
        org=OrgConfig(manager=None, subordinates=["ic_1", "ic_2"]),
    )
    md = generate_identity_md(identity)

    assert "# Role Identity" in md
    assert "You are **Manager** (roleId: manager)." in md
    assert "## Organization" in md
    assert "Your subordinates: ic_1, ic_2" in md
    assert "You have no manager." in md
    assert "## Persona" in md
    assert "Style: structured, push-style" in md
    assert "Goals: break down problems, assign tasks, track progress" in md
    assert "Don't: do all the work for subordinates" in md
    assert "## Reply Rules" in md
    assert "observe silently" in md


# ---------------------------------------------------------------------------
# IC identity
# ---------------------------------------------------------------------------


def test_ic_identity_md():
    identity = IdentityConfig(
        role_id="ic_1",
        display_name="IC-1",
        persona=PersonaConfig(
            style="research-oriented",
            goals=["analyze", "investigate"],
        ),
        org=OrgConfig(manager="manager"),
    )
    md = generate_identity_md(identity)

    assert "You are **IC-1** (roleId: ic_1)." in md
    assert "Your manager: manager" in md
    # No subordinates line for IC
    assert "Your subordinates:" not in md


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_identity_md_no_display_name_uses_role_id():
    identity = IdentityConfig(role_id="ic_2", display_name="")
    md = generate_identity_md(identity)
    assert "You are **ic_2** (roleId: ic_2)." in md


def test_identity_md_no_role_id_no_display_name():
    identity = IdentityConfig(role_id="", display_name="")
    md = generate_identity_md(identity)
    assert "You are **Agent** (roleId: unknown)." in md


def test_identity_md_empty_persona():
    identity = IdentityConfig(
        role_id="bot",
        display_name="Bot",
        persona=PersonaConfig(),
        org=OrgConfig(),
    )
    md = generate_identity_md(identity)
    assert "## Persona" in md
    # No style/goals/dont lines when empty
    assert "Style:" not in md
    assert "Goals:" not in md
    assert "Don't:" not in md


def test_identity_md_with_peers():
    identity = IdentityConfig(
        role_id="ic_1",
        display_name="IC-1",
        org=OrgConfig(manager="manager", peers=["ic_2"]),
    )
    md = generate_identity_md(identity)
    assert "Your peers: ic_2" in md


def test_identity_md_no_peers_omitted():
    identity = IdentityConfig(
        role_id="ic_1",
        display_name="IC-1",
        org=OrgConfig(manager="manager"),
    )
    md = generate_identity_md(identity)
    assert "peers" not in md.lower() or "Your peers:" not in md


def test_identity_md_multiple_dont():
    identity = IdentityConfig(
        role_id="manager",
        display_name="Manager",
        persona=PersonaConfig(dont=["guess", "skip verification", "be rude"]),
    )
    md = generate_identity_md(identity)
    assert "Don't: guess, skip verification, be rude" in md


def test_identity_md_reply_rules_present():
    identity = IdentityConfig(role_id="x", display_name="X")
    md = generate_identity_md(identity)
    assert "You see ALL messages in the group chat." in md
    assert "Only reply when:" in md
    assert "observe silently" in md


def test_identity_md_ends_with_newline():
    identity = IdentityConfig(role_id="x", display_name="X")
    md = generate_identity_md(identity)
    assert md.endswith("\n")


def test_identity_md_written_to_workspace(tmp_path):
    """Integration: write IDENTITY.md to workspace and verify it can be read."""
    identity = IdentityConfig(
        role_id="manager",
        display_name="Manager",
        persona=PersonaConfig(style="direct", goals=["lead"]),
        org=OrgConfig(subordinates=["ic_1"]),
    )
    md = generate_identity_md(identity)

    identity_file = tmp_path / "IDENTITY.md"
    identity_file.write_text(md, encoding="utf-8")

    content = identity_file.read_text(encoding="utf-8")
    assert "# Role Identity" in content
    assert "Manager" in content


# ---------------------------------------------------------------------------
# Discussion principles in generated MD (spec §4.5, §4.6)
# ---------------------------------------------------------------------------


def test_identity_md_research_principles_included_by_default():
    identity = IdentityConfig(role_id="ic_1", display_name="IC-1")
    md = generate_identity_md(identity)
    assert "## Research Before Answering" in md
    assert "Trigger Conditions" in md
    assert "Execution Flow" in md
    assert "Output Format" in md
    assert "Exceptions" in md


def test_identity_md_topic_selection_off_by_default():
    identity = IdentityConfig(role_id="ic_1", display_name="IC-1")
    md = generate_identity_md(identity)
    assert "## Topic Selection Principles" not in md


def test_identity_md_both_principles_enabled():
    identity = IdentityConfig(
        role_id="manager",
        display_name="Manager",
        discussion_principles=DiscussionPrinciplesConfig(
            research_before_answer=True,
            topic_selection=True,
        ),
    )
    md = generate_identity_md(identity)
    assert "## Research Before Answering" in md
    assert "## Topic Selection Principles" in md
    assert "Requires distributed cognition" in md


def test_identity_md_both_principles_disabled():
    identity = IdentityConfig(
        role_id="bot",
        display_name="Bot",
        discussion_principles=DiscussionPrinciplesConfig(
            research_before_answer=False,
            topic_selection=False,
        ),
    )
    md = generate_identity_md(identity)
    assert "## Research Before Answering" not in md
    assert "## Topic Selection Principles" not in md


def test_identity_md_extra_rules():
    identity = IdentityConfig(
        role_id="bot",
        display_name="Bot",
        discussion_principles=DiscussionPrinciplesConfig(
            extra_rules=["always cite sources", "keep replies under 200 words"],
        ),
    )
    md = generate_identity_md(identity)
    assert "## Additional Discussion Rules" in md
    assert "- always cite sources" in md
    assert "- keep replies under 200 words" in md


def test_identity_md_extra_rules_empty_no_section():
    identity = IdentityConfig(role_id="bot", display_name="Bot")
    md = generate_identity_md(identity)
    assert "## Additional Discussion Rules" not in md


def test_identity_md_full_manager_config():
    """Full manager config: research + topic selection + extra rules."""
    identity = IdentityConfig(
        role_id="manager",
        display_name="Manager",
        persona=PersonaConfig(
            style="structured",
            goals=["drive discussion"],
            dont=["do subordinates' work"],
        ),
        org=OrgConfig(subordinates=["ic_1", "ic_2"]),
        discussion_principles=DiscussionPrinciplesConfig(
            research_before_answer=True,
            topic_selection=True,
            extra_rules=["prefer open-ended questions"],
        ),
    )
    md = generate_identity_md(identity)

    # Structural sections present
    assert "# Role Identity" in md
    assert "## Organization" in md
    assert "## Persona" in md
    assert "## Reply Rules" in md
    assert "## Research Before Answering" in md
    assert "## Topic Selection Principles" in md
    assert "## Additional Discussion Rules" in md
    assert "- prefer open-ended questions" in md


# ---------------------------------------------------------------------------
# Team Directory & Mention Recognition (team_members field)
# ---------------------------------------------------------------------------


def test_identity_md_team_directory_present():
    identity = IdentityConfig(
        role_id="ic_1",
        display_name="IC-1",
        team_members={"manager": "Manager", "ic_1": "IC-1", "ic_2": "IC-2"},
    )
    md = generate_identity_md(identity)
    assert "## Team Directory" in md
    assert "- Manager (manager)" in md
    assert "- IC-1 (ic_1) ← you" in md
    assert "- IC-2 (ic_2)" in md
    # IC-2 should NOT have the ← you marker
    assert "IC-2 (ic_2) ← you" not in md


def test_identity_md_team_directory_manager_marker():
    identity = IdentityConfig(
        role_id="manager",
        display_name="Manager",
        team_members={"manager": "Manager", "ic_1": "IC-1"},
    )
    md = generate_identity_md(identity)
    assert "- Manager (manager) ← you" in md
    assert "- IC-1 (ic_1)" in md
    assert "IC-1 (ic_1) ← you" not in md


def test_identity_md_mention_recognition():
    identity = IdentityConfig(
        role_id="ic_1",
        display_name="IC-1",
        team_members={"manager": "Manager", "ic_1": "IC-1"},
    )
    md = generate_identity_md(identity)
    assert "## Mention Recognition" in md
    assert "Your display name is **IC-1**." in md
    assert '"@IC-1"' in md


def test_identity_md_no_team_members_no_directory():
    identity = IdentityConfig(role_id="ic_1", display_name="IC-1")
    md = generate_identity_md(identity)
    assert "## Team Directory" not in md
    assert "## Mention Recognition" not in md


def test_identity_md_empty_team_members_no_directory():
    identity = IdentityConfig(role_id="ic_1", display_name="IC-1", team_members={})
    md = generate_identity_md(identity)
    assert "## Team Directory" not in md
    assert "## Mention Recognition" not in md
