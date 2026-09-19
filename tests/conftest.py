"""Test fixtures.

These run against a real Home Assistant rather than against stubs. The
state machine under test is almost entirely about TIME — a floor that has
to hold for five unbroken minutes, a cycle whose length decides whether it
counts as laundry — and the only honest way to test that is to move a real
clock past a real scheduled callback. A stub that calls the timer straight
back proves the callback is wired, not that the floor works.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Home Assistant refuses to load a custom component without this."""
    yield
