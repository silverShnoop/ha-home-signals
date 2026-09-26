"""Keeping a photo from a card where an AI task can see it."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_signals.const import DOMAIN, SERVICE_SAVE_PHOTO
from custom_components.home_signals.photos import KEEP

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


async def _start(hass: HomeAssistant, tmp_path: Path) -> Path:
    hass.config.media_dirs = {"local": str(tmp_path)}
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return tmp_path / DOMAIN


async def _save(hass: HomeAssistant, image: str, **extra) -> dict:
    return await hass.services.async_call(
        DOMAIN, SERVICE_SAVE_PHOTO, {"image": image, **extra},
        blocking=True, return_response=True,
    )


async def test_a_photo_lands_where_an_attachment_can_reach_it(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    root = await _start(hass, tmp_path)
    answer = await _save(
        hass, "data:image/jpeg;base64," + base64.b64encode(JPEG).decode(), folder="fridge"
    )
    name = answer["media_content_id"].rsplit("/", 1)[1]
    assert answer["media_content_id"] == f"media-source://media_source/local/{DOMAIN}/fridge/{name}"
    assert answer["media_content_type"] == "image/jpeg"
    assert (root / "fridge" / name).read_bytes() == JPEG


async def test_only_the_newest_are_kept(hass: HomeAssistant, tmp_path: Path) -> None:
    root = await _start(hass, tmp_path)
    for _ in range(KEEP + 3):
        await _save(hass, base64.b64encode(JPEG).decode())
    assert len(list((root / "photos").iterdir())) == KEEP


@pytest.mark.parametrize(
    ("image", "folder", "why"),
    [
        ("not base64!", "photos", "not base64"),
        (base64.b64encode(b"GIF89a" + b"\x00" * 20).decode(), "photos", "JPEG, PNG or WebP"),
        (base64.b64encode(JPEG).decode(), "../etc", "letters, numbers"),
    ],
)
async def test_refusals_say_why(
    hass: HomeAssistant, tmp_path: Path, image: str, folder: str, why: str
) -> None:
    await _start(hass, tmp_path)
    with pytest.raises(ServiceValidationError, match=why):
        await _save(hass, image, folder=folder)
