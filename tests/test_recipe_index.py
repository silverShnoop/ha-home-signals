"""Finding a meal: the recipe index, tags, favourites and "last made".

Mealie's HTTP API is mocked. The index is what a picker filters on, so the
tests pin what it carries and that it does not reread every recipe on every
call: a panel opens the picker many times a day.
"""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.home_signals.const import (
    DOMAIN,
    SERVICE_MARK_MADE,
    SERVICE_RECIPE_INDEX,
    SERVICE_SAVE_RECIPE,
)

BASE = "http://mealie.local:9000/api"
LISTING = f"{BASE}/recipes?perPage=-1&orderBy=name&orderDirection=asc"

SUMMARIES = {"items": [
    {"id": "r1", "slug": "fajitas", "name": "Chicken fajitas", "totalTime": "45 minutes",
     "image": "abc", "tags": [{"name": "Dinner", "slug": "dinner"}, {"name": "Chicken", "slug": "chicken"}],
     "lastMade": "2026-09-01T18:00:00Z", "dateAdded": "2026-08-20", "updatedAt": "2026-09-01T10:00:00Z"},
    {"id": "r2", "slug": "oats", "name": "Overnight oats", "totalTime": None,
     "image": None, "tags": [], "lastMade": None, "dateAdded": "2026-09-25",
     "updatedAt": "2026-09-25T10:00:00Z"},
]}
FAJITAS = {"id": "r1", "slug": "fajitas", "recipeIngredient": [
    {"display": "500g chicken thighs"}, {"note": "2 peppers"}, {"display": ""},
]}
OATS = {"id": "r2", "slug": "oats", "recipeIngredient": [{"originalText": "50g oats"}]}


async def _start(hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain="mealie",
        data={"host": "http://mealie.local:9000", "api_token": "secret-token"},
    ).add_to_hass(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _calls(aioclient_mock: AiohttpClientMocker, method: str, path: str) -> list:
    return [
        call for call in aioclient_mock.mock_calls
        if call[0] == method and str(call[1]).split("?")[0].endswith(path)
    ]


def _body(call) -> dict:
    return json.loads(call[2]) if isinstance(call[2], (str, bytes)) else call[2]


def _mock_box(aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(LISTING, json=SUMMARIES)
    aioclient_mock.get(f"{BASE}/users/self/favorites", json={"ratings": [
        {"recipeId": "r2", "isFavorite": True},
    ]})
    aioclient_mock.get(f"{BASE}/recipes/fajitas", json=FAJITAS)
    aioclient_mock.get(f"{BASE}/recipes/oats", json=OATS)


async def test_the_index_carries_what_a_picker_filters_on(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    _mock_box(aioclient_mock)

    answer = await hass.services.async_call(
        DOMAIN, SERVICE_RECIPE_INDEX, {}, blocking=True, return_response=True,
    )

    fajitas, oats = answer["recipes"]
    assert fajitas["name"] == "Chicken fajitas"
    assert fajitas["tags"] == ["Dinner", "Chicken"]
    assert fajitas["ingredients"] == ["500g chicken thighs", "2 peppers"], "a blank line was kept"
    assert fajitas["last_made"] == "2026-09-01"
    assert fajitas["favourite"] is False
    assert oats["favourite"] is True
    assert oats["ingredients"] == ["50g oats"]
    assert oats["last_made"] is None
    assert answer["tags"] == ["Chicken", "Dinner"]


async def test_a_second_index_does_not_reread_recipes_that_have_not_changed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    _mock_box(aioclient_mock)

    for _ in range(2):
        await hass.services.async_call(
            DOMAIN, SERVICE_RECIPE_INDEX, {}, blocking=True, return_response=True,
        )

    assert len(_calls(aioclient_mock, "GET", "/recipes/fajitas")) == 1


async def test_saving_tags_creates_the_missing_ones_and_matches_case(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    aioclient_mock.get(f"{BASE}/recipes/fajitas", json={**FAJITAS, "name": "Chicken fajitas"})
    aioclient_mock.get(f"{BASE}/organizers/tags?perPage=-1", json={"items": [
        {"id": "t1", "name": "Dinner", "slug": "dinner"},
    ]})
    aioclient_mock.post(
        f"{BASE}/organizers/tags", status=201,
        json={"id": "t2", "name": "Quick", "slug": "quick"},
    )
    aioclient_mock.patch(f"{BASE}/recipes/fajitas", json={
        **FAJITAS, "tags": [{"name": "Dinner"}, {"name": "Quick"}],
    })

    answer = await hass.services.async_call(
        DOMAIN, SERVICE_SAVE_RECIPE,
        {"recipe": "fajitas", "tags": ["dinner", "Quick", "quick"]},
        blocking=True, return_response=True,
    )

    created = _calls(aioclient_mock, "POST", "/organizers/tags")
    assert [_body(c) for c in created] == [{"name": "Quick"}], "a tag was made twice"
    (patch,) = _calls(aioclient_mock, "PATCH", "/recipes/fajitas")
    assert [t["slug"] for t in _body(patch)["tags"]] == ["dinner", "quick"]
    assert answer["tags"] == ["Dinner", "Quick"]


async def test_a_favourite_is_the_token_users(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    aioclient_mock.get(f"{BASE}/recipes/fajitas", json=FAJITAS)
    aioclient_mock.get(f"{BASE}/users/self", json={"id": "u1"})
    aioclient_mock.post(f"{BASE}/users/u1/ratings/fajitas", json=None)

    await hass.services.async_call(
        DOMAIN, SERVICE_SAVE_RECIPE, {"recipe": "fajitas", "favourite": True},
        blocking=True, return_response=True,
    )

    (rated,) = _calls(aioclient_mock, "POST", "/users/u1/ratings/fajitas")
    assert _body(rated) == {"isFavorite": True}
    assert not _calls(aioclient_mock, "PATCH", "/recipes/fajitas"), "nothing else was asked to change"


async def test_mark_made_only_moves_forward(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    aioclient_mock.get(f"{BASE}/recipes/r1", json={
        "id": "r1", "slug": "fajitas", "lastMade": "2026-09-10T12:00:00Z",
    })
    aioclient_mock.patch(f"{BASE}/recipes/fajitas/last-made", json={})

    older = await hass.services.async_call(
        DOMAIN, SERVICE_MARK_MADE, {"recipe": "r1", "date": "2026-09-05"},
        blocking=True, return_response=True,
    )
    newer = await hass.services.async_call(
        DOMAIN, SERVICE_MARK_MADE, {"recipe": "r1", "date": "2026-09-20"},
        blocking=True, return_response=True,
    )

    assert older["changed"] is False
    assert newer == {"slug": "fajitas", "last_made": "2026-09-20", "changed": True}
    (sent,) = _calls(aioclient_mock, "PATCH", "/recipes/fajitas/last-made")
    assert _body(sent)["timestamp"].startswith("2026-09-20T12:00:00")


async def test_a_list_of_tags_arrives_as_a_list(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The bug this guards: cv.string first turned the list into its text."""
    await _start(hass)
    aioclient_mock.get(f"{BASE}/recipes/fajitas", json=FAJITAS)
    aioclient_mock.get(f"{BASE}/organizers/tags?perPage=-1", json={"items": [
        {"id": "t1", "name": "Dinner", "slug": "dinner"},
        {"id": "t2", "name": "Chicken", "slug": "chicken"},
    ]})
    aioclient_mock.patch(f"{BASE}/recipes/fajitas", json=FAJITAS)

    await hass.services.async_call(
        DOMAIN, SERVICE_SAVE_RECIPE, {"recipe": "fajitas", "tags": ["Dinner", "Chicken"]},
        blocking=True, return_response=True,
    )

    (patch,) = _calls(aioclient_mock, "PATCH", "/recipes/fajitas")
    assert [t["name"] for t in _body(patch)["tags"]] == ["Dinner", "Chicken"]
    assert not _calls(aioclient_mock, "POST", "/organizers/tags")


async def test_a_mangled_tag_is_found_by_its_slug_and_renamed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    aioclient_mock.get(f"{BASE}/recipes/fajitas", json=FAJITAS)
    aioclient_mock.get(f"{BASE}/organizers/tags?perPage=-1", json={"items": [
        {"id": "t9", "name": "['Lunch'", "slug": "lunch"},
    ]})
    aioclient_mock.put(f"{BASE}/organizers/tags/t9", json={"id": "t9", "name": "Lunch", "slug": "lunch"})
    aioclient_mock.patch(f"{BASE}/recipes/fajitas", json=FAJITAS)

    await hass.services.async_call(
        DOMAIN, SERVICE_SAVE_RECIPE, {"recipe": "fajitas", "tags": "Lunch"},
        blocking=True, return_response=True,
    )

    (renamed,) = _calls(aioclient_mock, "PUT", "/organizers/tags/t9")
    assert _body(renamed) == {"name": "Lunch"}
    assert not _calls(aioclient_mock, "POST", "/organizers/tags"), "a second tag was made on the same slug"
    (patch,) = _calls(aioclient_mock, "PATCH", "/recipes/fajitas")
    assert _body(patch)["tags"] == [{"id": "t9", "name": "Lunch", "slug": "lunch"}]


async def test_prune_deletes_the_tags_nothing_uses(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    aioclient_mock.get(f"{BASE}/organizers/tags/empty", json=[
        {"id": "t8", "name": "'Quick'"}, {"id": "t9", "name": "old"},
    ])
    aioclient_mock.delete(f"{BASE}/organizers/tags/t8", json={})
    aioclient_mock.delete(f"{BASE}/organizers/tags/t9", json={})

    answer = await hass.services.async_call(
        DOMAIN, "prune_tags", {}, blocking=True, return_response=True,
    )

    assert answer == {"deleted": ["'Quick'", "old"]}
    assert len(_calls(aioclient_mock, "DELETE", "/organizers/tags/t8")) == 1
