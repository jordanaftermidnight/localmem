"""LOCALMEM contradiction detection — temporal triple conflict resolution."""

from __future__ import annotations

import logging

import aiosqlite

from .models import ContradictionEvent, Triple, _now

logger = logging.getLogger(__name__)


async def detect_and_resolve(
    db: aiosqlite.Connection,
    new_triple: Triple,
) -> ContradictionEvent | None:
    """Check for contradicting active triples and resolve if found.

    When a new triple has the same (subject, predicate) as an existing active
    triple but a different object, the old triple is superseded:
    - old.valid_to is set to now
    - old.superseded_by is set to the new triple's id
    - a ContradictionEvent is returned for the calling agent to handle
    """
    cursor = await db.execute(
        "SELECT * FROM triples WHERE subject=? AND predicate=? AND valid_to IS NULL",
        (new_triple.subject, new_triple.predicate),
    )
    existing = await cursor.fetchone()

    if not existing or existing["object"] == new_triple.object:
        return None

    now = _now()
    await db.execute(
        "UPDATE triples SET valid_to=?, superseded_by=? WHERE id=?",
        (now, new_triple.id, existing["id"]),
    )

    old_triple = Triple(
        id=existing["id"],
        subject=existing["subject"],
        predicate=existing["predicate"],
        object=existing["object"],
        confidence=existing["confidence"],
        valid_from=existing["valid_from"],
        valid_to=now,
        source_agent=existing["source_agent"],
        source_entry_id=existing["source_entry_id"],
        created_at=existing["created_at"],
        superseded_by=new_triple.id,
    )

    event = ContradictionEvent(
        old_triple=old_triple,
        new_triple=new_triple,
        subject=new_triple.subject,
        predicate=new_triple.predicate,
        old_value=existing["object"],
        new_value=new_triple.object,
    )

    logger.info(
        f"Contradiction detected: {new_triple.subject}.{new_triple.predicate} "
        f"changed from '{event.old_value}' to '{event.new_value}'"
    )
    return event
