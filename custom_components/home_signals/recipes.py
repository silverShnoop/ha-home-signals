"""Writing recipes to Mealie, which Home Assistant's own integration cannot.

The core Mealie integration reads recipes and imports one from a link, and
that is all. A family recipe with no web page, a quantity that needs fixing
after an import, a recipe nobody wants any more -- each of those meant
opening Mealie's own interface, and the point of the meal card is that
nobody has to.

So these two actions talk to Mealie's API directly. They borrow the address
and token from the Mealie integration's config entry rather than asking for
them again: a second copy of the token is a second place for it to go stale,
and the house already told Home Assistant where Mealie is.

An ingredient line that has not changed keeps Mealie's own parse of it --
the food, the unit, the quantity it worked out on import. Only a line that
was actually edited is replaced by plain text. Rewriting every line as text
on every save would quietly throw that away the first time anybody fixed a
typo in the method.
"""

from __future__ import annotations

import json
import re
from typing import Any
import uuid

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DESCRIPTION,
    ATTR_INGREDIENTS,
    ATTR_METHOD,
    ATTR_NAME,
    ATTR_RECIPE,
    ATTR_SERVINGS,
    ATTR_TOTAL_TIME,
    ATTR_URL,
    DOMAIN,
    MEALIE_DOMAIN,
    SERVICE_DELETE_RECIPE,
    SERVICE_IMPORT_RECIPE,
    SERVICE_SAVE_RECIPE,
)

_TIMEOUT = aiohttp.ClientTimeout(total=20)
# An import can take a minute. A page with no recipe data is read by
# Mealie's AI provider, and a video is downloaded and transcribed first.
# The core integration gives up after ten seconds, while Mealie carries on
# and saves the recipe anyway, so the person is told it failed when it
# worked. That is the whole reason this action exists.
_IMPORT_TIMEOUT = aiohttp.ClientTimeout(total=300)

# "- ", "• ", "3. " or "3) " at the start of a line.
_MARKER = re.compile(r"^\s*(?:[-*•]+|\d{1,2}[.)])\s+")


def _lines(value: Any) -> list[str] | None:
    """A list of lines from a list or from text with one per line.

    None means "not given", which is different from an empty list: a save
    that does not mention the method must leave the method alone, whereas
    one that sends an empty method means to clear it.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = value.splitlines()
    out = []
    for line in value:
        # A pasted list often carries its own bullets or numbers. Mealie
        # numbers the method itself, so "1. Heat the oil" would read "1. 1.".
        text = _MARKER.sub("", str(line)).strip()
        if text:
            out.append(text)
    return out


SAVE_SCHEMA = vol.Schema({
    vol.Optional(ATTR_RECIPE): cv.string,
    vol.Optional(ATTR_NAME): cv.string,
    vol.Optional(ATTR_DESCRIPTION): cv.string,
    vol.Optional(ATTR_TOTAL_TIME): cv.string,
    vol.Optional(ATTR_SERVINGS): vol.Coerce(float),
    vol.Optional(ATTR_INGREDIENTS): vol.Any(cv.string, [cv.string]),
    vol.Optional(ATTR_METHOD): vol.Any(cv.string, [cv.string]),
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
})

IMPORT_SCHEMA = vol.Schema({
    vol.Required(ATTR_URL): cv.string,
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
})

DELETE_SCHEMA = vol.Schema({
    vol.Required(ATTR_RECIPE): cv.string,
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
})


class _Mealie:
    """Just enough of Mealie's API to write a recipe."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        host = str(entry.data.get("host", "")).rstrip("/")
        if not host or not entry.data.get("api_token"):
            raise ServiceValidationError(
                "The Mealie integration has no address or token to borrow."
            )
        self._base = f"{host}/api"
        self._headers = {"Authorization": f"Bearer {entry.data['api_token']}"}
        self._session = async_get_clientsession(
            hass, verify_ssl=bool(entry.data.get("verify_ssl", True))
        )

    async def request(
        self, method: str, path: str, body: Any = None,
        timeout: aiohttp.ClientTimeout = _TIMEOUT,
    ) -> Any:
        try:
            async with self._session.request(
                method, f"{self._base}{path}", json=body,
                headers=self._headers, timeout=timeout,
            ) as resp:
                if resp.status == 404:
                    raise ServiceValidationError("Mealie has no such recipe.")
                if resp.status in (401, 403):
                    raise HomeAssistantError(
                        "Mealie refused the token. Re-authenticate the Mealie integration."
                    )
                if resp.status >= 400:
                    detail = (await resp.text())[:300]
                    raise HomeAssistantError(f"Mealie said {resp.status}: {detail}")
                # Parsed from the body rather than trusted to the header:
                # Mealie answers a create with a bare JSON string, and a
                # proxy in front of it may not say what it is sending.
                text = await resp.text()
                if not text:
                    return None
                try:
                    return json.loads(text)
                except ValueError:
                    return text
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HomeAssistantError(f"Could not reach Mealie: {err}") from err


def _mealie_entry(hass: HomeAssistant, entry_id: str | None) -> ConfigEntry:
    """The Mealie config entry: the one named, else the one that is loaded."""
    entries = hass.config_entries.async_entries(MEALIE_DOMAIN)
    if entry_id:
        for entry in entries:
            if entry.entry_id == entry_id:
                return entry
        raise ServiceValidationError(f"No Mealie config entry {entry_id}.")
    loaded = [e for e in entries if e.state is ConfigEntryState.LOADED]
    if loaded or entries:
        return (loaded or entries)[0]
    raise ServiceValidationError("The Mealie integration is not set up.")


def _ingredient(line: str) -> dict[str, Any]:
    """A plain-text ingredient. Quantity 0, or Mealie shows "1 2 onions"."""
    return {
        "quantity": 0,
        "unit": None,
        "food": None,
        "note": line,
        "title": "",
        "originalText": line,
        "referenceId": str(uuid.uuid4()),
    }


def _said(ingredient: dict[str, Any]) -> set[str]:
    """Every way Mealie might already be showing an ingredient line."""
    return {
        str(ingredient.get(key) or "").strip()
        for key in ("display", "note", "originalText")
    } - {""}


def _merge_ingredients(old: list[dict[str, Any]], lines: list[str]) -> list[dict[str, Any]]:
    """The new lines, keeping Mealie's parse of any line that did not change."""
    spare = list(old)
    out = []
    for line in lines:
        match = next((ing for ing in spare if line in _said(ing)), None)
        if match is not None:
            spare.remove(match)
            out.append(match)
        else:
            out.append(_ingredient(line))
    return out


def _merge_steps(old: list[dict[str, Any]], lines: list[str]) -> list[dict[str, Any]]:
    spare = list(old)
    out = []
    for line in lines:
        match = next((s for s in spare if str(s.get("text") or "").strip() == line), None)
        if match is not None:
            spare.remove(match)
            out.append(match)
        else:
            out.append({
                "id": str(uuid.uuid4()), "title": "", "summary": "",
                "text": line, "ingredientReferences": [],
            })
    return out


async def async_save_recipe(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Create a recipe, or change the one named. Returns where it now lives."""
    data = call.data
    api = _Mealie(hass, _mealie_entry(hass, data.get(ATTR_CONFIG_ENTRY_ID)))
    slug = data.get(ATTR_RECIPE)

    if not slug:
        name = str(data.get(ATTR_NAME) or "").strip()
        if not name:
            raise ServiceValidationError("A new recipe needs a name.")
        created = await api.request("POST", "/recipes", {"name": name})
        # Mealie answers a create with the new slug, as a bare JSON string.
        slug = created if isinstance(created, str) else (created or {}).get("slug")
        if not slug:
            raise HomeAssistantError("Mealie created the recipe but did not say where.")

    current = await api.request("GET", f"/recipes/{slug}") or {}
    patch: dict[str, Any] = {}
    if ATTR_NAME in data and str(data[ATTR_NAME]).strip():
        patch["name"] = str(data[ATTR_NAME]).strip()
    if ATTR_DESCRIPTION in data:
        patch["description"] = data[ATTR_DESCRIPTION]
    if ATTR_TOTAL_TIME in data:
        patch["totalTime"] = data[ATTR_TOTAL_TIME]
    if ATTR_SERVINGS in data:
        patch["recipeServings"] = data[ATTR_SERVINGS]
    ingredients = _lines(data.get(ATTR_INGREDIENTS))
    if ingredients is not None:
        patch["recipeIngredient"] = _merge_ingredients(
            current.get("recipeIngredient") or [], ingredients
        )
    steps = _lines(data.get(ATTR_METHOD))
    if steps is not None:
        patch["recipeInstructions"] = _merge_steps(
            current.get("recipeInstructions") or [], steps
        )

    saved = current
    if patch:
        answer = await api.request("PATCH", f"/recipes/{slug}", patch)
        saved = answer if isinstance(answer, dict) else current
    # A rename moves the recipe to a new slug, so the answer is read from
    # what Mealie sent back, not from what was asked for.
    return {
        "slug": saved.get("slug", slug),
        "recipe_id": saved.get("id", current.get("id")),
        "name": saved.get("name", current.get("name")),
    }


async def async_import_recipe(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Import a recipe from a web page or a video. Returns where it now lives."""
    api = _Mealie(hass, _mealie_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID)))
    url = str(call.data[ATTR_URL]).strip()
    try:
        slug = await api.request(
            "POST", "/recipes/create/url", {"url": url, "includeTags": False},
            timeout=_IMPORT_TIMEOUT,
        )
    except HomeAssistantError as err:
        # Mealie answers 400 when nothing it tried found a recipe.
        if "said 400" in str(err):
            raise ServiceValidationError(f"No recipe could be read from {url}.") from err
        raise
    if not isinstance(slug, str) or not slug:
        raise HomeAssistantError("Mealie imported the recipe but did not say where.")
    saved = await api.request("GET", f"/recipes/{slug}") or {}
    return {
        "slug": saved.get("slug", slug),
        "recipe_id": saved.get("id"),
        "name": saved.get("name"),
    }


async def async_delete_recipe(hass: HomeAssistant, call: ServiceCall) -> None:
    """Delete a recipe. Meals already planned with it lose their recipe."""
    api = _Mealie(hass, _mealie_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID)))
    await api.request("DELETE", f"/recipes/{call.data[ATTR_RECIPE]}")


def async_register_recipe_services(hass: HomeAssistant) -> None:
    """Register once. A reload of the entry must not register twice."""
    if hass.services.has_service(DOMAIN, SERVICE_SAVE_RECIPE):
        return

    async def _save(call: ServiceCall) -> ServiceResponse:
        return await async_save_recipe(hass, call)

    async def _delete(call: ServiceCall) -> None:
        await async_delete_recipe(hass, call)

    async def _import(call: ServiceCall) -> ServiceResponse:
        return await async_import_recipe(hass, call)

    hass.services.async_register(
        DOMAIN, SERVICE_SAVE_RECIPE, _save,
        schema=SAVE_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_RECIPE, _delete, schema=DELETE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_IMPORT_RECIPE, _import,
        schema=IMPORT_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
