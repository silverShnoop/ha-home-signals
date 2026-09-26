"""Keeping a photo taken on the panel, so an AI task can look at it.

A card can take a photo -- a cookbook page, the inside of the fridge -- but
it has nowhere to put it. Home Assistant's own upload endpoint is for
administrators, and the kitchen panel is not one. An AI task reads its
attachments from media sources, so this writes the photo into local media
and answers with the id an attachment takes.

The card shrinks the photo before sending it: a websocket message is
limited to a few megabytes, and a model reads a page no better at twelve
megapixels than at two.

Only the newest few are kept. These are working copies, not a photo album,
and a folder that grows by one every time somebody wonders what is for tea
is a slow leak on a small disk.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from pathlib import Path
import re

import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import ATTR_FOLDER, ATTR_IMAGE, DOMAIN, SERVICE_SAVE_PHOTO

MAX_BYTES = 3 * 1024 * 1024
KEEP = 12

# What the first bytes of each accepted kind look like, and its extension.
_KINDS = (
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"RIFF", "webp", "image/webp"),
)
_DATA_URL = re.compile(r"^data:[\w/+.-]+;base64,", re.IGNORECASE)
_FOLDER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

SAVE_PHOTO_SCHEMA = vol.Schema({
    vol.Required(ATTR_IMAGE): cv.string,
    vol.Optional(ATTR_FOLDER, default="photos"): cv.string,
})


def _decode(image: str) -> tuple[bytes, str, str]:
    """The photo's bytes, extension and content type, or a clear refusal."""
    try:
        raw = base64.b64decode(_DATA_URL.sub("", image.strip()), validate=True)
    except (binascii.Error, ValueError) as err:
        raise ServiceValidationError("That is not a photo: it is not base64.") from err
    if len(raw) > MAX_BYTES:
        raise ServiceValidationError("That photo is too big. Send it smaller than 3 MB.")
    for magic, ext, kind in _KINDS:
        if raw.startswith(magic) and (ext != "webp" or raw[8:12] == b"WEBP"):
            return raw, ext, kind
    raise ServiceValidationError("That is not a JPEG, PNG or WebP photo.")


def _write(target: Path, name: str, raw: bytes) -> None:
    """Write the photo, then drop all but the newest few in its folder."""
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_bytes(raw)
    kept = sorted(
        (p for p in target.iterdir() if p.is_file()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for old in kept[KEEP:]:
        old.unlink(missing_ok=True)


async def async_save_photo(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Save a photo into local media and say how to attach it."""
    folder = str(call.data[ATTR_FOLDER]).strip().lower()
    if not _FOLDER.match(folder):
        raise ServiceValidationError("A folder is letters, numbers, - and _ only.")
    raw, ext, kind = _decode(call.data[ATTR_IMAGE])
    media = hass.config.media_dirs.get("local")
    if not media:
        raise ServiceValidationError("Home Assistant has no local media folder.")
    name = f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.{ext}"
    target = Path(media) / DOMAIN / folder
    await hass.async_add_executor_job(_write, target, name, raw)
    return {
        "media_content_id": f"media-source://media_source/local/{DOMAIN}/{folder}/{name}",
        "media_content_type": kind,
    }


def async_register_photo_services(hass: HomeAssistant) -> None:
    """Register once. A reload of the entry must not register twice."""
    if hass.services.has_service(DOMAIN, SERVICE_SAVE_PHOTO):
        return

    async def _save(call: ServiceCall) -> ServiceResponse:
        return await async_save_photo(hass, call)

    hass.services.async_register(
        DOMAIN, SERVICE_SAVE_PHOTO, _save,
        schema=SAVE_PHOTO_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
