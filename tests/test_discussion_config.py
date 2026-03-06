"""Tests for discussion-related config models (IdentityConfig, DiscussionConfig)."""

import json

from nanobot.config.loader import load_config
from nanobot.config.schema import (
    DISCUSSION_REPLY_DELAY_DEFAULTS,
    Config,
    DiscussionConfig,
    DiscussionPrinciplesConfig,
    IdentityConfig,
    OrgConfig,
    PersonaConfig,
    ReplyDelayConfig,
)

# ---------------------------------------------------------------------------
# PersonaConfig
# ---------------------------------------------------------------------------


def test_persona_config_defaults():
    p = PersonaConfig()
    assert p.style == ""
    assert p.goals == []
    assert p.dont == []


def test_persona_config_with_values():
    p = PersonaConfig(style="analytical", goals=["research"], dont=["guess"])
    assert p.style == "analytical"
    assert p.goals == ["research"]
    assert p.dont == ["guess"]


# ---------------------------------------------------------------------------
# OrgConfig
# ---------------------------------------------------------------------------


def test_org_config_defaults():
    o = OrgConfig()
    assert o.manager is None
    assert o.subordinates == []
    assert o.peers == []


def test_org_config_with_values():
    o = OrgConfig(manager="manager", subordinates=["ic_1"], peers=["ic_2"])
    assert o.manager == "manager"
    assert o.subordinates == ["ic_1"]
    assert o.peers == ["ic_2"]


# ---------------------------------------------------------------------------
# IdentityConfig
# ---------------------------------------------------------------------------


def test_identity_config_defaults():
    i = IdentityConfig()
    assert i.role_id == ""
    assert i.display_name == ""
    assert isinstance(i.persona, PersonaConfig)
    assert isinstance(i.org, OrgConfig)


def test_identity_config_manager():
    i = IdentityConfig(
        role_id="manager",
        display_name="Manager",
        persona=PersonaConfig(
            style="structured",
            goals=["track progress", "keep discussion active"],
            dont=["do work for subordinates"],
        ),
        org=OrgConfig(manager=None, subordinates=["ic_1", "ic_2"]),
    )
    assert i.role_id == "manager"
    assert i.display_name == "Manager"
    assert i.org.subordinates == ["ic_1", "ic_2"]
    assert i.org.manager is None


def test_identity_config_ic():
    i = IdentityConfig(
        role_id="ic_1",
        display_name="IC-1",
        persona=PersonaConfig(style="research-oriented", goals=["analyze"]),
        org=OrgConfig(manager="manager"),
    )
    assert i.org.manager == "manager"
    assert i.org.subordinates == []


# ---------------------------------------------------------------------------
# IdentityConfig JSON serialization (camelCase aliases)
# ---------------------------------------------------------------------------


def test_identity_config_camel_case_roundtrip():
    data = {
        "roleId": "ic_2",
        "displayName": "IC-2",
        "persona": {"style": "implementation", "goals": ["build"], "dont": []},
        "org": {"manager": "manager", "subordinates": [], "peers": ["ic_1"]},
    }
    i = IdentityConfig.model_validate(data)
    assert i.role_id == "ic_2"
    assert i.display_name == "IC-2"
    assert i.org.peers == ["ic_1"]

    dumped = i.model_dump(by_alias=True)
    assert dumped["roleId"] == "ic_2"
    assert dumped["displayName"] == "IC-2"


# ---------------------------------------------------------------------------
# DiscussionConfig
# ---------------------------------------------------------------------------


def test_discussion_config_defaults():
    d = DiscussionConfig()
    assert d.enabled is False
    assert d.idle_warn_after_sec == 300
    assert d.idle_nudge_after_sec == 900
    assert d.idle_new_topic_after_sec == 1800
    assert d.max_consecutive_manager_nudges == 2


def test_discussion_config_custom_values():
    d = DiscussionConfig(
        enabled=True,
        idle_warn_after_sec=60,
        idle_nudge_after_sec=120,
        idle_new_topic_after_sec=240,
        max_consecutive_manager_nudges=3,
    )
    assert d.enabled is True
    assert d.idle_warn_after_sec == 60


def test_discussion_config_camel_case():
    data = {
        "enabled": True,
        "idleWarnAfterSec": 100,
        "idleNudgeAfterSec": 200,
        "idleNewTopicAfterSec": 400,
        "maxConsecutiveManagerNudges": 5,
    }
    d = DiscussionConfig.model_validate(data)
    assert d.idle_warn_after_sec == 100
    assert d.max_consecutive_manager_nudges == 5


# ---------------------------------------------------------------------------
# AgentDefaults.identity integration
# ---------------------------------------------------------------------------


def test_agent_defaults_identity_none_by_default():
    cfg = Config()
    assert cfg.agents.defaults.identity is None


def test_agent_defaults_identity_from_json(tmp_path):
    data = {
        "agents": {
            "defaults": {
                "identity": {
                    "roleId": "manager",
                    "displayName": "Manager",
                    "persona": {"style": "push", "goals": ["drive"], "dont": []},
                    "org": {"manager": None, "subordinates": ["ic_1"]},
                }
            }
        }
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(data), encoding="utf-8")

    cfg = load_config(config_path=config_file)
    assert cfg.agents.defaults.identity is not None
    assert cfg.agents.defaults.identity.role_id == "manager"
    assert cfg.agents.defaults.identity.org.subordinates == ["ic_1"]


# ---------------------------------------------------------------------------
# Root Config.discussion integration
# ---------------------------------------------------------------------------


def test_config_discussion_defaults():
    cfg = Config()
    assert cfg.discussion.enabled is False


def test_config_discussion_from_json(tmp_path):
    data = {
        "discussion": {
            "enabled": True,
            "idleWarnAfterSec": 60,
        }
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(data), encoding="utf-8")

    cfg = load_config(config_path=config_file)
    assert cfg.discussion.enabled is True
    assert cfg.discussion.idle_warn_after_sec == 60
    # Non-overridden fields keep defaults
    assert cfg.discussion.idle_nudge_after_sec == 900


# ---------------------------------------------------------------------------
# DiscussionPrinciplesConfig
# ---------------------------------------------------------------------------


def test_discussion_principles_defaults():
    dp = DiscussionPrinciplesConfig()
    assert dp.research_before_answer is True
    assert dp.topic_selection is False
    assert dp.extra_rules == []


def test_discussion_principles_custom_values():
    dp = DiscussionPrinciplesConfig(
        research_before_answer=False, topic_selection=True, extra_rules=["be concise"]
    )
    assert dp.research_before_answer is False
    assert dp.topic_selection is True
    assert dp.extra_rules == ["be concise"]


def test_discussion_principles_camel_case():
    data = {
        "researchBeforeAnswer": False,
        "topicSelection": True,
        "extraRules": ["rule1", "rule2"],
    }
    dp = DiscussionPrinciplesConfig.model_validate(data)
    assert dp.research_before_answer is False
    assert dp.topic_selection is True
    assert dp.extra_rules == ["rule1", "rule2"]


def test_identity_config_has_discussion_principles():
    i = IdentityConfig()
    assert isinstance(i.discussion_principles, DiscussionPrinciplesConfig)
    assert i.discussion_principles.research_before_answer is True
    assert i.discussion_principles.topic_selection is False


def test_identity_config_discussion_principles_from_json():
    data = {
        "roleId": "manager",
        "displayName": "Manager",
        "discussionPrinciples": {"topicSelection": True},
    }
    i = IdentityConfig.model_validate(data)
    assert i.discussion_principles.topic_selection is True
    assert i.discussion_principles.research_before_answer is True  # default


def test_full_config_with_discussion_principles(tmp_path):
    data = {
        "agents": {
            "defaults": {
                "identity": {
                    "roleId": "manager",
                    "displayName": "Manager",
                    "discussionPrinciples": {
                        "topicSelection": True,
                        "extraRules": ["always cite sources"],
                    },
                }
            }
        }
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(data), encoding="utf-8")

    cfg = load_config(config_path=config_file)
    dp = cfg.agents.defaults.identity.discussion_principles
    assert dp.topic_selection is True
    assert dp.research_before_answer is True
    assert dp.extra_rules == ["always cite sources"]


def test_full_team_config_roundtrip(tmp_path):
    """Simulate a full manager config with identity + discussion."""
    data = {
        "agents": {
            "defaults": {
                "workspace": str(tmp_path / "workspace"),
                "identity": {
                    "roleId": "manager",
                    "displayName": "Manager",
                    "persona": {
                        "style": "structured",
                        "goals": ["drive discussion", "track progress"],
                        "dont": ["do subordinates' work"],
                    },
                    "org": {"subordinates": ["ic_1", "ic_2"]},
                },
            }
        },
        "channels": {"teams": {"enabled": True, "groupPolicy": "open"}},
        "discussion": {
            "enabled": True,
            "idleWarnAfterSec": 300,
            "idleNudgeAfterSec": 900,
            "idleNewTopicAfterSec": 1800,
            "maxConsecutiveManagerNudges": 2,
        },
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(data), encoding="utf-8")

    cfg = load_config(config_path=config_file)
    assert cfg.agents.defaults.identity.role_id == "manager"
    assert cfg.channels.teams.group_policy == "open"
    assert cfg.discussion.enabled is True


# ---------------------------------------------------------------------------
# ReplyDelayConfig
# ---------------------------------------------------------------------------


def test_reply_delay_defaults():
    rd = ReplyDelayConfig()
    assert rd.enabled is False
    assert rd.base_delay_sec == 5.0
    assert rd.jitter_sec == 10.0
    assert rd.per_100_chars_sec == 1.0
    assert rd.max_delay_sec == 30.0


def test_reply_delay_custom_values():
    rd = ReplyDelayConfig(
        enabled=True,
        base_delay_sec=3.0,
        jitter_sec=5.0,
        per_100_chars_sec=0.5,
        max_delay_sec=20.0,
    )
    assert rd.enabled is True
    assert rd.base_delay_sec == 3.0
    assert rd.jitter_sec == 5.0
    assert rd.per_100_chars_sec == 0.5
    assert rd.max_delay_sec == 20.0


def test_reply_delay_camel_case():
    data = {
        "enabled": True,
        "baseDelaySec": 2.0,
        "jitterSec": 8.0,
        "per100CharsSec": 0.8,
        "maxDelaySec": 25.0,
    }
    rd = ReplyDelayConfig.model_validate(data)
    assert rd.enabled is True
    assert rd.base_delay_sec == 2.0
    assert rd.jitter_sec == 8.0
    assert rd.per_100_chars_sec == 0.8
    assert rd.max_delay_sec == 25.0


def test_agent_defaults_reply_delay():
    cfg = Config()
    rd = cfg.agents.defaults.reply_delay
    assert isinstance(rd, ReplyDelayConfig)
    assert rd.enabled is False


def test_agent_defaults_reply_delay_from_json(tmp_path):
    data = {
        "agents": {
            "defaults": {
                "replyDelay": {
                    "enabled": True,
                    "baseDelaySec": 3.0,
                    "jitterSec": 5.0,
                }
            }
        }
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(data), encoding="utf-8")

    cfg = load_config(config_path=config_file)
    rd = cfg.agents.defaults.reply_delay
    assert rd.enabled is True
    assert rd.base_delay_sec == 3.0
    assert rd.jitter_sec == 5.0
    assert rd.per_100_chars_sec == 1.0  # default preserved
    assert rd.max_delay_sec == 30.0  # default preserved


# ---------------------------------------------------------------------------
# IdentityConfig.team_members
# ---------------------------------------------------------------------------


def test_identity_config_team_members_default():
    i = IdentityConfig()
    assert i.team_members == {}


def test_identity_config_team_members_set():
    i = IdentityConfig(
        role_id="manager",
        display_name="Manager",
        team_members={"manager": "Manager", "ic_1": "IC-1", "ic_2": "IC-2"},
    )
    assert i.team_members == {"manager": "Manager", "ic_1": "IC-1", "ic_2": "IC-2"}


def test_identity_config_team_members_camel_case():
    data = {
        "roleId": "ic_1",
        "displayName": "IC-1",
        "teamMembers": {"manager": "Manager", "ic_1": "IC-1"},
    }
    i = IdentityConfig.model_validate(data)
    assert i.team_members == {"manager": "Manager", "ic_1": "IC-1"}


def test_identity_config_team_members_roundtrip():
    i = IdentityConfig(
        role_id="ic_1",
        display_name="IC-1",
        team_members={"manager": "Manager", "ic_1": "IC-1"},
    )
    dumped = i.model_dump(by_alias=True)
    assert dumped["teamMembers"] == {"manager": "Manager", "ic_1": "IC-1"}


def test_identity_config_team_members_from_full_json(tmp_path):
    data = {
        "agents": {
            "defaults": {
                "identity": {
                    "roleId": "manager",
                    "displayName": "Manager",
                    "teamMembers": {"manager": "Manager", "ic_1": "IC-1"},
                }
            }
        }
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(data), encoding="utf-8")

    cfg = load_config(config_path=config_file)
    assert cfg.agents.defaults.identity.team_members == {"manager": "Manager", "ic_1": "IC-1"}


# ---------------------------------------------------------------------------
# DISCUSSION_REPLY_DELAY_DEFAULTS constant
# ---------------------------------------------------------------------------


def test_discussion_reply_delay_defaults_values():
    d = DISCUSSION_REPLY_DELAY_DEFAULTS
    assert d.enabled is True
    assert d.base_delay_sec == 15.0
    assert d.jitter_sec == 30.0
    assert d.per_100_chars_sec == 2.0
    assert d.max_delay_sec == 120.0


# ---------------------------------------------------------------------------
# DiscussionConfig.reply_delay_override
# ---------------------------------------------------------------------------


def test_discussion_config_reply_delay_override_default():
    d = DiscussionConfig()
    assert d.reply_delay_override is None


def test_discussion_config_reply_delay_override_set():
    override = ReplyDelayConfig(enabled=True, base_delay_sec=10.0)
    d = DiscussionConfig(reply_delay_override=override)
    assert d.reply_delay_override is not None
    assert d.reply_delay_override.base_delay_sec == 10.0


def test_discussion_config_reply_delay_override_camel_case():
    data = {
        "enabled": True,
        "replyDelayOverride": {
            "enabled": True,
            "baseDelaySec": 20.0,
            "jitterSec": 15.0,
        },
    }
    d = DiscussionConfig.model_validate(data)
    assert d.reply_delay_override is not None
    assert d.reply_delay_override.base_delay_sec == 20.0
    assert d.reply_delay_override.jitter_sec == 15.0


def test_discussion_config_reply_delay_override_from_json(tmp_path):
    data = {
        "discussion": {
            "enabled": True,
            "replyDelayOverride": {
                "enabled": True,
                "baseDelaySec": 25.0,
                "maxDelaySec": 90.0,
            },
        }
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(data), encoding="utf-8")

    cfg = load_config(config_path=config_file)
    override = cfg.discussion.reply_delay_override
    assert override is not None
    assert override.base_delay_sec == 25.0
    assert override.max_delay_sec == 90.0
