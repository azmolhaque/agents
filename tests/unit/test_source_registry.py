"""The source registry, which is where the passive-only boundary is declared.

These are mostly tests that bad config *fails loudly*. A source that is misconfigured
must refuse to run rather than run unclassified, because the legality class is what the
egress layer checks before making a request.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cindraleads.config import settings
from cindraleads.errors import ConfigError
from cindraleads.sources import SourceRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def minimal(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "sources": [{"id": "s1", "legality_class": "public_record"}],
    }
    data.update(overrides)
    return data


# ------------------------------------------------------- the shipped config


@pytest.fixture
def shipped() -> SourceRegistry:
    cfg = settings()
    object.__setattr__(cfg, "config_dir", CONFIG_DIR)
    return SourceRegistry.from_config(cfg)


def test_shipped_config_loads(shipped: SourceRegistry):
    assert shipped.sources
    assert all(s.legality_class for s in shipped.sources.values())


def test_every_shipped_source_has_a_valid_legality_class(shipped: SourceRegistry):
    valid = {"public_record", "public_web", "licensed_api", "first_party"}
    for source in shipped.sources.values():
        assert source.legality_class in valid, source.id


def test_linkedin_upwork_fiverr_are_not_direct_sources(shipped: SourceRegistry):
    """They are reachable only as SerpAPI results. A direct source row for any of
    them would be a ToS violation waiting to be implemented."""
    for banned in ("linkedin", "upwork", "fiverr"):
        assert not any(banned in sid for sid in shipped.sources), banned
        assert not any(banned in (s.base_url or "") for s in shipped.sources.values()), banned


def test_public_web_policy_matches_the_approved_deviation(shipped: SourceRegistry):
    """PLAN.md 2.5, approved: 6 per domain per 24 h, >= 3 s apart."""
    policy = shipped.public_web
    assert policy.fetch_budget_per_domain_24h == 6
    assert policy.min_interval_seconds >= 3.0
    assert policy.respect_robots is True
    assert policy.obey_crawl_delay is True


def test_cross_origin_redirects_are_not_followed(shipped: SourceRegistry):
    """A redirect off the origin we intended to visit escapes the per-domain budget
    and the robots decision we already made."""
    assert shipped.public_web.follow_cross_origin_redirects is False


def test_inbound_is_first_party_and_off_until_phase_6(shipped: SourceRegistry):
    inbound = shipped.get("inbound_web3forms")
    assert inbound.legality_class == "first_party"
    assert inbound.enabled is False


def test_serpapi_sources_declare_their_key_and_cost(shipped: SourceRegistry):
    for source in shipped.by_class("licensed_api"):
        if source.id.startswith("serpapi"):
            assert source.auth_env == "SERPAPI_KEY"
            assert source.cost_units >= 1, "SerpAPI calls are rationed and must cost"


def test_free_sources_cost_nothing(shipped: SourceRegistry):
    assert shipped.get("crtsh").cost_units == 0
    assert shipped.get("company_site").cost_units == 0


def test_volatile_sources_have_shorter_cache_ttls(shipped: SourceRegistry):
    """News and marketplace briefs go stale fast; a company homepage does not."""
    assert shipped.get("serpapi_news").cache_ttl_hours < shipped.get("company_site").cache_ttl_hours


def test_budget_reads_quota_at_startup_rather_than_hardcoding(shipped: SourceRegistry):
    serp = shipped.budget["serpapi"]
    assert serp["discover_quota_at_startup"] is True
    assert 0 < serp["safety_fraction"] <= 0.9
    # A daily counter is spent by mid-morning because harvest runs hourly.
    assert serp["refill_window_hours"] == 24


# ------------------------------------------------------------ failure modes


def test_missing_legality_class_is_fatal():
    with pytest.raises(ConfigError, match="legality_class"):
        SourceRegistry.from_dict({"sources": [{"id": "x"}]})


def test_invented_legality_class_is_fatal():
    """The whole point of the class is that it is checked. 'scrape_anyway' must not
    be expressible."""
    with pytest.raises(ConfigError, match="legality_class"):
        SourceRegistry.from_dict({"sources": [{"id": "x", "legality_class": "scrape_anyway"}]})


def test_missing_id_is_fatal():
    with pytest.raises(ConfigError, match="missing an 'id'"):
        SourceRegistry.from_dict({"sources": [{"legality_class": "public_web"}]})


def test_duplicate_id_is_fatal():
    with pytest.raises(ConfigError, match="duplicate source id"):
        SourceRegistry.from_dict(
            {
                "sources": [
                    {"id": "dup", "legality_class": "public_web"},
                    {"id": "dup", "legality_class": "public_record"},
                ]
            }
        )


def test_empty_source_list_is_fatal():
    with pytest.raises(ConfigError, match="non-empty"):
        SourceRegistry.from_dict({"sources": []})


def test_unknown_source_lookup_lists_what_is_available():
    registry = SourceRegistry.from_dict(minimal())
    with pytest.raises(ConfigError, match="unknown source"):
        registry.get("nope")


def test_disabled_source_raises_at_fetch_time():
    """Disabling a source is a decision. Silently returning None invites a caller to
    route around it."""
    registry = SourceRegistry.from_dict(
        {"sources": [{"id": "off", "legality_class": "public_web", "enabled": False}]}
    )
    assert registry.get("off").enabled is False
    with pytest.raises(ConfigError, match="disabled"):
        registry.require_enabled("off")


def test_unknown_yaml_keys_are_ignored_not_fatal():
    """A field documented in YAML before the code supports it must not stop boot."""
    registry = SourceRegistry.from_dict(
        {
            "sources": [{"id": "s", "legality_class": "public_web", "future_field": 1}],
            "defaults": {"user_agent": "x", "not_a_field_yet": True},
        }
    )
    assert registry.defaults.user_agent == "x"


def test_defaults_carry_the_circuit_breaker_settings():
    registry = SourceRegistry.from_dict(minimal())
    assert registry.defaults.circuit_failure_threshold == 3
    assert registry.defaults.circuit_open_seconds == 900.0


# ------------------------------------------- discovery / enrichment split


def test_sources_declare_a_role(shipped: SourceRegistry):
    """PLAN.md decision 7. Discovery finds companies we do not know; enrichment
    deepens ones we do. The split is what makes a 250-search month workable."""
    for source in shipped.sources.values():
        assert source.role in ("discovery", "enrichment"), source.id


def test_an_invented_role_is_fatal():
    with pytest.raises(ConfigError, match="role"):
        SourceRegistry.from_dict(
            {"sources": [{"id": "x", "legality_class": "public_web", "role": "vibes"}]}
        )


def test_the_hiring_triggers_are_reachable_without_spending_a_credit(shipped: SourceRegistry):
    """T3/T4/T5/T11 were assigned to SerpAPI in the master prompt. A known company's
    ATS board is public JSON, so they cost nothing once the company is known."""
    free_ids = {s.id for s in shipped.free_sources()}
    assert {"greenhouse_boards", "lever_postings", "ashby_postings"} <= free_ids


def test_most_enabled_sources_cost_nothing(shipped: SourceRegistry):
    """If the pipeline depended on paid search for routine work, 250/month would run
    out in a day. The free set has to dominate."""
    free = shipped.free_sources()
    costed = [s for s in shipped.enabled_sources() if s.cost_units]
    assert len(free) > len(costed) * 2
    # Everything that costs a credit is SerpAPI, and it is discovery-only.
    for source in costed:
        assert source.id.startswith("serpapi")
        assert source.role == "discovery"


def test_serpapi_is_capped_daily_not_monthly(shipped: SourceRegistry):
    """Spending against the monthly quota directly would let one morning burn the
    lot. 8/day at 85% is ~204/month with headroom."""
    serp = shipped.budget["serpapi"]
    assert serp["daily_cap"] == 8
    assert serp["refill_window_hours"] == 24
    assert serp["daily_cap"] * 30 * serp["safety_fraction"] < 250
