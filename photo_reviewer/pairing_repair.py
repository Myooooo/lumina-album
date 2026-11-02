"""One-off migration for camera-album pairings written before volume identities.

Those rows have no ``volume_key``, so the card they point at can be checked only
through the integer column - which is 0 for the exFAT cards that need this most.
Filling the key in the first time the card is seen makes the pairing as good as a
fresh one, without the user having to re-import or re-pair anything.
"""

from __future__ import annotations

import logging
from typing import Any

from .db import Database
from .volume import volume_identity

logger = logging.getLogger(__name__)

#: Records that the backfill has run, so it is not attempted on every start.
PAIRING_REPAIR_KEY = "card_pairing_identity"
PAIRING_REPAIR_VERSION = "1"


def needs_pairing_repair(db: Database) -> bool:
    return db.get_meta(PAIRING_REPAIR_KEY) != PAIRING_REPAIR_VERSION


def repair_pairing_identities(db: Database) -> dict[str, int]:
    """Fill in ``volume_key`` for pairings that predate it.

    Only pairings whose card is connected right now can be filled: the identity
    has to be read from the volume itself, and a card that is away has none. The
    rest are left alone - they keep working through the legacy integer check, and
    the next import of that card records a fresh identity anyway.
    """
    report = {"checked": 0, "filled": 0, "duplicates_merged": 0}
    seen_albums: dict[str, list[dict[str, Any]]] = {}
    for pairing in db.list_card_albums():
        report["checked"] += 1
        seen_albums.setdefault(pairing["folder"], []).append(pairing)

    for folder, pairings in seen_albums.items():
        # Older builds appended a row per drive letter instead of replacing the
        # previous one. Keep the newest (the list is already ordered) and drop
        # the rest so the album has exactly one card again.
        for stale in pairings[1:]:
            db.remove_card_album(folder, stale["source"])
            report["duplicates_merged"] += 1

        current = pairings[0]
        if current.get("volume_key"):
            continue
        identity = volume_identity(current["source"])
        if identity is None:
            continue
        db.upsert_card_album(
            folder,
            current["source"],
            current.get("volume"),
            f"{identity.key}:{identity.source}",
        )
        report["filled"] += 1
        logger.info(
            "card pairing identity backfilled for %s (%s)", folder, identity.source
        )

    return report


def run_pairing_repairs(db: Database) -> dict[str, object]:
    if not needs_pairing_repair(db):
        return {}
    report = repair_pairing_identities(db)
    db.set_meta(PAIRING_REPAIR_KEY, PAIRING_REPAIR_VERSION)
    return {"card_pairings": report}
