"""Checks that the app follows the Semente 0.4 manifest and domain contracts.

The migration from 0.3 to 0.4 renamed features, moved the terms text out of
``agents.yml`` into a standalone file and made input guardrails opt-in. These
tests keep the app from silently falling back to the legacy aliases.
"""

from pathlib import Path

from semente.manifest import Manifest

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_uses_the_current_feature_names():
    manifest = Manifest.load(ROOT / "semente.yaml")

    assert set(manifest.features) == {
        "tts",
        "interaction-assessment",
        "summarization",
        "guardrails",
        "terms-acceptance",
    }
    assert not {"feedback_workflow", "pii_guardrail"} & set(manifest.features)


def test_the_terms_gate_points_at_an_existing_file():
    manifest = Manifest.load(ROOT / "semente.yaml")
    terms = manifest.features["terms-acceptance"]

    assert terms.enable is True
    assert (manifest.source_dir / terms.file).is_file()
    assert "Terms of Use" in (ROOT / "domain" / "terms.md").read_text(encoding="utf-8")


def test_the_welcoming_agent_has_both_onboarding_stages():
    from semente.configs.prompts import get_agent_config

    config = get_agent_config("welcoming_agent")

    assert "instructions_persona" in config
    assert "persona_not_informed" in config
    assert "terms_text" not in config


def test_the_domain_declares_its_guardrail_chain():
    from domain import domain_spec

    assert [guard.name for guard in domain_spec.guardrails] == ["email", "card"]
