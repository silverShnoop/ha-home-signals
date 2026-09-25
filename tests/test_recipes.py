"""Writing recipes to Mealie.

Mealie's HTTP API is mocked; the integration itself is set up for real, so
these also prove the actions are registered and borrow the Mealie entry's
address and token rather than needing their own.

The one behaviour worth most is the merge: an edit that fixes one line of
the method must not throw away Mealie's parse of every ingredient, which is
what a naive "replace the lists" save would do the first time anybody used
it.
"""

from __future__ import annotations

import json

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.home_signals.const import (
    DOMAIN,
    SERVICE_DELETE_RECIPE,
    SERVICE_IMPORT_RECIPE,
    SERVICE_SAVE_RECIPE,
)
from custom_components.home_signals.recipes import _lines

BASE = "http://mealie.local:9000/api"

PARSED_OIL = {
    "quantity": 3, "unit": {"name": "tablespoon"}, "food": {"name": "sunflower oil"},
    "note": "", "display": "3 tablespoons sunflower oil", "referenceId": "oil",
}
PARSED_GARLIC = {
    "quantity": 3, "unit": {"name": "clove"}, "food": {"name": "garlic"},
    "note": "thinly sliced", "display": "3 cloves garlic thinly sliced", "referenceId": "garlic",
}
RECIPE = {
    "id": "rid-1", "slug": "sea-bass", "name": "Sea bass",
    "recipeIngredient": [PARSED_OIL, PARSED_GARLIC],
    "recipeInstructions": [{"id": "s1", "text": "Heat the oil."}],
}


async def _start(hass: HomeAssistant, with_mealie: bool = True) -> None:
    if with_mealie:
        MockConfigEntry(
            domain="mealie",
            data={"host": "http://mealie.local:9000", "api_token": "secret-token"},
        ).add_to_hass(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _sent(aioclient_mock: AiohttpClientMocker, method: str) -> list:
    """The bodies sent with one method, in order."""
    return [
        json.loads(call[2]) if isinstance(call[2], (str, bytes)) else call[2]
        for call in aioclient_mock.mock_calls
        if call[0] == method
    ]


async def test_the_actions_exist(hass: HomeAssistant) -> None:
    await _start(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_SAVE_RECIPE)
    assert hass.services.has_service(DOMAIN, SERVICE_DELETE_RECIPE)
    assert hass.services.has_service(DOMAIN, SERVICE_IMPORT_RECIPE)


async def test_an_edit_keeps_the_parse_of_every_line_it_did_not_touch(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    aioclient_mock.get(f"{BASE}/recipes/sea-bass", json=RECIPE)
    aioclient_mock.patch(f"{BASE}/recipes/sea-bass", json={**RECIPE, "name": "Sea bass"})

    await hass.services.async_call(
        DOMAIN, SERVICE_SAVE_RECIPE,
        {
            "recipe": "sea-bass",
            "ingredients": "3 tablespoons sunflower oil\n4 cloves garlic",
        },
        blocking=True, return_response=True,
    )

    (patch,) = _sent(aioclient_mock, "PATCH")
    oil, garlic = patch["recipeIngredient"]
    assert oil == PARSED_OIL, "an unchanged line lost Mealie's parse"
    assert garlic["note"] == "4 cloves garlic" and garlic["food"] is None, (
        "an edited line should be plain text"
    )
    assert garlic["quantity"] == 0, "Mealie would show a stray 1 in front of it"
    assert "recipeInstructions" not in patch, "a method nobody sent was rewritten"


async def test_the_token_is_borrowed_from_the_mealie_integration(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    aioclient_mock.get(f"{BASE}/recipes/sea-bass", json=RECIPE)
    aioclient_mock.patch(f"{BASE}/recipes/sea-bass", json=RECIPE)

    await hass.services.async_call(
        DOMAIN, SERVICE_SAVE_RECIPE, {"recipe": "sea-bass", "name": "Sea bass"},
        blocking=True, return_response=True,
    )

    headers = aioclient_mock.mock_calls[0][3]
    assert headers["Authorization"] == "Bearer secret-token"


async def test_a_new_recipe_is_created_then_filled_in(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    aioclient_mock.post(f"{BASE}/recipes", text='"nanas-curry"', status=201)
    aioclient_mock.get(
        f"{BASE}/recipes/nanas-curry",
        json={"id": "rid-2", "slug": "nanas-curry", "name": "Nana's curry",
              "recipeIngredient": [], "recipeInstructions": []},
    )
    aioclient_mock.patch(
        f"{BASE}/recipes/nanas-curry",
        json={"id": "rid-2", "slug": "nanas-curry", "name": "Nana's curry"},
    )

    answer = await hass.services.async_call(
        DOMAIN, SERVICE_SAVE_RECIPE,
        {
            "name": "Nana's curry",
            "servings": 4,
            "total_time": "1 hour",
            "ingredients": ["- 2 onions", "1 tin tomatoes"],
            "method": "1. Fry the onions.\n2) Add the tomatoes.\n\n",
        },
        blocking=True, return_response=True,
    )

    assert _sent(aioclient_mock, "POST") == [{"name": "Nana's curry"}]
    (patch,) = _sent(aioclient_mock, "PATCH")
    assert [i["note"] for i in patch["recipeIngredient"]] == ["2 onions", "1 tin tomatoes"]
    assert [s["text"] for s in patch["recipeInstructions"]] == [
        "Fry the onions.", "Add the tomatoes.",
    ], "a pasted list's own numbers were kept, so Mealie would number them twice"
    assert patch["recipeServings"] == 4
    assert patch["totalTime"] == "1 hour"
    assert answer == {"slug": "nanas-curry", "recipe_id": "rid-2", "name": "Nana's curry"}


async def test_a_rename_answers_with_the_new_slug(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    aioclient_mock.get(f"{BASE}/recipes/sea-bass", json=RECIPE)
    aioclient_mock.patch(
        f"{BASE}/recipes/sea-bass",
        json={**RECIPE, "slug": "gingery-sea-bass", "name": "Gingery sea bass"},
    )

    answer = await hass.services.async_call(
        DOMAIN, SERVICE_SAVE_RECIPE, {"recipe": "sea-bass", "name": "Gingery sea bass"},
        blocking=True, return_response=True,
    )

    assert answer["slug"] == "gingery-sea-bass"


async def test_a_new_recipe_needs_a_name(hass: HomeAssistant) -> None:
    await _start(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_SAVE_RECIPE, {"ingredients": "2 onions"},
            blocking=True, return_response=True,
        )


async def test_without_mealie_it_says_so(hass: HomeAssistant) -> None:
    await _start(hass, with_mealie=False)
    with pytest.raises(ServiceValidationError, match="not set up"):
        await hass.services.async_call(
            DOMAIN, SERVICE_DELETE_RECIPE, {"recipe": "sea-bass"}, blocking=True,
        )


async def test_delete_and_a_refused_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    aioclient_mock.delete(f"{BASE}/recipes/sea-bass", status=200, json={})
    await hass.services.async_call(
        DOMAIN, SERVICE_DELETE_RECIPE, {"recipe": "sea-bass"}, blocking=True,
    )
    assert [c[0] for c in aioclient_mock.mock_calls] == ["DELETE"]

    aioclient_mock.clear_requests()
    aioclient_mock.delete(f"{BASE}/recipes/sea-bass", status=401)
    with pytest.raises(HomeAssistantError, match="refused the token"):
        await hass.services.async_call(
            DOMAIN, SERVICE_DELETE_RECIPE, {"recipe": "sea-bass"}, blocking=True,
        )


async def test_an_import_answers_with_where_the_recipe_lives(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    aioclient_mock.post(f"{BASE}/recipes/create/url", text='"spaghetti-puttanesca"', status=201)
    aioclient_mock.get(
        f"{BASE}/recipes/spaghetti-puttanesca",
        json={"id": "rid-3", "slug": "spaghetti-puttanesca", "name": "Spaghetti Puttanesca"},
    )

    answer = await hass.services.async_call(
        DOMAIN, SERVICE_IMPORT_RECIPE,
        {"url": " https://www.youtube.com/shorts/ekOjr2XP_zU "},
        blocking=True, return_response=True,
    )

    assert _sent(aioclient_mock, "POST") == [
        {"url": "https://www.youtube.com/shorts/ekOjr2XP_zU", "includeTags": False}
    ]
    assert answer == {
        "slug": "spaghetti-puttanesca", "recipe_id": "rid-3", "name": "Spaghetti Puttanesca",
    }


async def test_an_import_with_no_recipe_says_so(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    aioclient_mock.post(f"{BASE}/recipes/create/url", status=400, json={"detail": "BAD_RECIPE_DATA"})
    with pytest.raises(ServiceValidationError, match="No recipe could be read"):
        await hass.services.async_call(
            DOMAIN, SERVICE_IMPORT_RECIPE, {"url": "https://example.com/"},
            blocking=True, return_response=True,
        )


def test_lines_from_text_or_a_list() -> None:
    assert _lines(None) is None, "not given must stay different from empty"
    assert _lines("") == []
    assert _lines("• one\n* two\n3. three\n10) ten\n  ") == ["one", "two", "three", "ten"]
    assert _lines(["2 onions", "1.5 kg potatoes"]) == ["2 onions", "1.5 kg potatoes"], (
        "a quantity is not a list number"
    )
