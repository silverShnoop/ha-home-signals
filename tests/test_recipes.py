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

import asyncio
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
from custom_components.home_signals import recipes
from custom_components.home_signals.recipes import (
    _add_event,
    _apply_prep,
    _apply_sections,
    _follow_marks,
    _sections_of,
    _lines,
    _prep_of,
    _provenance_of,
    _source_of_url,
)

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


async def test_two_saves_to_one_recipe_take_turns(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An import tags and splits the recipe side by side. Mealie rewrites a
    recipe's lines on every save, so two saves at once left each line twice:
    the second must not read the recipe until the first has written it."""
    await _start(hass)
    seen: list[str] = []

    async def request(self, method, path, body=None, **kwargs):
        if path == "/recipes/sea-bass":
            seen.append(method)
        await asyncio.sleep(0.01)
        return dict(RECIPE)

    monkeypatch.setattr(recipes._Mealie, "request", request)

    await asyncio.gather(*(
        hass.services.async_call(
            DOMAIN, SERVICE_SAVE_RECIPE, {"recipe": "sea-bass", "tags": []} if i else
            {"recipe": "sea-bass", "prep": {"mode": "none"}},
            blocking=True, return_response=True,
        )
        for i in range(2)
    ))

    assert seen == ["GET", "PATCH", "GET", "PATCH"], seen


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
    aioclient_mock.get(f"{BASE}/recipes?perPage=-1", json={"items": [RECIPE]})
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
    assert {k: v for k, v in answer.items() if k != "provenance"} == {
        "slug": "nanas-curry", "recipe_id": "rid-2", "name": "Nana's curry", "tags": [], "prep": None,
        "sections": [],
    }
    stored = json.loads(patch["extras"]["provenance"])
    assert stored["source"]["kind"] == "typed", "a new recipe with nothing said about it was typed in"


async def test_a_name_already_in_the_box_makes_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Mealie makes "Sea bass (1)" and then refuses to rename it, so asking
    to create it anyway left an empty shell behind every time."""
    await _start(hass)
    aioclient_mock.get(f"{BASE}/recipes?perPage=-1", json={"items": [RECIPE]})

    for name in ("Sea bass", "sea  BASS!"):
        with pytest.raises(ServiceValidationError, match="Sea bass"):
            await hass.services.async_call(
                DOMAIN, SERVICE_SAVE_RECIPE, {"name": name, "ingredients": "1 fish"},
                blocking=True, return_response=True,
            )
    assert not _sent(aioclient_mock, "POST")


async def test_a_new_recipe_that_cannot_be_filled_in_is_taken_away(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    aioclient_mock.get(f"{BASE}/recipes?perPage=-1", json={"items": []})
    aioclient_mock.post(f"{BASE}/recipes", text='"stew"', status=201)
    aioclient_mock.get(f"{BASE}/recipes/stew", json={"id": "rid-3", "slug": "stew", "name": "Stew"})
    aioclient_mock.patch(f"{BASE}/recipes/stew", status=400, text="no")
    aioclient_mock.delete(f"{BASE}/recipes/stew", status=200)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, SERVICE_SAVE_RECIPE, {"name": "Stew", "ingredients": "1 onion"},
            blocking=True, return_response=True,
        )
    assert [str(c[1]) for c in aioclient_mock.mock_calls if c[0] == "DELETE"] == [f"{BASE}/recipes/stew"]


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
        json={"id": "rid-3", "slug": "spaghetti-puttanesca", "name": "Spaghetti Puttanesca",
              "recipeInstructions": [{"text": "Crush the garlic."}, {"text": "Add the tomatoes."}]},
    )
    aioclient_mock.patch(f"{BASE}/recipes/spaghetti-puttanesca", json={})

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
    # A video is read by Mealie's own AI: the source says so, and every step
    # it wrote is marked as read by AI.
    (patch,) = _sent(aioclient_mock, "PATCH")
    stored = json.loads(patch["extras"]["provenance"])
    assert stored["source"]["kind"] == "video"
    assert stored["source"]["url"] == "https://www.youtube.com/shorts/ekOjr2XP_zU"
    assert [e["what"] for e in stored["events"]] == ["read"]
    assert stored["events"][0]["by"] == "Mealie"
    assert stored["marks"] == {"1": {"mark": "interpreted", "event": 1}, "2": {"mark": "interpreted", "event": 1}}


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


def test_a_web_page_is_a_page_and_a_video_is_a_video() -> None:
    assert _source_of_url("https://www.bbcgoodfood.com/recipes/chicken-katsu-curry") == "page"
    assert _source_of_url("https://youtu.be/abc") == "video"
    assert _source_of_url("https://m.youtube.com/watch?v=1") == "video"
    assert _source_of_url("https://www.instagram.com/reel/x/") == "video"
    assert _source_of_url("https://notyoutube.com.example/x") == "page"


FAJITAS = [
    {"text": "Marinate the chicken."},
    {"text": "Fry the onion wedges; add the peppers."},
    {"text": "Griddle the chicken."},
]


def test_prep_noted_in_place_leaves_the_method_alone() -> None:
    steps, extras = _apply_prep(FAJITAS, {}, {
        "mode": "split", "checked": False,
        "steps": [
            {"n": 2, "ahead_max": 48, "keeps": "Fridge", "ahead": "Cut the onion into wedges and the peppers into strips.",
             "cook": "Fry the onion wedges; add the peppers."},
            {"n": 1, "ahead_max": 24, "keeps": "Fridge", "if_ahead": "Cover and chill."},
        ],
    })
    assert [s["text"] for s in steps] == [s["text"] for s in FAJITAS], "the method stays as it is"
    assert not any(s.get("title") for s in steps), "no Prep ahead / To cook sections"
    got = _prep_of({"recipeInstructions": steps, "extras": extras})
    assert got["in_place"] is True
    assert [s["n"] for s in got["steps"]] == [1, 2], "kept in the method's order"
    assert got["steps"][0]["if_ahead"] == "Cover and chill."
    assert got["steps"][1]["ahead"].startswith("Cut the onion")


def test_storing_it_and_the_night_are_two_lines() -> None:
    _, extras = _apply_prep(FAJITAS, {}, {
        "mode": "split", "steps": [{"n": 1, "ahead_max": 24, "store": "Cover and chill.",
                                    "if_ahead": "Take it out 20 mins before griddling."}],
    })
    step = _prep_of({"recipeInstructions": FAJITAS, "extras": extras})["steps"][0]
    assert step["store"] == "Cover and chill."
    assert step["if_ahead"] == "Take it out 20 mins before griddling."


def test_sections_title_groups_of_steps_the_way_mealie_does() -> None:
    steps = _apply_sections(FAJITAS, [{"n": 1, "title": "The chicken"}, {"n": 2, "title": " The veg "}])
    assert [s["title"] for s in steps] == ["The chicken", "The veg", ""]
    assert _sections_of(steps) == [{"n": 1, "title": "The chicken"}, {"n": 2, "title": "The veg"}]
    # The sections given are all there are: an empty list clears them.
    assert not any(s["title"] for s in _apply_sections(steps, []))
    # The old Prep ahead / To cook headings are not sections.
    assert _sections_of([{"text": "a", "title": "Prep ahead"}, {"text": "b", "title": "To cook"}]) == []
    with pytest.raises(ServiceValidationError):
        _apply_sections(FAJITAS, [{"n": 4, "title": "Nowhere"}])
    with pytest.raises(ServiceValidationError):
        _apply_sections(FAJITAS, [{"title": "No step"}])


def test_a_whole_part_made_ahead_carries_its_reheat() -> None:
    method = [{"text": f"Step {i}."} for i in range(1, 8)]
    _, extras = _apply_prep(method, {}, {
        "mode": "split", "checked": True, "reheat": "Reheat the sauce until piping hot.", "reheat_at": 3,
        "steps": [{"n": 3, "ahead_max": 72}, {"n": 4, "ahead_max": 72}, {"n": 5, "ahead_max": 72}],
    })
    got = _prep_of({"recipeInstructions": method, "extras": extras})
    assert got["reheat"] == "Reheat the sauce until piping hot."
    assert got["reheat_at"] == 3


def test_a_note_on_a_step_that_is_not_there_is_refused_or_ignored() -> None:
    with pytest.raises(ServiceValidationError):
        _apply_prep(FAJITAS, {}, {"mode": "split", "steps": [{"n": 9, "ahead_max": 24}]})
    with pytest.raises(ServiceValidationError):
        _apply_prep(FAJITAS, {}, {"mode": "split", "steps": [{"n": 1}, {"n": 1}]})
    # Steps deleted in Mealie's own editor since: read as no split.
    stale = {"prep": json.dumps({"mode": "split", "steps": [{"n": 3, "ahead_max": 24}]})}
    assert _prep_of({"recipeInstructions": FAJITAS[:2], "extras": stale}) is None


def test_an_older_split_is_still_read_by_its_order() -> None:
    extras = {"prep": json.dumps({"mode": "split", "checked": True, "steps": [{"ahead_max": 24}]})}
    got = _prep_of({"recipeInstructions": FAJITAS, "extras": extras})
    assert got["steps"] == [{"ahead_max": 24}]
    assert "in_place" not in got


def test_what_ai_did_is_recorded_and_a_rewritten_step_loses_its_mark() -> None:
    prov = _provenance_of({"createdAt": "2026-09-27T20:00:00"})
    assert prov == {"source": {"added": "2026-09-27T20:00:00"}, "events": [], "marks": {}}
    _add_event(prov, {"what": "wrote", "by": "Anthropic: Claude Sonnet 5", "model": "ai_task.x", "mark": "created"}, 3)
    _add_event(prov, {"what": "tagged", "by": "Anthropic: Claude Sonnet 5"}, 3)
    assert [e["id"] for e in prov["events"]] == [1, 2]
    assert set(prov["marks"]) == {"1", "2", "3"}
    assert all(m == {"mark": "created", "event": 1} for m in prov["marks"].values())
    # Step 2 rewritten by a person, and a new step put in front.
    before = ["Boil the potatoes.", "Fry the mince.", "Bake for 30 mins."]
    after = ["Heat the oven.", "Boil the potatoes.", "Fry the lamb mince.", "Bake for 30 mins."]
    marks = _follow_marks(prov["marks"], before, after)
    assert marks == {"2": {"mark": "created", "event": 1}, "4": {"mark": "created", "event": 1}}
    # Read back, the marks survive and an unknown event's are dropped.
    stored = {"extras": {"provenance": json.dumps({
        "source": {"kind": "written"}, "events": prov["events"],
        "marks": {"1": {"mark": "created", "event": 1}, "2": {"mark": "created", "event": 9}},
    })}}
    back = _provenance_of(stored)
    assert back["source"]["kind"] == "written"
    assert back["marks"] == {"1": {"mark": "created", "event": 1}}
    with pytest.raises(ServiceValidationError):
        _add_event(prov, {"what": "guessed"}, 3)


async def test_saving_with_ai_records_it(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    aioclient_mock.get(f"{BASE}/recipes/sea-bass", json=dict(RECIPE, extras={}))
    aioclient_mock.patch(f"{BASE}/recipes/sea-bass", json=dict(RECIPE))

    await hass.services.async_call(
        DOMAIN, SERVICE_SAVE_RECIPE,
        {"recipe": "sea-bass", "source": {"kind": "photo"},
         "ai": {"what": "read", "by": "Anthropic: Claude Sonnet 5", "model": "ai_task.anthropic_claude_sonnet_5", "mark": "interpreted"}},
        blocking=True, return_response=True,
    )
    (patch,) = _sent(aioclient_mock, "PATCH")
    stored = json.loads(patch["extras"]["provenance"])
    assert stored["source"]["kind"] == "photo"
    assert stored["events"][0]["model"] == "ai_task.anthropic_claude_sonnet_5"
    assert stored["marks"] == {"1": {"mark": "interpreted", "event": 1}}


JPEG = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2w=="


async def test_a_photo_goes_onto_the_recipe(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    aioclient_mock.get(f"{BASE}/recipes/sea-bass", json=RECIPE)
    aioclient_mock.put(f"{BASE}/recipes/sea-bass/image", json={"image": "abc"})

    answer = await hass.services.async_call(
        DOMAIN, SERVICE_SAVE_RECIPE, {"recipe": "sea-bass", "image": JPEG},
        blocking=True, return_response=True,
    )

    (put,) = [c for c in aioclient_mock.mock_calls if c[0] == "PUT"]
    assert str(put[1]).endswith("/recipes/sea-bass/image")
    assert answer["image"] is True


async def test_a_photo_that_is_not_one_writes_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _start(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_SAVE_RECIPE, {"name": "Stew", "image": "bm90IGEgcGhvdG8="},
            blocking=True, return_response=True,
        )
    assert not aioclient_mock.mock_calls, "a new recipe was made before the photo was refused"


# --- what reaches Mealie's URL --------------------------------------------


def test_a_recipe_is_a_slug_or_an_id_and_nothing_else() -> None:
    """The slug goes into a path. A slash or a question mark in it would be
    read as more path, or as a query, and `../` walks out of /recipes."""
    import voluptuous as vol

    for ok in ("sea-bass", "nanas_curry", "r1", "fa4a64da-a534-47b0-b86a-fe72973244fd", "A1"):
        assert recipes.DELETE_SCHEMA({"recipe": ok})["recipe"] == ok
        assert recipes.MARK_MADE_SCHEMA({"recipe": ok})["recipe"] == ok
        assert recipes.SAVE_SCHEMA({"recipe": ok})["recipe"] == ok
    for bad in ("../admin/users/x", "sea-bass/last-made", "a?b=c", "a b", "", "-x", ".", "a#b"):
        for schema in (recipes.DELETE_SCHEMA, recipes.MARK_MADE_SCHEMA, recipes.SAVE_SCHEMA):
            with pytest.raises(vol.Invalid):
                schema({"recipe": bad})


def test_every_path_segment_stays_one_segment() -> None:
    """An id from Mealie's own answer is not checked at the schema, so it is
    quoted on the way into the path instead."""
    assert recipes._path("recipes", "sea-bass") == "/recipes/sea-bass"  # noqa: SLF001
    assert recipes._path("recipes", "sea-bass", "last-made") == "/recipes/sea-bass/last-made"  # noqa: SLF001
    odd = recipes._path("users", "../x?y=1#z", "ratings", "a/b")  # noqa: SLF001
    head, *segments = odd.split("/")[1:]
    assert head == "users"
    for segment in segments:
        assert "/" not in segment and "?" not in segment and "#" not in segment
    assert odd == "/users/..%2Fx%3Fy%3D1%23z/ratings/a%2Fb"


async def test_an_odd_slug_is_refused_before_mealie_is_asked(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    import voluptuous as vol

    await _start(hass)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, SERVICE_DELETE_RECIPE, {"recipe": "../admin/users/x"}, blocking=True,
        )
    assert aioclient_mock.mock_calls == []


def test_an_import_link_is_a_web_address_and_not_the_house() -> None:
    """Mealie fetches whatever it is given, from inside the house."""
    import voluptuous as vol

    assert recipes.IMPORT_SCHEMA({"url": " https://example.com/recipe "})["url"] == "https://example.com/recipe"
    assert recipes.IMPORT_SCHEMA({"url": "http://93.184.216.34/x"})["url"] == "http://93.184.216.34/x"
    for bad in (
        "ftp://example.com/recipe",
        "file:///etc/passwd",
        "not a link",
        "http://127.0.0.1:8123/api/",
        "http://10.0.0.5/",
        "http://192.168.1.20:9000/",
        "http://172.16.3.4/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://0.0.0.0/",
        "http://localhost/",
        "http://LOCALHOST:8123/",
        "http://mealie.local/",
        "http://homeassistant/",
        "http://supervisor/core/api",
        "http://hassio/",
        "http://x.internal/",
    ):
        with pytest.raises(vol.Invalid):
            recipes.IMPORT_SCHEMA({"url": bad})


async def test_a_local_link_is_refused_before_mealie_is_asked(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    import voluptuous as vol

    await _start(hass)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, SERVICE_IMPORT_RECIPE, {"url": "http://127.0.0.1:8123/"},
            blocking=True, return_response=True,
        )
    assert aioclient_mock.mock_calls == []


async def test_a_recipes_lock_does_not_outlive_the_save(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """One lock per slug, while a save holds or waits for it, and no longer."""
    await _start(hass)
    aioclient_mock.get(f"{BASE}/recipes/sea-bass", json=RECIPE)
    aioclient_mock.patch(f"{BASE}/recipes/sea-bass", json={**RECIPE, "name": "Sea bass"})
    locks = hass.data.get(f"{DOMAIN}_recipe_locks")
    assert not locks
    await asyncio.gather(*(
        hass.services.async_call(
            DOMAIN, SERVICE_SAVE_RECIPE, {"recipe": "sea-bass", "description": str(n)},
            blocking=True,
        )
        for n in range(3)
    ))
    assert hass.data[f"{DOMAIN}_recipe_locks"] == {}
    # And after a save that failed.
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/recipes/sea-bass", status=500, text="boom")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, SERVICE_SAVE_RECIPE, {"recipe": "sea-bass", "description": "x"},
            blocking=True,
        )
    assert hass.data[f"{DOMAIN}_recipe_locks"] == {}


def test_a_number_that_is_not_finite_is_not_a_number() -> None:
    import voluptuous as vol

    from custom_components.home_signals.recipes import _prep_step

    assert recipes.SAVE_SCHEMA({"servings": "4"})["servings"] == 4.0
    for bad in ("inf", "-inf", "nan", float("inf"), float("nan")):
        with pytest.raises(vol.Invalid):
            recipes.SAVE_SCHEMA({"servings": bad})
    step = _prep_step({"n": 2, "minutes": float("inf"), "ahead_max": "nan", "ahead_min": 3})
    assert step == {"n": 2, "ahead_min": 3}
    assert "n" not in _prep_step({"n": float("inf")})
