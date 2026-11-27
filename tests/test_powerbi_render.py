from datetime import datetime, timedelta, timezone

import pytest

from app.powerbi_render import EmbedConfig, RenderError, is_fresh, resolve_page_name

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def test_is_fresh_reuses_a_token_only_until_the_refresh_margin():
    """A token with more than five minutes left is reused; with five or fewer, or expired, it is minted again."""
    def config_expiring_in(minutes: int) -> EmbedConfig:
        return EmbedConfig(report_id="r", embed_url="u", embed_token="t", expires_at=NOW + timedelta(minutes=minutes))

    assert is_fresh(config_expiring_in(30), NOW)
    assert not is_fresh(config_expiring_in(5), NOW) and not is_fresh(config_expiring_in(-1), NOW)


def test_resolve_page_name_gives_the_page_id_and_lists_the_real_names_for_an_unknown_one():
    """A display name gives its page id, and an unknown name lists the real pages."""
    pages = [{"name": "p1", "displayName": "Model Evaluation"}, {"name": "p2", "displayName": "Financial Impact"}]
    assert resolve_page_name("Financial Impact", pages) == "p2"
    with pytest.raises(RenderError, match="Financial Impact"):
        resolve_page_name("Cost", pages)
