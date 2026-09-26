"""Writing recipes to Mealie, which Home Assistant's own integration cannot.

The core Mealie integration reads recipes and imports one from a link, and
that is all. A family recipe with no web page, a quantity that needs fixing
after an import, a recipe nobody wants any more -- each of those meant
opening Mealie's own interface, and the point of the meal card is that
nobody has to.

So these actions talk to Mealie's API directly. They borrow the address
and token from the Mealie integration's config entry rather than asking for
them again: a second copy of the token is a second place for it to go stale,
and the house already told Home Assistant where Mealie is.

An ingredient line that has not changed keeps Mealie's own parse of it --
the food, the unit, the quantity it worked out on import. Only a line that
was actually edited is replaced by plain text. Rewriting every line as text
on every save would quietly throw that away the first time anybody fixed a
typo in the method.

Two more serve finding a meal rather than writing one. ``recipe_index``
answers every recipe with what a picker needs to filter it: tags, the
ingredient lines, when it was last made and whether it is a favourite.
Mealie's recipe list carries no ingredients, so each full recipe is read
once and kept until Mealie says it changed. ``mark_made`` records that a
recipe was eaten, which is what "not had lately" is measured from.
"""

from __future__ import annotations

import asyncio
import datetime as dt
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
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DATE,
    ATTR_DESCRIPTION,
    ATTR_FAVOURITE,
    ATTR_INGREDIENTS,
    ATTR_METHOD,
    ATTR_NAME,
    ATTR_RECIPE,
    ATTR_SERVINGS,
    ATTR_TAGS,
    ATTR_TOTAL_TIME,
    ATTR_URL,
    DOMAIN,
    MEALIE_DOMAIN,
    SERVICE_DELETE_RECIPE,
    SERVICE_IMPORT_RECIPE,
    SERVICE_MARK_MADE,
    SERVICE_RECIPE_INDEX,
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
    vol.Optional(ATTR_TAGS): vol.Any(cv.string, [cv.string]),
    vol.Optional(ATTR_FAVOURITE): cv.boolean,
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
})

INDEX_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
})

MARK_MADE_SCHEMA = vol.Schema({
    vol.Required(ATTR_RECIPE): cv.string,
    vol.Optional(ATTR_DATE): cv.date,
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
})

# Full recipes, by slug, for the index: (when Mealie last changed it, the
# ingredient lines). Kept in hass.data so a reload does not read them all
# again, and dropped per recipe as soon as its date changes.
_INDEX_CACHE = f"{DOMAIN}_recipe_index"
# Mealie is on the same box; a few at a time is quick and leaves it room.
_INDEX_PARALLEL = 6

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

    tags = _tag_names(data.get(ATTR_TAGS))
    if tags is not None:
        patch["tags"] = await _tags_for(api, tags)

    saved = current
    if patch:
        answer = await api.request("PATCH", f"/recipes/{slug}", patch)
        saved = answer if isinstance(answer, dict) else current
    # A rename moves the recipe to a new slug, so the answer is read from
    # what Mealie sent back, not from what was asked for.
    slug = saved.get("slug", slug)
    if ATTR_FAVOURITE in data:
        await _set_favourite(api, slug, data[ATTR_FAVOURITE])
    return {
        "slug": slug,
        "recipe_id": saved.get("id", current.get("id")),
        "name": saved.get("name", current.get("name")),
        "tags": [t.get("name") for t in (saved.get("tags") or []) if isinstance(t, dict)],
    }


def _tag_names(value: Any) -> list[str] | None:
    """Tag names from a list or a comma-separated string, deduplicated."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.split(",")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = str(item).strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


async def _tags_for(api: _Mealie, names: list[str]) -> list[dict[str, Any]]:
    """Mealie's tags for these names, creating any it does not have yet.

    Matched ignoring case, so "quick" from one script and "Quick" from
    another are the same tag rather than two chips side by side.
    """
    have = await api.request("GET", "/organizers/tags?perPage=-1") or {}
    known = {
        str(t.get("name", "")).lower(): t
        for t in (have.get("items") or [])
        if isinstance(t, dict)
    }
    out = []
    for name in names:
        tag = known.get(name.lower())
        if tag is None:
            tag = await api.request("POST", "/organizers/tags", {"name": name}) or {}
            known[name.lower()] = tag
        if tag.get("slug"):
            out.append({"id": tag.get("id"), "name": tag.get("name", name), "slug": tag["slug"]})
    return out


async def _set_favourite(api: _Mealie, slug: str, favourite: bool) -> None:
    """A favourite belongs to a Mealie user: the one whose token this is."""
    me = await api.request("GET", "/users/self") or {}
    if not me.get("id"):
        raise HomeAssistantError("Mealie did not say whose token this is.")
    await api.request(
        "POST", f"/users/{me['id']}/ratings/{slug}", {"isFavorite": bool(favourite)}
    )


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


def _ingredient_lines(recipe: dict[str, Any]) -> list[str]:
    out = []
    for ing in recipe.get("recipeIngredient") or []:
        if not isinstance(ing, dict):
            continue
        text = next(
            (str(ing.get(k)).strip() for k in ("display", "note", "originalText")
             if ing.get(k) and str(ing.get(k)).strip()),
            "",
        )
        if text:
            out.append(text)
    return out


def _day(value: Any) -> str | None:
    """A Mealie timestamp as a local date, or None."""
    if not value:
        return None
    parsed = dt_util.parse_datetime(str(value))
    if parsed is None:
        parsed_date = dt_util.parse_date(str(value)[:10])
        return parsed_date.isoformat() if parsed_date else None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return dt_util.as_local(parsed).date().isoformat()


async def async_recipe_index(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Every recipe, with what a picker filters on."""
    api = _Mealie(hass, _mealie_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID)))
    listing = await api.request("GET", "/recipes?perPage=-1&orderBy=name&orderDirection=asc") or {}
    summaries = [r for r in (listing.get("items") or []) if isinstance(r, dict) and r.get("slug")]

    # Favourites are the token user's. A Mealie that refuses the question
    # (an old version, a token without a user) still gets an index.
    favourites: set[str] = set()
    try:
        mine = await api.request("GET", "/users/self/favorites") or {}
        favourites = {
            str(r.get("recipeId")) for r in (mine.get("ratings") or [])
            if isinstance(r, dict) and r.get("isFavorite")
        }
    except HomeAssistantError:
        pass

    cache: dict[str, tuple[str, list[str]]] = hass.data.setdefault(_INDEX_CACHE, {})
    gate = asyncio.Semaphore(_INDEX_PARALLEL)

    async def lines(summary: dict[str, Any]) -> list[str]:
        slug = summary["slug"]
        stamp = str(summary.get("updatedAt") or summary.get("dateUpdated") or "")
        hit = cache.get(slug)
        if hit and hit[0] == stamp:
            return hit[1]
        async with gate:
            try:
                full = await api.request("GET", f"/recipes/{slug}") or {}
            except HomeAssistantError:
                return hit[1] if hit else []
        got = _ingredient_lines(full)
        cache[slug] = (stamp, got)
        return got

    all_lines = await asyncio.gather(*(lines(r) for r in summaries))
    live = {r["slug"] for r in summaries}
    for gone in [slug for slug in cache if slug not in live]:
        del cache[gone]

    recipes = []
    tag_names: set[str] = set()
    for summary, ingredients in zip(summaries, all_lines, strict=True):
        tags = [
            str(t.get("name")) for t in (summary.get("tags") or [])
            if isinstance(t, dict) and t.get("name")
        ]
        tag_names.update(tags)
        recipes.append({
            "recipe_id": summary.get("id"),
            "slug": summary.get("slug"),
            "name": summary.get("name"),
            "total_time": summary.get("totalTime"),
            "image": summary.get("image"),
            "tags": tags,
            "ingredients": ingredients,
            "last_made": _day(summary.get("lastMade")),
            "date_added": _day(summary.get("dateAdded") or summary.get("createdAt")),
            "favourite": str(summary.get("id")) in favourites,
        })
    return {"recipes": recipes, "tags": sorted(tag_names, key=str.lower)}


async def async_mark_made(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Record that a recipe was eaten, on a day (today unless given).

    Only ever moves the date forward: marking last Tuesday's dinner after
    it was cooked again on Friday must not make it look older than it is.
    """
    api = _Mealie(hass, _mealie_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID)))
    recipe = await api.request("GET", f"/recipes/{call.data[ATTR_RECIPE]}") or {}
    slug = recipe.get("slug")
    if not slug:
        raise ServiceValidationError("Mealie has no such recipe.")
    day: dt.date = call.data.get(ATTR_DATE) or dt_util.now().date()
    before = _day(recipe.get("lastMade"))
    if before and before >= day.isoformat():
        return {"slug": slug, "last_made": before, "changed": False}
    # Midday local, so the date reads the same in any timezone Mealie shows it in.
    stamp = dt.datetime.combine(day, dt.time(12), tzinfo=dt_util.get_default_time_zone())
    await api.request("PATCH", f"/recipes/{slug}/last-made", {"timestamp": stamp.isoformat()})
    return {"slug": slug, "last_made": day.isoformat(), "changed": True}


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

    async def _index(call: ServiceCall) -> ServiceResponse:
        return await async_recipe_index(hass, call)

    async def _made(call: ServiceCall) -> ServiceResponse:
        return await async_mark_made(hass, call)

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
    hass.services.async_register(
        DOMAIN, SERVICE_RECIPE_INDEX, _index,
        schema=INDEX_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_MARK_MADE, _made,
        schema=MARK_MADE_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
