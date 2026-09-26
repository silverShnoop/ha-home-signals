"""Recipe photos from Mealie, for the meal cards.

Mealie keeps a photo for most imported recipes, but only Mealie serves
them, and the kitchen panel cannot reach Mealie: the app runs behind Home
Assistant's ingress, which a card's <img> cannot use. So this view fetches
the photo from Mealie on the panel's behalf, at the address the Mealie
integration already knows.

It needs Home Assistant's authentication like any other API path. An
<img> cannot send a bearer token, so the card asks Home Assistant to sign
the path first (auth/sign_path) and uses the signed URL. Photos are cached
by the browser for a day, because a recipe's photo does not change once it
is imported and the panel repaints often.
"""

from __future__ import annotations

from http import HTTPStatus
import re

from aiohttp import ClientError, ClientTimeout, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, MEALIE_DOMAIN

SIZES = {"tiny": "tiny-original.webp", "min": "min-original.webp", "original": "original.webp"}
_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_TIMEOUT = ClientTimeout(total=15)


class RecipeImageView(HomeAssistantView):
    """GET /api/home_signals/recipe_image/<recipe_id>/<size>."""

    url = f"/api/{DOMAIN}/recipe_image/{{recipe_id}}/{{size}}"
    name = f"api:{DOMAIN}:recipe_image"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, recipe_id: str, size: str) -> web.Response:
        if not _ID.match(recipe_id) or size not in SIZES:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        entries = [
            e for e in self.hass.config_entries.async_entries(MEALIE_DOMAIN)
            if e.data.get("host")
        ]
        if not entries:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        entry = entries[0]
        host = str(entry.data["host"]).rstrip("/")
        session = async_get_clientsession(
            self.hass, verify_ssl=bool(entry.data.get("verify_ssl", True))
        )
        url = f"{host}/api/media/recipes/{recipe_id}/images/{SIZES[size]}"
        headers = {}
        if entry.data.get("api_token"):
            headers["Authorization"] = f"Bearer {entry.data['api_token']}"
        try:
            async with session.get(url, headers=headers, timeout=_TIMEOUT) as resp:
                if resp.status != HTTPStatus.OK:
                    return web.Response(status=HTTPStatus.NOT_FOUND)
                body = await resp.read()
                kind = resp.headers.get("Content-Type", "image/webp")
        except (ClientError, TimeoutError, HomeAssistantError):
            return web.Response(status=HTTPStatus.BAD_GATEWAY)
        if not kind.startswith("image/"):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return web.Response(
            body=body, content_type=kind.split(";")[0],
            headers={"Cache-Control": "private, max-age=86400"},
        )


def async_register_image_view(hass: HomeAssistant) -> None:
    """Register once; a reload of the entry must not register twice."""
    key = f"{DOMAIN}_image_view"
    if hass.data.get(key):
        return
    hass.http.register_view(RecipeImageView(hass))
    hass.data[key] = True
