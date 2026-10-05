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

A recipe can also say what can be done ahead. The method stays the
recipe's own, in its own order -- prepping is optional, and a method
rewritten prep-first reads wrongly to anybody cooking it all on the night.
Each step that can be done ahead is noted in the recipe's ``extras`` as
``prep``, by its place in the method (``n``, from 1): how far ahead, where
it keeps, and, where a step does two things, which half goes ahead and
which stays at the stove, and any line that only applies when it was made
ahead ("cover and chill"). By place rather than by step id, because a
method saved as text gets new step ids every time and would lose its notes
on the first typo fixed. An older split moved the prep steps to the front
under "Prep ahead" and "To cook" section titles, with no ``n``; those are
still read, by their order.

Where a recipe came from, and what AI did to it, is kept in ``extras`` as
``provenance``: the source (a page, a video, a photo, something said,
typed, or written by AI from a name), and a list of events -- read,
written, split, tagged -- each with the model and when. A step AI read,
wrote or changed carries a mark naming the event. A step a person edits
loses its mark, because it is theirs now.
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
    ATTR_IMAGE,
    ATTR_INGREDIENTS,
    ATTR_METHOD,
    ATTR_NAME,
    ATTR_PREP,
    ATTR_AI,
    ATTR_RECIPE,
    ATTR_SECTIONS,
    ATTR_SERVINGS,
    ATTR_SOURCE,
    ATTR_TAGS,
    ATTR_TOTAL_TIME,
    ATTR_URL,
    DOMAIN,
    MEALIE_DOMAIN,
    SERVICE_DELETE_RECIPE,
    SERVICE_IMPORT_RECIPE,
    SERVICE_MARK_MADE,
    SERVICE_PRUNE_TAGS,
    SERVICE_RECIPE_INDEX,
    SERVICE_SAVE_RECIPE,
)
from .photos import decode_photo

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
    # A list is tried first. cv.string turns anything into text, so with it
    # first a list of tags arrived as "['Dinner', 'Quick']" and was split on
    # its commas into tags called "['Dinner'" and "'Quick']".
    vol.Optional(ATTR_INGREDIENTS): vol.Any([cv.string], cv.string),
    vol.Optional(ATTR_METHOD): vol.Any([cv.string], cv.string),
    vol.Optional(ATTR_TAGS): vol.Any([cv.string], cv.string),
    vol.Optional(ATTR_FAVOURITE): cv.boolean,
    vol.Optional(ATTR_PREP): vol.Any(None, dict),
    # Where the recipe came from, and something AI just did to it. See the
    # module's docstring.
    vol.Optional(ATTR_SOURCE): dict,
    vol.Optional(ATTR_AI): dict,
    vol.Optional(ATTR_SECTIONS): vol.Any(None, [dict]),
    # The recipe's photo, as a base64 JPEG, PNG or WebP (a data URL is
    # fine): the dish, cut from a cookbook page or taken on its own.
    vol.Optional(ATTR_IMAGE): cv.string,
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
})

INDEX_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
})

PRUNE_SCHEMA = vol.Schema({
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

    async def set_image(self, slug: str, raw: bytes, ext: str, kind: str) -> None:
        """Replace a recipe's photo. Mealie takes it as a form upload."""
        form = aiohttp.FormData()
        form.add_field("image", raw, filename=f"image.{ext}", content_type=kind)
        form.add_field("extension", ext)
        try:
            async with self._session.put(
                f"{self._base}/recipes/{slug}/image", data=form,
                headers=self._headers, timeout=_TIMEOUT,
            ) as resp:
                if resp.status >= 400:
                    detail = (await resp.text())[:300]
                    raise HomeAssistantError(f"Mealie refused the photo: {resp.status} {detail}")
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


PREP_TITLE = "Prep ahead"
COOK_TITLE = "To cook"
_PREP_MODES = ("split", "none", "order")
_STEP_KEYS = ("ahead_max", "ahead_min", "minutes")
# The words a step's note can carry: the half done ahead and the half left
# for the stove, where one step does both; how to keep it when it is made
# ahead (store, said at the prep); and what that changes on the night
# (if_ahead, said at the stove).
_TEXT_KEYS = ("ahead", "cook", "store", "if_ahead")
_TEXT_MAX = 600
_TITLE_MAX = 80


def _sections_of(instructions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The method's section titles, as the step each one starts at."""
    return [
        {"n": i, "title": str(step.get("title")).strip()}
        for i, step in enumerate(instructions, start=1)
        if isinstance(step, dict) and str(step.get("title") or "").strip()
        and str(step.get("title")).strip() not in (PREP_TITLE, COOK_TITLE)
    ]


def _apply_sections(
    instructions: list[dict[str, Any]], sections: list[Any]
) -> list[dict[str, Any]]:
    """Title the method's groups of steps, Mealie's own way: each title on
    the step its group starts at. Every other title is cleared, so the
    sections given are all there are."""
    titles: dict[int, str] = {}
    for item in sections:
        if not isinstance(item, dict):
            raise ServiceValidationError("Each section is {n, title}.")
        try:
            n = int(item.get("n"))
        except (TypeError, ValueError) as err:
            raise ServiceValidationError("Each section names the step it starts at, by its number from 1.") from err
        title = str(item.get("title") or "").strip()[:_TITLE_MAX]
        if not 1 <= n <= len(instructions):
            raise ServiceValidationError(f"There is no step {n} for a section to start at.")
        if title:
            titles[n] = title
    steps = [dict(s) for s in instructions]
    for i, step in enumerate(steps, start=1):
        step["title"] = titles.get(i, "")
    return steps


def _prep_step(value: Any) -> dict[str, Any]:
    """One prep step's note, kept to the fields the house uses."""
    raw = value if isinstance(value, dict) else {}
    out: dict[str, Any] = {}
    try:
        n = int(raw.get("n"))
    except (TypeError, ValueError):
        n = 0
    if n >= 1:
        out["n"] = n
    for key in _TEXT_KEYS:
        text = str(raw.get(key) or "").strip()
        if text:
            out[key] = text[:_TEXT_MAX]
    for key in _STEP_KEYS:
        try:
            number = float(raw.get(key))
        except (TypeError, ValueError):
            continue
        if number >= 0:
            out[key] = int(number) if number == int(number) else number
    for key in ("keeps", "source"):
        if raw.get(key):
            out[key] = str(raw[key]).strip()
    return out


def _prep_of(recipe: dict[str, Any]) -> dict[str, Any] | None:
    """The split a recipe carries, or None for one that was never split.

    A split whose count no longer fits the method (steps deleted in
    Mealie's own editor) is read as no split rather than a wrong one.
    """
    extras = recipe.get("extras") if isinstance(recipe.get("extras"), dict) else {}
    raw = extras.get(ATTR_PREP)
    if not raw:
        return None
    try:
        got = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(got, dict) or got.get("mode") not in ("split", "none"):
        return None
    if got["mode"] == "none":
        return {"mode": "none", "checked": bool(got.get("checked"))}
    steps = [_prep_step(s) for s in (got.get("steps") or [])]
    method = recipe.get("recipeInstructions") or []
    if not steps or len(steps) >= len(method) + 1:
        return None
    out = {"mode": "split", "steps": steps, "checked": bool(got.get("checked"))}
    if _in_place(steps):
        # Noted in place: every note names a step that is still there.
        if not _places_fit(steps, len(method)):
            return None
        out["in_place"] = True
        reheat = str(got.get("reheat") or "").strip()
        if reheat:
            out["reheat"] = reheat[:_TEXT_MAX]
            try:
                at = int(got.get("reheat_at"))
            except (TypeError, ValueError):
                at = steps[0]["n"]
            out["reheat_at"] = at if 1 <= at <= len(method) else steps[0]["n"]
        return out
    if original := _original(got.get("original")):
        out["original"] = original
    return out


def _in_place(steps: list[dict[str, Any]]) -> bool:
    """Whether a split names its steps' places, rather than moving them."""
    return bool(steps) and all("n" in s for s in steps)


def _places_fit(steps: list[dict[str, Any]], count: int) -> bool:
    """Each note on a different step of the method, in its order."""
    places = [s["n"] for s in steps]
    return all(1 <= n <= count for n in places) and places == sorted(set(places))


def _original(value: Any) -> list[str]:
    """The method as it was before an unchecked split, as lines."""
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if isinstance(x, str) and x.strip()]


def _apply_prep(
    instructions: list[dict[str, Any]], extras: dict[str, Any], prep: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Mark the split on the method and in extras; or take it off again."""
    mode = prep.get("mode")
    if mode not in _PREP_MODES:
        raise ServiceValidationError("prep.mode is split, none or order.")
    steps = [dict(s) for s in instructions]
    for step in steps:
        if str(step.get("title") or "").strip() in (PREP_TITLE, COOK_TITLE):
            step["title"] = ""
    extras = {k: v for k, v in (extras or {}).items() if k != ATTR_PREP}
    if mode == "order":
        return steps, extras
    if mode == "none":
        extras[ATTR_PREP] = json.dumps({"mode": "none", "checked": bool(prep.get("checked"))})
        return steps, extras
    timing = [_prep_step(s) for s in (prep.get("steps") or [])]
    if not timing or len(timing) > len(steps):
        raise ServiceValidationError(
            "A split needs between one prep step and every step of the method."
        )
    if _in_place(timing):
        # Noted in place: the method is left exactly as it is.
        timing.sort(key=lambda s: s["n"])
        if not _places_fit(timing, len(steps)):
            raise ServiceValidationError(
                "Each prep note names a different step of the method, by its number from 1."
            )
        stored = {"mode": "split", "steps": timing, "checked": bool(prep.get("checked"))}
        reheat = str(prep.get("reheat") or "").strip()
        if reheat:
            stored["reheat"] = reheat[:_TEXT_MAX]
            try:
                stored["reheat_at"] = int(prep.get("reheat_at"))
            except (TypeError, ValueError):
                stored["reheat_at"] = timing[0]["n"]
        extras[ATTR_PREP] = json.dumps(stored)
        return steps, extras
    steps[0]["title"] = PREP_TITLE
    if len(timing) < len(steps):
        steps[len(timing)]["title"] = COOK_TITLE
    stored: dict[str, Any] = {"mode": "split", "steps": timing, "checked": bool(prep.get("checked"))}
    # Kept only until somebody has looked: "Keep in order" puts the
    # method back as it came, which a split that moved or cut steps cannot
    # otherwise know. A checked split has no use for it.
    if not stored["checked"] and (original := _original(prep.get("original"))):
        stored["original"] = original
    extras[ATTR_PREP] = json.dumps(stored)
    return steps, extras


# ---- where a recipe came from ----

PROVENANCE = "provenance"
_SOURCES = ("page", "video", "photo", "said", "typed", "written")
_MARKS = ("interpreted", "created", "enhanced")
_EVENTS = ("read", "wrote", "split", "tagged", "checked")
# A recipe read from a video is transcribed by Mealie's own AI first.
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "instagram.com", "tiktok.com", "facebook.com", "fb.watch")


def _now() -> str:
    return dt_util.now().replace(microsecond=0).isoformat()


def _provenance_of(recipe: dict[str, Any]) -> dict[str, Any]:
    """Where a recipe came from and what AI did to it, as stored.

    Always answers: a recipe with nothing stored has an empty history and
    the date Mealie says it was added.
    """
    extras = recipe.get("extras") if isinstance(recipe.get("extras"), dict) else {}
    raw = extras.get(PROVENANCE)
    try:
        got = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        got = {}
    got = got if isinstance(got, dict) else {}
    source = got.get("source") if isinstance(got.get("source"), dict) else {}
    out_source: dict[str, Any] = {}
    if source.get("kind") in _SOURCES:
        out_source["kind"] = source["kind"]
    for key in ("url", "added", "from"):
        if source.get(key):
            out_source[key] = str(source[key])[:300]
    if "added" not in out_source:
        added = recipe.get("createdAt") or recipe.get("dateAdded")
        if added:
            out_source["added"] = str(added)
    events = []
    for e in got.get("events") or []:
        if not isinstance(e, dict) or e.get("what") not in _EVENTS:
            continue
        try:
            eid = int(e.get("id"))
        except (TypeError, ValueError):
            continue
        event = {"id": eid, "what": e["what"]}
        for key in ("by", "model", "at", "note"):
            if e.get(key):
                event[key] = str(e[key])[:200]
        events.append(event)
    known = {e["id"] for e in events}
    marks = {}
    raw_marks = got.get("marks") if isinstance(got.get("marks"), dict) else {}
    for key, m in raw_marks.items():
        if not isinstance(m, dict) or m.get("mark") not in _MARKS:
            continue
        try:
            n = int(key)
            eid = int(m.get("event"))
        except (TypeError, ValueError):
            continue
        if n >= 1 and eid in known:
            marks[str(n)] = {"mark": m["mark"], "event": eid}
    return {"source": out_source, "events": events, "marks": marks}


def _texts(instructions: list[dict[str, Any]]) -> list[str]:
    return [str(s.get("text") or "").strip() for s in instructions if isinstance(s, dict)]


def _follow_marks(marks: dict[str, Any], before: list[str], after: list[str]) -> dict[str, Any]:
    """A method saved again keeps each step's mark only where the step's
    words are unchanged, wherever it now sits. A step a person rewrote is
    theirs, and loses it."""
    out: dict[str, Any] = {}
    used: set[int] = set()
    for key, mark in marks.items():
        i = int(key) - 1
        if i < 0 or i >= len(before):
            continue
        for j, text in enumerate(after):
            if j not in used and text == before[i]:
                used.add(j)
                out[str(j + 1)] = mark
                break
    return out


def _add_event(prov: dict[str, Any], ai: dict[str, Any], count: int) -> None:
    """Record something AI did, and mark the steps it did it to."""
    what = str(ai.get("what") or "").strip()
    if what not in _EVENTS:
        raise ServiceValidationError(f"ai.what is one of {', '.join(_EVENTS)}.")
    eid = max([e["id"] for e in prov["events"]] + [0]) + 1
    event: dict[str, Any] = {"id": eid, "what": what, "at": _now()}
    for key in ("by", "model", "note"):
        if ai.get(key):
            event[key] = str(ai[key]).strip()[:200]
    prov["events"].append(event)
    mark = ai.get("mark")
    if mark is None:
        return
    if mark not in _MARKS:
        raise ServiceValidationError(f"ai.mark is one of {', '.join(_MARKS)}.")
    steps = ai.get("steps", "all")
    if steps == "all":
        places = range(1, count + 1)
    else:
        places = []
        for n in steps if isinstance(steps, list) else [steps]:
            try:
                places.append(int(n))
            except (TypeError, ValueError):
                continue
    for n in places:
        if 1 <= n <= count:
            prov["marks"][str(n)] = {"mark": mark, "event": eid}


def _stored_provenance(prov: dict[str, Any]) -> str:
    return json.dumps({"source": prov["source"], "events": prov["events"], "marks": prov["marks"]})


def _source_of_url(url: str) -> str:
    host = re.sub(r"^https?://(www\.|m\.)?", "", url.lower()).split("/")[0]
    return "video" if any(host == h or host.endswith("." + h) for h in _VIDEO_HOSTS) else "page"


async def async_save_recipe(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Create a recipe, or change the one named. Returns where it now lives."""
    data = call.data
    api = _Mealie(hass, _mealie_entry(hass, data.get(ATTR_CONFIG_ENTRY_ID)))
    slug = data.get(ATTR_RECIPE)

    # Refused before anything is written, so a bad photo cannot leave half
    # a new recipe behind.
    photo = decode_photo(data[ATTR_IMAGE]) if data.get(ATTR_IMAGE) else None

    if not slug:
        name = str(data.get(ATTR_NAME) or "").strip()
        if not name:
            raise ServiceValidationError("A new recipe needs a name.")
        # Asked before anything is made. Mealie creates a second one under
        # "Name (1)" and then refuses the rename to the name asked for, so
        # the save failed and left a "1 Cup Flour" shell behind every time.
        same = await _named(api, name)
        if same:
            raise ServiceValidationError(f"“{same}” is already in the recipe box.")
        created = await api.request("POST", "/recipes", {"name": name})
        # Mealie answers a create with the new slug, as a bare JSON string.
        slug = created if isinstance(created, str) else (created or {}).get("slug")
        if not slug:
            raise HomeAssistantError("Mealie created the recipe but did not say where.")
        made = slug
    else:
        made = None

    async with _recipe_lock(hass, slug):
        try:
            saved = await _save_to(api, slug, data)
            if photo:
                await api.set_image(saved["slug"], *photo)
                saved["image"] = True
        except Exception:
            # A new recipe that could not be filled in is taken away again,
            # rather than left in the box as an empty shell.
            if made:
                try:
                    await api.request("DELETE", f"/recipes/{made}")
                except HomeAssistantError:
                    pass
            raise
        return saved


def _slugish(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


async def _named(api: _Mealie, name: str) -> str | None:
    """The name of a recipe already called this, or that Mealie would give
    the same address."""
    listing = await api.request("GET", "/recipes?perPage=-1") or {}
    want = name.casefold()
    for r in listing.get("items") or []:
        if not isinstance(r, dict):
            continue
        have = str(r.get("name") or "")
        if have.casefold() == want or (r.get("slug") and r["slug"] == _slugish(name)):
            return have or name
    return None


def _recipe_lock(hass: HomeAssistant, slug: str) -> asyncio.Lock:
    """One write at a time to a recipe.

    A save reads the recipe and writes it back. Mealie writes a recipe's
    ingredients and steps by deleting and re-adding them, so two saves at
    once both add theirs: an import's tagging and its split, which run
    side by side, left every line of a recipe twice, and the second save's
    extras overwrote the first's.
    """
    locks: dict[str, asyncio.Lock] = hass.data.setdefault(f"{DOMAIN}_recipe_locks", {})
    return locks.setdefault(str(slug), asyncio.Lock())


async def _save_to(api: _Mealie, slug: str, data: Any) -> dict[str, Any]:
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

    # The split is applied to the method as it will be after this save: the
    # new one when a method was sent, otherwise the one already there.
    extras = dict(current.get("extras")) if isinstance(current.get("extras"), dict) else {}
    extras_changed = False
    if data.get(ATTR_PREP) is not None:
        method = patch.get("recipeInstructions", current.get("recipeInstructions") or [])
        patch["recipeInstructions"], extras = _apply_prep(method, extras, data[ATTR_PREP])
        extras_changed = True
    if data.get(ATTR_SECTIONS) is not None:
        method = patch.get("recipeInstructions", current.get("recipeInstructions") or [])
        patch["recipeInstructions"] = _apply_sections(method, data[ATTR_SECTIONS])

    # Where it came from, and what AI did: kept with it, and a step a person
    # rewrote gives up its mark.
    prov = _provenance_of(current)
    had = json.dumps(prov, sort_keys=True)
    after = _texts(patch.get("recipeInstructions", current.get("recipeInstructions") or []))
    if "recipeInstructions" in patch:
        prov["marks"] = _follow_marks(prov["marks"], _texts(current.get("recipeInstructions") or []), after)
    source = data.get(ATTR_SOURCE)
    if isinstance(source, dict) and source.get("kind") in _SOURCES:
        kept = {"kind": source["kind"], "added": prov["source"].get("added") or _now()}
        for key in ("url", "from"):
            if source.get(key):
                kept[key] = str(source[key])[:300]
        prov["source"] = kept
    elif not data.get(ATTR_RECIPE) and "kind" not in prov["source"]:
        # A new recipe with nothing said about it was typed in.
        prov["source"] = {"kind": "typed", "added": _now()}
    if isinstance(data.get(ATTR_AI), dict):
        _add_event(prov, data[ATTR_AI], len(after))
    if json.dumps(prov, sort_keys=True) != had:
        extras[PROVENANCE] = _stored_provenance(prov)
        extras_changed = True
    if extras_changed:
        patch["extras"] = extras

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
        "prep": _prep_of(saved),
        "sections": _sections_of(saved.get("recipeInstructions") or []),
        "provenance": _provenance_of(saved),
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


def _tag_slug(name: str) -> str:
    """The slug Mealie would give a tag: what makes two names the same tag."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


async def _tags_for(api: _Mealie, names: list[str]) -> list[dict[str, Any]]:
    """Mealie's tags for these names, creating any it does not have yet.

    Matched by slug, as Mealie does: "quick", "Quick" and "'Quick'" are one
    tag, and a second can never be created beside it. A tag found under
    another spelling is renamed to the one asked for, so a mangled name
    left by an earlier save is mended rather than kept.
    """
    async def known() -> dict[str, dict[str, Any]]:
        have = await api.request("GET", "/organizers/tags?perPage=-1") or {}
        return {
            str(t.get("slug") or _tag_slug(str(t.get("name", "")))): t
            for t in (have.get("items") or [])
            if isinstance(t, dict)
        }

    tags = await known()
    out = []
    for name in names:
        slug = _tag_slug(name)
        if not slug:
            continue
        tag = tags.get(slug)
        if tag is None:
            try:
                tag = await api.request("POST", "/organizers/tags", {"name": name}) or {}
            except HomeAssistantError as err:
                # Made by someone else a moment ago: read it back.
                if "said 409" not in str(err):
                    raise
                tags = await known()
                tag = tags.get(slug) or {}
            tags[slug] = tag
        elif tag.get("id") and str(tag.get("name", "")).lower() != name.lower():
            renamed = await api.request("PUT", f"/organizers/tags/{tag['id']}", {"name": name})
            tag = renamed if isinstance(renamed, dict) and renamed.get("slug") else {**tag, "name": name}
            tags[slug] = tag
        if tag.get("slug") and all(t["slug"] != tag["slug"] for t in out):
            out.append({"id": tag.get("id"), "name": tag.get("name", name), "slug": tag["slug"]})
    return out


async def async_prune_tags(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Delete every tag no recipe uses. Answers with their names."""
    api = _Mealie(hass, _mealie_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID)))
    empty = await api.request("GET", "/organizers/tags/empty") or []
    if isinstance(empty, dict):
        empty = empty.get("items") or []
    gone = []
    for tag in empty:
        if isinstance(tag, dict) and tag.get("id"):
            await api.request("DELETE", f"/organizers/tags/{tag['id']}")
            gone.append(tag.get("name"))
    return {"deleted": gone}


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
    # Where it came from. A video is transcribed and read by Mealie's own
    # AI, which does not say which model it used; every step it wrote is
    # marked as read by AI.
    kind = _source_of_url(url)
    prov = _provenance_of(saved)
    if "kind" not in prov["source"]:
        prov["source"] = {"kind": kind, "url": url, "added": _now()}
        if kind == "video":
            _add_event(prov, {"what": "read", "by": "Mealie", "mark": "interpreted",
                              "note": "Read from the video by Mealie's AI"},
                       len(_texts(saved.get("recipeInstructions") or [])))
        extras = dict(saved.get("extras")) if isinstance(saved.get("extras"), dict) else {}
        extras[PROVENANCE] = _stored_provenance(prov)
        try:
            await api.request("PATCH", f"/recipes/{saved.get('slug', slug)}", {"extras": extras})
        except HomeAssistantError:
            pass
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

    cache: dict[str, tuple[str, list[str], Any, Any, Any]] = hass.data.setdefault(_INDEX_CACHE, {})
    gate = asyncio.Semaphore(_INDEX_PARALLEL)

    async def lines(summary: dict[str, Any]) -> tuple[list[str], Any, Any, Any]:
        slug = summary["slug"]
        stamp = str(summary.get("updatedAt") or summary.get("dateUpdated") or "")
        hit = cache.get(slug)
        if hit and hit[0] == stamp and len(hit) == 5:
            return hit[1], hit[2], hit[3], hit[4]
        async with gate:
            try:
                full = await api.request("GET", f"/recipes/{slug}") or {}
            except HomeAssistantError:
                return (hit[1], hit[2], hit[3], hit[4]) if hit and len(hit) == 5 else ([], None, None, [])
        got = (_ingredient_lines(full), _prep_of(full), _provenance_of(full),
               _sections_of(full.get("recipeInstructions") or []))
        cache[slug] = (stamp, *got)
        return got

    all_lines = await asyncio.gather(*(lines(r) for r in summaries))
    live = {r["slug"] for r in summaries}
    for gone in [slug for slug in cache if slug not in live]:
        del cache[gone]

    recipes = []
    tag_names: set[str] = set()
    for summary, (ingredients, prep, provenance, sections) in zip(summaries, all_lines, strict=True):
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
            # Where it came from, so a link shared a second time is known
            # before it is imported again.
            "source": summary.get("orgURL") or None,
            # The prep split, or None for a recipe that was never split.
            "prep": prep,
            # Where it came from and what AI did to it.
            "provenance": provenance,
            # The method's groups of steps: each title, at the step it starts.
            "sections": sections,
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

    async def _prune(call: ServiceCall) -> ServiceResponse:
        return await async_prune_tags(hass, call)

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
        DOMAIN, SERVICE_PRUNE_TAGS, _prune,
        schema=PRUNE_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_MARK_MADE, _made,
        schema=MARK_MADE_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
