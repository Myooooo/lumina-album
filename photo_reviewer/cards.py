"""Keep a local album and the camera card it came from in step.

An album folder that was filled by :mod:`photo_reviewer.importer` is a *camera
album*: its files sit at the same relative paths they had on the card, because
the import preserves the card's directory structure. That makes the card side of
any later operation a pure path join, and this module is where that join - and
everything that writes to a card - lives.

Deleting a photo locally can therefore also remove it from the card. The card is
often not plugged in when the user tidies up, so an operation that cannot run is
not lost: it is queued in the ``card_actions`` table and offered, with a preview,
the next time that card shows up.

Three guarantees hold everywhere in this module:

* a path is only ever handed to the filesystem after :func:`card_path_for` has
  proved it stays inside the card root;
* a card whose volume fingerprint no longer matches the recorded one is treated
  as a *different* disk (drive letters get recycled) and is never written to;
* a card that is away, read-only or replaced never turns a successful local
  operation into an error - the intent is queued instead.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import Database
from .importer import MAX_REPORTED_ITEMS
from .paths import canonical_path, is_within

logger = logging.getLogger(__name__)


class CardError(RuntimeError):
    """Raised when a card operation is unsafe or impossible."""


#: A file should be moved into the card's own trash.
KIND_DELETE = "delete"

#: A previously trashed file should be put back on the card.
KIND_RESTORE = "restore"

#: Waiting for the card to come back.
STATE_PENDING = "pending"

#: Already done on the card; the row remembers where the file went.
STATE_APPLIED = "applied"

#: The photo was permanently deleted locally, so the card copy has to be erased
#: (not trashed) the next time the card is reachable.
STATE_ERASE = "erase"


@dataclass
class CardTarget:
    """One local album plus the card it was filled from, right now."""

    album: str
    source: str
    volume: int | None = None
    connected: bool = False
    writable: bool = False
    same_volume: bool = True
    volume_unknown: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """True when the card is connected, writable and the right disk."""
        return self.connected and self.writable and self.same_volume

    @property
    def state(self) -> str:
        """A short machine-readable summary for the UI."""
        if not self.connected:
            return "disconnected"
        if not self.same_volume:
            return "volume_mismatch"
        if not self.writable:
            return "read_only"
        return "connected"

    def as_dict(self) -> dict[str, Any]:
        return {
            "album": self.album,
            "source": self.source,
            "volume": self.volume,
            "connected": self.connected,
            "writable": self.writable,
            "same_volume": self.same_volume,
            "volume_unknown": self.volume_unknown,
            "usable": self.usable,
            "state": self.state,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Volume identity
# ---------------------------------------------------------------------------

def volume_id(path: str) -> int | None:
    """Return a fingerprint of the volume holding ``path``.

    On Windows ``st_dev`` is the volume serial number the filesystem reports, so
    two different cards that both mounted as ``E:`` get different values and a
    recycled drive letter is detectable. POSIX reports a device number with the
    same property. ``None`` means the platform would not tell us; callers treat
    that as "unknown", never as a match.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    device = getattr(stat, "st_dev", None)
    return int(device) if device is not None else None


def resolve_card(
    album: str, source: str, recorded_volume: int | None = None
) -> CardTarget:
    """Check whether the card for ``album`` is plugged in and safe to write."""
    target = CardTarget(
        album=canonical_path(album),
        source=canonical_path(source),
        volume=recorded_volume,
    )
    if not target.source or not os.path.isdir(target.source):
        return target

    target.connected = True
    target.writable = os.access(target.source, os.W_OK)

    current = volume_id(target.source)
    if current is None or recorded_volume is None:
        target.volume_unknown = True
        target.warnings.append("无法读取磁盘指纹，本次跳过磁盘一致性校验。")
    elif current != recorded_volume:
        target.same_volume = False
        target.warnings.append("该路径现在挂载的是另一块磁盘，已停用同步以免误删。")
    else:
        target.volume = current

    if not target.writable:
        target.warnings.append("卡片处于写保护状态，暂时无法同步。")
    return target


def _unusable_reason(target: CardTarget) -> str:
    if not target.connected:
        return "卡片未连接"
    if not target.same_volume:
        return "该路径已不是上次那张卡"
    if not target.writable:
        return "卡片处于写保护状态"
    return "卡片不可用"


# ---------------------------------------------------------------------------
# Pairings
# ---------------------------------------------------------------------------

def record_pairing(db: Database, folder: str, source: str) -> dict[str, Any] | None:
    """Remember that ``folder`` is fed by the card at ``source``.

    Called after an import copies (or confirms) files, which is what makes a
    folder a camera album in the first place.
    """
    album = canonical_path(folder)
    card = canonical_path(source)
    if not album or not card or album == card:
        return None
    if is_within(card, album) or is_within(album, card):
        # Nested paths would make "relative to the album" ambiguous.
        return None
    volume = volume_id(card)
    db.upsert_card_album(album, card, volume)
    logger.info("camera album pairing recorded: %s <- %s", album, card)
    return {"album": album, "source": card, "volume": volume}


def pairing_for(db: Database, folder: str) -> dict[str, Any] | None:
    """Return the pairing for a local folder, if it is a camera album."""
    if not folder:
        return None
    return db.card_album_for(canonical_path(folder))


def target_for(db: Database, folder: str) -> CardTarget | None:
    """Resolve the card of a camera album, or ``None`` when it is not one."""
    pairing = pairing_for(db, folder)
    if not pairing:
        return None
    return resolve_card(pairing["folder"], pairing["source"], pairing.get("volume"))


def _queue_counts(db: Database, source: str) -> dict[str, int]:
    queued = db.card_actions(source=source, state=STATE_PENDING)
    erasing = db.card_actions(source=source, state=STATE_ERASE)
    return {
        "delete_count": sum(1 for a in queued if a["kind"] == KIND_DELETE),
        "restore_count": sum(1 for a in queued if a["kind"] == KIND_RESTORE),
        "erase_count": len(erasing),
        "raw_count": sum(1 for a in queued + erasing if a.get("is_raw")),
        "total_bytes": sum(
            int(a.get("size") or 0) for a in queued if a["kind"] == KIND_DELETE
        ),
        "total": len(queued) + len(erasing),
    }


def card_status(db: Database, folder: str | None = None) -> list[dict[str, Any]]:
    """Describe every camera album, its card and its queued work."""
    pairings = db.list_card_albums(canonical_path(folder) if folder else None)
    statuses = []
    for pairing in pairings:
        target = resolve_card(
            pairing["folder"], pairing["source"], pairing.get("volume")
        )
        entry = target.as_dict()
        entry["id"] = pairing.get("id")
        entry["updated_at"] = pairing.get("updated_at")
        entry["pending"] = _queue_counts(db, pairing["source"])
        statuses.append(entry)
    return statuses


# ---------------------------------------------------------------------------
# Paths on the card
# ---------------------------------------------------------------------------

def relative_to_album(album: str, path: str | None) -> str | None:
    """Return ``path`` relative to the album root, or ``None`` if outside it.

    The import mirrors the card layout, so this relative path is also the path
    on the card.
    """
    if not path:
        return None
    album_root = canonical_path(album)
    full = canonical_path(path)
    if not album_root or not full:
        return None
    try:
        relative = os.path.relpath(full, album_root)
    except ValueError:  # different drives
        return None
    if relative.startswith("..") or os.path.isabs(relative):
        return None
    return relative


def card_path_for(source: str, relative_path: str) -> str:
    """Join a card root and a relative path, refusing anything that escapes.

    ``relative_path`` ultimately comes from a database row built out of browser
    input, so it is validated rather than trusted: an absolute path or a ``..``
    segment raises instead of landing somewhere else on the disk.
    """
    if not relative_path:
        raise CardError("缺少相对路径，无法定位卡片上的文件")
    cleaned = str(relative_path).replace("/", os.sep).replace("\\", os.sep).strip()
    if not cleaned or os.path.isabs(cleaned) or cleaned.startswith(os.sep):
        raise CardError(f"路径不合法: {relative_path}")
    parts = [p for p in cleaned.split(os.sep) if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise CardError(f"路径不合法: {relative_path}")
    card_root = canonical_path(source)
    if not card_root:
        raise CardError("卡片路径为空")
    full = os.path.join(card_root, *parts)
    if not is_within(full, card_root):
        raise CardError(f"路径越界: {relative_path}")
    return full


def _walk_card(source: str, include_hidden: bool = False):
    """Walk a card, never descending into its dot-directories by default."""
    for root, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if include_hidden or not d.startswith(".")]
        yield root, files


def find_on_card(target: CardTarget, relative_path: str) -> str | None:
    """Locate a live photo on the card.

    Three attempts, in decreasing order of confidence:

    1. the recorded relative path, which is exact for anything the importer
       copied because it preserves the card's folder structure;
    2. the same path as a *tail* match, for a card whose root moved by a level
       (importing ``E:\\`` instead of ``E:\\DCIM`` puts an extra folder in front);
    3. a unique file-name match, for a card reorganised by hand.

    Attempts 2 and 3 only accept a single hit. Numbered camera folders repeat
    file names constantly (``100D3500\\DSC_0001.JPG`` and
    ``101D3500\\DSC_0001.JPG``), so an ambiguous stem must resolve to "not
    found" rather than to a guess that deletes the wrong picture.
    """
    if not target.connected:
        return None
    try:
        exact = card_path_for(target.source, relative_path)
    except CardError:
        exact = None
    if exact and os.path.isfile(exact):
        return exact

    cleaned = str(relative_path).replace("/", os.sep).replace("\\", os.sep).strip()
    tail = os.sep + os.path.normpath(cleaned).lower()
    stem = Path(relative_path).stem.lower()
    suffix = Path(relative_path).suffix.lower()
    tail_matches: list[str] = []
    name_matches: list[str] = []
    for root, files in _walk_card(target.source):
        for name in files:
            if name.startswith("._"):
                continue
            full = os.path.join(root, name)
            card_relative = os.path.relpath(full, target.source).lower()
            if card_relative.endswith(tail):
                tail_matches.append(full)
            elif Path(name).stem.lower() == stem and (
                not suffix or Path(name).suffix.lower() == suffix
            ):
                name_matches.append(full)

    if len(tail_matches) == 1:
        return tail_matches[0]
    if not tail_matches and len(name_matches) == 1:
        return name_matches[0]
    return None


def card_trash_dir(source: str, trash_dir_name: str) -> Path:
    """The card's own trash folder for today.

    Named like the local trash and kept hidden, which matters twice: the scanner
    and the importer both skip dot-directories, so a photo parked here is never
    re-indexed or copied back into the album.
    """
    stamp = datetime.now().astimezone().strftime("%Y%m%d")
    day = Path(canonical_path(source)) / trash_dir_name / stamp
    day.mkdir(parents=True, exist_ok=True)
    return day


def _free_name(directory: Path, source: str) -> Path:
    """Pick a name inside ``directory`` that does not collide."""
    dest = directory / Path(source).name
    if dest.exists():
        dest = (
            directory
            / f"{Path(source).stem}_{uuid.uuid4().hex[:8]}{Path(source).suffix.lower()}"
        )
    return dest


def trash_on_card(target: CardTarget, path: str, trash_dir_name: str) -> str:
    """Move one file from the card into the card's own trash folder."""
    if not target.usable:
        raise CardError(_unusable_reason(target))
    if not is_within(path, target.source):
        raise CardError(f"路径不在卡片内: {path}")
    if not os.path.exists(path):
        raise FileNotFoundError(f"卡片上找不到文件: {path}")
    dest = _free_name(card_trash_dir(target.source, trash_dir_name), path)
    shutil.move(path, str(dest))
    return str(dest)


def restore_on_card(target: CardTarget, card_path: str, relative_path: str) -> str:
    """Put a file from the card's trash back where it came from."""
    if not target.usable:
        raise CardError(_unusable_reason(target))
    if not is_within(card_path, target.source):
        raise CardError(f"路径不在卡片内: {card_path}")
    if not os.path.exists(card_path):
        raise FileNotFoundError(f"卡片回收站里找不到文件: {card_path}")
    original = card_path_for(target.source, relative_path)
    Path(original).parent.mkdir(parents=True, exist_ok=True)
    if os.path.exists(original):
        original = str(_free_name(Path(original).parent, original))
    shutil.move(card_path, original)
    return original


def find_in_card_trash(target: CardTarget, relative_path: str) -> str | None:
    """Look for a trashed copy on the card, searching only its hidden folders.

    The card's trash flattens the directory structure, so this is a name search.
    A card holding two different pictures with the same name is ambiguous and
    resolves to "not found" instead of moving an arbitrary one back.
    """
    if not target.connected:
        return None
    stem = Path(relative_path).stem.lower()
    suffix = Path(relative_path).suffix.lower()
    matches: list[str] = []
    for root, files in _walk_card(target.source, include_hidden=True):
        if root != target.source and not Path(root).name.startswith("."):
            # Inside the card, only dot-directories (its trash) hold removed
            # files; anything else is a photo in its own right.
            continue
        for name in files:
            if Path(name).stem.lower() != stem:
                continue
            if suffix and Path(name).suffix.lower() != suffix:
                continue
            matches.append(os.path.join(root, name))
            if len(matches) > 1:
                return None
    return matches[0] if len(matches) == 1 else None


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

def queue_action(
    db: Database,
    target: CardTarget,
    kind: str,
    relative_path: str,
    *,
    filename: str | None = None,
    size: int = 0,
    is_raw: bool = False,
    photo_id: int | None = None,
    state: str = STATE_PENDING,
    card_path: str | None = None,
) -> dict[str, Any]:
    """Record what should happen to one file on the card."""
    if kind not in (KIND_DELETE, KIND_RESTORE):
        raise CardError(f"未知的卡片操作: {kind}")
    action = {
        "album": target.album,
        "source": target.source,
        "volume": target.volume,
        "kind": kind,
        "state": state,
        "relative_path": relative_path,
        "card_path": card_path,
        "filename": filename or Path(relative_path).name,
        "size": int(size or 0),
        "is_raw": bool(is_raw),
        "photo_id": photo_id,
    }
    db.save_card_action(action)
    return action


def delete_from_card(
    db: Database,
    target: CardTarget,
    relative_path: str,
    trash_dir_name: str,
    *,
    filename: str | None = None,
    size: int = 0,
    is_raw: bool = False,
    photo_id: int | None = None,
) -> dict[str, Any]:
    """Delete one file from the card, or queue that deletion for later.

    Never raises for a card that is away, read-only or replaced: those cases
    queue the intent, so a successful local delete is not reported as a failure.
    """
    existing = db.card_action_for(target.album, relative_path)
    if existing and existing["state"] in (STATE_APPLIED, STATE_ERASE):
        # Already recorded against the card; do not downgrade a finished action
        # back into a queued one.
        return {
            "synced": False,
            "queued": False,
            "card_path": existing.get("card_path"),
            "already": True,
        }
    if existing and existing["kind"] == KIND_RESTORE:
        # The photo had been restored onto the card, so it is there again and
        # there is nothing to delete.
        db.delete_card_action(existing["id"])
        return {"synced": False, "queued": False, "card_path": None}

    if not target.usable:
        queue_action(
            db,
            target,
            KIND_DELETE,
            relative_path,
            filename=filename,
            size=size,
            is_raw=is_raw,
            photo_id=photo_id,
        )
        return {"synced": False, "queued": True, "card_path": None}

    path = find_on_card(target, relative_path)
    if path is None:
        # The card no longer has the file: nothing to sync, nothing to queue.
        return {"synced": False, "queued": False, "card_path": None}
    card_path = trash_on_card(target, path, trash_dir_name)
    queue_action(
        db,
        target,
        KIND_DELETE,
        relative_path,
        filename=filename,
        size=size,
        is_raw=is_raw,
        photo_id=photo_id,
        state=STATE_APPLIED,
        card_path=card_path,
    )
    return {"synced": True, "queued": False, "card_path": card_path}


def restore_to_card(
    db: Database,
    target: CardTarget,
    relative_path: str,
    *,
    filename: str | None = None,
    size: int = 0,
    is_raw: bool = False,
    photo_id: int | None = None,
) -> dict[str, Any]:
    """Undo a card deletion, or queue that undo for when the card returns."""
    existing = db.card_action_for(target.album, relative_path)
    if not existing:
        # The card never lost this file, so there is nothing to undo.
        return {"synced": False, "queued": False}

    if existing["state"] == STATE_PENDING:
        # The delete never reached the card; dropping the intent *is* the undo.
        db.delete_card_action(existing["id"])
        return {"synced": False, "queued": False, "cancelled": True}

    if existing["state"] == STATE_ERASE:
        # Permanently removed locally, so the local row is gone for good and the
        # card copy has nothing to come back to.
        return {"synced": False, "queued": False, "missing": True}

    if not target.usable:
        queue_action(
            db,
            target,
            KIND_RESTORE,
            relative_path,
            filename=filename or existing["filename"],
            size=size or int(existing.get("size") or 0),
            is_raw=is_raw or bool(existing.get("is_raw")),
            photo_id=photo_id,
            state=STATE_PENDING,
            card_path=existing.get("card_path"),
        )
        return {"synced": False, "queued": True}

    card_path = existing.get("card_path")
    if not card_path or not os.path.exists(card_path):
        card_path = find_in_card_trash(target, relative_path)
    if not card_path:
        db.delete_card_action(existing["id"])
        return {"synced": False, "queued": False, "missing": True}
    restored = restore_on_card(target, card_path, relative_path)
    db.delete_card_action(existing["id"])
    return {"synced": True, "queued": False, "card_path": restored}


def purge_for_photo(
    db: Database, target: CardTarget | None, photo_id: int
) -> dict[str, int]:
    """Handle the card side of permanently removing a photo.

    A trashed file on a reachable card is erased. A card that is away keeps the
    row, marked ``erase``, so the file is cleaned up the next time that card is
    connected instead of being silently forgotten. When the folder is not a
    camera album at all there is no card to wait for, so the rows just go.
    """
    actions = db.card_action_for_photo(photo_id)
    if not actions:
        return {"purged": 0, "deferred": 0}
    if target is None:
        db.delete_card_actions([a["id"] for a in actions])
        return {"purged": 0, "deferred": 0}

    purged = 0
    deferred = 0
    done: list[int] = []
    for action in actions:
        card_path = action.get("card_path")
        reachable = bool(target.connected and target.same_volume)
        if action["state"] == STATE_PENDING and action["kind"] == KIND_DELETE:
            # The local photo is gone for good, so the card copy must be erased
            # rather than moved into the card's trash.
            db.set_card_action_state(action["id"], STATE_ERASE)
            if reachable:
                # The delete never ran, so the card still holds the file where
                # it always was; the search has to cover the live tree too.
                purged += _erase(
                    target, card_path, action["relative_path"], search_live=True
                )
                db.delete_card_action(action["id"])
            else:
                deferred += 1
            continue
        if reachable:
            purged += _erase(target, card_path, action["relative_path"])
            done.append(action["id"])
        elif action["state"] != STATE_ERASE:
            db.set_card_action_state(action["id"], STATE_ERASE)
            deferred += 1
        else:
            deferred += 1
    db.delete_card_actions(done)
    return {"purged": purged, "deferred": deferred}


def _erase(
    target: CardTarget,
    card_path: str | None,
    relative_path: str,
    search_live: bool = False,
) -> int:
    """Remove a card file. Returns 1 when one was actually removed.

    ``search_live`` extends the search from the card's own trash to the whole
    card, which is what an erase queued before the deletion ever reached the card
    needs: the file is still sitting at its normal path.
    """
    candidate = card_path if card_path and os.path.isfile(card_path) else None
    if candidate is None and search_live:
        candidate = find_on_card(target, relative_path)
    if candidate is None:
        candidate = find_in_card_trash(target, relative_path)
    if not candidate:
        return 0
    if not is_within(candidate, target.source):
        raise CardError(f"路径不在卡片内: {candidate}")
    try:
        os.remove(candidate)
        return 1
    except OSError:
        logger.warning("could not erase %s from the card", candidate, exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# Pending work preview and execution
# ---------------------------------------------------------------------------

def _target_for_source(db: Database, source: str) -> CardTarget | None:
    pairing = next((p for p in db.list_card_albums() if p["source"] == source), None)
    if not pairing:
        return None
    return resolve_card(pairing["folder"], source, pairing.get("volume"))


def pending_preview(
    db: Database, source: str | None = None, limit: int = MAX_REPORTED_ITEMS
) -> dict[str, Any]:
    """Describe the queued card work, for a confirmation dialog."""
    source = canonical_path(source) if source else None
    actions = db.card_actions(source=source, state=STATE_PENDING)
    erasing = db.card_actions(source=source, state=STATE_ERASE)
    if source is None:
        sources = {a["source"] for a in actions + erasing}
        if len(sources) == 1:
            source = sources.pop()
    target = _target_for_source(db, source) if source else None

    items: list[dict[str, Any]] = []
    missing = 0
    delete_count = 0
    restore_count = 0
    erase_count = 0
    raw_count = 0
    total_bytes = 0

    for action in actions:
        is_delete = action["kind"] == KIND_DELETE
        on_card: bool | None = None
        if target and target.connected:
            if is_delete:
                on_card = find_on_card(target, action["relative_path"]) is not None
            else:
                on_card = _trash_copy_exists(target, action)
            if not on_card:
                missing += 1
        if is_delete:
            delete_count += 1
            total_bytes += int(action.get("size") or 0)
        else:
            restore_count += 1
        if action.get("is_raw"):
            raw_count += 1
        if len(items) < limit:
            items.append(
                {
                    "filename": action["filename"],
                    "relative_path": action["relative_path"],
                    "kind": action["kind"],
                    "size": int(action.get("size") or 0),
                    "is_raw": bool(action.get("is_raw")),
                    "on_card": on_card,
                    "album": action["album"],
                    "source": action["source"],
                }
            )

    for action in erasing:
        on_card = None
        if target and target.connected:
            on_card = _trash_copy_exists(target, action)
            if not on_card:
                missing += 1
        erase_count += 1
        if action.get("is_raw"):
            raw_count += 1
        if len(items) < limit:
            items.append(
                {
                    "filename": action["filename"],
                    "relative_path": action["relative_path"],
                    "kind": "erase",
                    "size": int(action.get("size") or 0),
                    "is_raw": bool(action.get("is_raw")),
                    "on_card": on_card,
                    "album": action["album"],
                    "source": action["source"],
                }
            )

    return {
        "source": source or "",
        "album": (actions + erasing)[0]["album"] if (actions or erasing) else "",
        "target": target.as_dict() if target else None,
        "ready": bool(target and target.usable),
        "summary": {
            "delete_count": delete_count,
            "restore_count": restore_count,
            "erase_count": erase_count,
            "raw_count": raw_count,
            "missing_count": missing,
            "total_bytes": total_bytes,
            "total": len(actions) + len(erasing),
        },
        "items": items,
        "items_truncated": max(0, (len(actions) + len(erasing)) - len(items)),
    }


def _trash_copy_exists(target: CardTarget, action: dict[str, Any]) -> bool:
    card_path = action.get("card_path")
    if card_path and os.path.exists(card_path):
        return True
    return find_in_card_trash(target, action["relative_path"]) is not None


def apply_pending(
    db: Database,
    source: str,
    trash_dir_name: str,
    include_raw: bool = True,
) -> dict[str, Any]:
    """Carry the queued work out on the card that just showed up.

    Every item is attempted independently: a card pulled out halfway through
    leaves the finished items recorded and the rest queued, so the next
    connection simply carries on.
    """
    source = canonical_path(source)
    summary: dict[str, Any] = {
        "source": source,
        "applied": 0,
        "restored": 0,
        "erased": 0,
        "skipped": 0,
        "missing": 0,
        "failed": [],
        "remaining": 0,
    }
    queued = db.card_actions(source=source, state=STATE_PENDING)
    erasing = db.card_actions(source=source, state=STATE_ERASE)
    if not queued and not erasing:
        return summary

    target = _target_for_source(db, source)
    if target is None:
        summary["failed"].append({"path": source, "error": "该卡片没有关联的相机相册"})
        summary["remaining"] = len(queued) + len(erasing)
        return summary
    if not target.usable:
        summary["failed"].append({"path": source, "error": _unusable_reason(target)})
        summary["remaining"] = len(queued) + len(erasing)
        return summary

    for action in erasing:
        if action.get("is_raw") and not include_raw:
            summary["skipped"] += 1
            continue
        try:
            summary["erased"] += _erase(
                target, action.get("card_path"), action["relative_path"], search_live=True
            )
            db.delete_card_action(action["id"])
        except (CardError, OSError) as exc:
            logger.warning("card erase failed for %s", action["relative_path"], exc_info=True)
            summary["failed"].append(
                {"path": action["relative_path"], "error": str(exc)}
            )

    for action in queued:
        relative_path = action["relative_path"]
        is_raw = bool(action.get("is_raw"))
        if is_raw and not include_raw:
            summary["skipped"] += 1
            continue
        try:
            if action["kind"] == KIND_DELETE:
                # A later restore of the same file wins over the queued delete.
                current = db.card_action_for(action["album"], relative_path)
                if current and current["kind"] == KIND_RESTORE:
                    db.delete_card_action(action["id"])
                    summary["skipped"] += 1
                    continue
                path = find_on_card(target, relative_path)
                if path is None:
                    # Already gone from the card; keeping the row would nag the
                    # user about a file that is not there any more.
                    db.delete_card_action(action["id"])
                    summary["missing"] += 1
                    continue
                card_path = trash_on_card(target, path, trash_dir_name)
                db.set_card_action_state(action["id"], STATE_APPLIED, card_path)
                summary["applied"] += 1
            else:
                card_path = action.get("card_path")
                if not card_path or not os.path.exists(card_path):
                    card_path = find_in_card_trash(target, relative_path)
                if not card_path:
                    db.delete_card_action(action["id"])
                    summary["missing"] += 1
                    continue
                restore_on_card(target, card_path, relative_path)
                db.delete_card_action(action["id"])
                summary["restored"] += 1
        except (CardError, OSError) as exc:
            logger.warning("card sync failed for %s", relative_path, exc_info=True)
            summary["failed"].append({"path": relative_path, "error": str(exc)})

    summary["remaining"] = len(db.card_actions(source=source, state=STATE_PENDING)) + len(
        db.card_actions(source=source, state=STATE_ERASE)
    )
    return summary


def forget_card(db: Database, folder: str, source: str | None = None) -> dict[str, int]:
    """Stop treating a folder as a camera album. Never deletes any file."""
    album = canonical_path(folder)
    removed = db.remove_card_album(album, canonical_path(source) if source else None)
    return {"pairings_removed": removed}
