"""Recipe photos, fetched from Mealie on the panel's behalf."""

from __future__ import annotations

from http import HTTPStatus

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.home_signals.const import DOMAIN

RID = "fa4a64da-a534-47b0-b86a-fe72973244fd"
BASE = "http://mealie.local:9000"


async def _start(hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain="mealie", data={"host": BASE, "api_token": "secret-token"},
    ).add_to_hass(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_photo_is_passed_through(
    hass: HomeAssistant, hass_client, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    aioclient_mock.get(
        f"{BASE}/api/media/recipes/{RID}/images/min-original.webp",
        content=b"RIFF....WEBP", headers={"Content-Type": "image/webp"},
    )
    client = await hass_client()
    resp = await client.get(f"/api/{DOMAIN}/recipe_image/{RID}/min")
    assert resp.status == HTTPStatus.OK
    assert await resp.read() == b"RIFF....WEBP"
    assert resp.headers["Content-Type"] == "image/webp"
    assert "max-age=86400" in resp.headers["Cache-Control"]


async def test_no_photo_and_odd_requests_are_not_found(
    hass: HomeAssistant, hass_client, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    aioclient_mock.get(
        f"{BASE}/api/media/recipes/{RID}/images/tiny-original.webp", status=404,
    )
    client = await hass_client()
    assert (await client.get(f"/api/{DOMAIN}/recipe_image/{RID}/tiny")).status == 404
    assert (await client.get(f"/api/{DOMAIN}/recipe_image/{RID}/huge")).status == 404
    # Home Assistant's own security filter refuses this before the view sees it.
    assert (await client.get(f"/api/{DOMAIN}/recipe_image/..%2Fetc/min")).status in (400, 404)
    assert (await client.get(f"/api/{DOMAIN}/recipe_image/not-an-id!/min")).status == 404


async def test_it_needs_home_assistants_auth(
    hass: HomeAssistant, hass_client_no_auth
) -> None:
    await _start(hass)
    client = await hass_client_no_auth()
    assert (await client.get(f"/api/{DOMAIN}/recipe_image/{RID}/min")).status == 401
