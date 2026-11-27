"""Renders a Power BI report page to an image. Shared by the coding agent app and the gateway: each passes its own service
principal's Power BI access token, so which reports a caller can reach is credentials and config, never an argument."""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import requests
from langchain_core.tools import ToolException
from playwright.async_api import Error as PlaywrightError, async_playwright

from app.config import POWER_BI_RENDER_TARGETS

logger = logging.getLogger(__name__)

POWER_BI_API = "https://api.powerbi.com/v1.0/myorg"
EMBED_TOKEN_REFRESH_MARGIN = timedelta(minutes=5)

BROWSER_CHANNEL = "chromium"
VIEWPORT = {"width": 1500, "height": 760}
RENDER_TIMEOUT_MS = 60_000
SETTLE_SECONDS = 1
BROWSER_TIMEOUT_SECONDS = 90

POWER_BI_CLIENT_JS = (Path(__file__).parent / "powerbi_client_js" / "powerbi.min.js").read_text(encoding="utf-8")
EMBED_PAGE = ("<!doctype html><html><head><style>html, body, #report {margin: 0; width: 100%; height: 100%}</style></head>"
              f"<body><div id='report'></div><script>{POWER_BI_CLIENT_JS}</script></body></html>")

# Embeds the report opened on pageName with the bookmark applied when one is given. Slicers are set as soon as the report has loaded,
# before its first render finishes. Resolves once that render is done, or with the slicer titles on the page when one asked for is missing.
EMBED_JS = """async ({embedUrl, token, reportId, pageName, bookmark, selections, timeoutMs}) => {
  const models = window['powerbi-client'].models;
  const report = window.powerbi.embed(document.getElementById('report'), {
    type: 'report', id: reportId, embedUrl, accessToken: token, tokenType: models.TokenType.Embed, pageName,
    bookmark: bookmark ? {name: bookmark} : undefined,
    settings: {panes: {filters: {visible: false}, pageNavigation: {visible: false}}},
  });
  window.report = report;
  window.waitFor = (event) => new Promise((resolve, reject) => {
    const finish = (settle, value) => { clearTimeout(timer); report.off(event, onEvent); report.off('error', onError); settle(value); };
    const timer = setTimeout(() => finish(reject, new Error(`The report did not ${event} within ${timeoutMs / 1000} seconds.`)), timeoutMs);
    const onEvent = () => finish(resolve);
    const onError = (error) => finish(reject, new Error(JSON.stringify(error.detail)));
    report.on(event, onEvent);
    report.on('error', onError);
  });
  const hasSelections = Object.keys(selections).length > 0;
  let rendered = hasSelections ? null : window.waitFor('rendered');
  if (hasSelections) {
    await window.waitFor('loaded');
    rendered = window.waitFor('rendered');
    const slicers = (await (await report.getActivePage()).getVisuals()).filter(visual => visual.type === 'slicer');
    const unknown = Object.keys(selections).filter(title => !slicers.some(slicer => slicer.title === title));
    if (unknown.length) return {unknown, titles: slicers.map(slicer => slicer.title)};
    for (const slicer of slicers.filter(slicer => slicer.title in selections)) {
      const state = await slicer.getSlicerState();
      await slicer.setSlicerState({...state, filters: [{
        $schema: 'http://powerbi.com/product/schema#basic', target: state.targets[0],
        filterType: models.FilterType.Basic, operator: 'In', values: selections[slicer.title]}]});
    }
  }
  await rendered;
  return null;
}"""

RenderTarget = Literal[tuple(POWER_BI_RENDER_TARGETS)]


class RenderError(ToolException):
    """A render failed for a reason the caller can act on; the message says what to do."""


@dataclass
class EmbedConfig:
    report_id: str
    embed_url: str
    embed_token: str
    expires_at: datetime


_embed_configs: dict[str, EmbedConfig] = {}


def is_fresh(config: EmbedConfig, now: datetime) -> bool:
    """True while the embed token has more than the refresh margin left."""
    return config.expires_at - now > EMBED_TOKEN_REFRESH_MARGIN


def _power_bi_call(method: str, path: str, access_token: str, **kwargs) -> dict:
    response = requests.request(method, f"{POWER_BI_API}{path}", headers={"Authorization": f"Bearer {access_token}"}, timeout=30, **kwargs)
    if response.status_code in (401, 403):
        raise RenderError(f"Power BI refused the request (HTTP {response.status_code}): the identity or its embed allowance is the "
                          "problem, not the request. Report that you could not render; do not retry.")
    if not response.ok:
        raise RenderError(f"Power BI request {method} {path} failed (HTTP {response.status_code}): {response.text}")
    return response.json()


async def get_embed_config(target: RenderTarget, access_token: str) -> EmbedConfig:
    """The embed URL and an embed token for the target's report, reused until shortly before the token expires."""
    cached = _embed_configs.get(target)
    if cached and is_fresh(cached, datetime.now(timezone.utc)):
        return cached

    ids = POWER_BI_RENDER_TARGETS[target]

    def fetch() -> EmbedConfig:
        report = _power_bi_call("GET", f"/groups/{ids['workspace_id']}/reports/{ids['report_id']}", access_token)
        token = _power_bi_call("POST", "/GenerateToken", access_token, json={
            "datasets": [{"id": ids["dataset_id"]}], "reports": [{"id": ids["report_id"]}],
            "targetWorkspaces": [{"id": ids["workspace_id"]}]})
        return EmbedConfig(report_id=ids["report_id"], embed_url=report["embedUrl"], embed_token=token["token"],
                           expires_at=datetime.fromisoformat(token["expiration"]))

    _embed_configs[target] = await asyncio.to_thread(fetch)
    return _embed_configs[target]


def resolve_page_name(requested: str, pages: list[dict]) -> str:
    """The id of the page with this display name."""
    for page in pages:
        if page["displayName"] == requested:
            return page["name"]
    raise RenderError(f"Page '{requested}' not found in the report. Its pages are: {[page['displayName'] for page in pages]}.")


async def render_page(target: RenderTarget, access_token: str, page: str, bookmark: str | None = None,
                      slicers: dict[str, list] | None = None, scale: float = 1.0) -> bytes:
    """A PNG of the page of the target's report with this display name, with the bookmark with this id applied when one is given and
    each slicer, by its title, set to the values listed for it."""
    try:
        config = await get_embed_config(target, access_token)

        # The pages are read on every render, not cached: a page just published must be found
        ids = POWER_BI_RENDER_TARGETS[target]
        pages = await asyncio.to_thread(_power_bi_call, "GET", f"/groups/{ids['workspace_id']}/reports/{ids['report_id']}/pages", access_token)
        page_id = resolve_page_name(page, pages["value"])

        async with asyncio.timeout(BROWSER_TIMEOUT_SECONDS), async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
            browser_page = await browser.new_page(viewport=VIEWPORT, device_scale_factor=scale)
            await browser_page.set_content(EMBED_PAGE)
            try:
                missing = await browser_page.evaluate(EMBED_JS, {
                    "embedUrl": config.embed_url, "token": config.embed_token, "reportId": config.report_id, "pageName": page_id,
                    "bookmark": bookmark, "selections": slicers or {}, "timeoutMs": RENDER_TIMEOUT_MS})
            except PlaywrightError as error:
                if "Missing_References" in str(error):
                    raise RenderError("The report could not render a visual because a measure or column it uses is missing. If you added one, "
                                      f"publish first; otherwise check its spelling in the TMDL. Details: {error}") from error
                hint = " A bookmark id is the file name in the report's bookmarks folder." if bookmark else ""
                raise RenderError(f"The report did not render: {error}.{hint}") from error
            if missing:
                raise RenderError(f"No slicer titled {missing['unknown']} on that page. Its slicers are: {missing['titles']}.")
            await browser_page.wait_for_timeout(SETTLE_SECONDS * 1000)
            return await browser_page.screenshot(type="png")
    except RenderError:
        raise
    except Exception as error:
        logger.exception("render failed")
        raise RenderError(f"Could not render ({type(error).__name__}: {error}). This is a problem with Power BI, the browser or the "
                          "network, not your request. Retry once, or carry on without a render.") from error
