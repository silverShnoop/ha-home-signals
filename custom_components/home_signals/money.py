"""How money is written on this panel.

One function, in its own module, because the second thing that needed it is
what made it shared: a wash's cost and a day's cost must not be spelled two
different ways on two cards in the same house.
"""

from __future__ import annotations


def money(pounds: float) -> str:
    """A cost the way somebody says it out loud: `33p`, or `£1.20`.

    Assembled in the backend rather than in the card because the switch
    between the two is a rule about the number, and the card's value
    language has no room for one -- a `suffix` cannot change its mind at a
    pound.

    It sits *beside* the number rather than replacing it, which is the same
    split `system_health` already makes between its rendered rows and its
    raw lists: an agent asked what the wash cost wants `0.33`, and a panel
    read from three metres away wants `33p`. One is derived from the other
    on a single line, so they cannot drift.

    The boundary is the case worth being careful about. Rounding in pence
    first and then deciding means 99.6p is `£1.00` rather than the
    technically-correct, obviously-wrong `100p`.
    """
    pence = round(pounds * 100)
    if pence < 100:
        return f"{pence}p"
    return f"£{pence / 100:.2f}"
