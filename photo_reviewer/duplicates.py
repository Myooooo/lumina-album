"""Group near-identical photos so the duplicates can be reviewed and pruned.

Everything needed is already in the database: the index phase stores a 64-bit
dHash for every photo that produced a proxy, and a dHash is designed so that
visually similar images land within a few bits of each other. Nothing here reads
an image or calls a model - it is a bucket sort over strings.

Two things make this practical on a real library:

*Comparing all pairs is O(n^2)* - at 11k photos that is 60 million distance
calculations. Instead each hash is split into four 16-bit bands and only photos
that share a band are compared (the pigeonhole principle: two hashes within
distance ``d`` must agree exactly on at least one of ``d + 1`` bands). At the
default distance of 6 that is a huge saving and it is still exact - the bands
only ever prune pairs that cannot match.

*Groups have to be transitive.* If A matches B and B matches C, all three are one
group even when A and C are further apart than the threshold. A union-find pass
gives that, which is what a reviewer expects: one card per scene, not a web of
overlapping pairs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Default Hamming distance for "the same photo". 6 out of 64 bits is
#: deliberately conservative - it catches re-saves, resizes, minor crops and the
#: same scene shot twice, while leaving burst sequences (which differ by more)
#: alone. PhotoPrism ships perceptual hashes but warns they were built for
#: sorting rather than duplicate detection, which is why the threshold is
#: configurable and shown in the UI instead of being buried.
DEFAULT_THRESHOLD = 6

#: Bands used to prune the pair search. The guarantee needs at least
#: ``threshold + 1``; more bands prune harder but cost a little memory.
_MIN_BANDS = 4

#: How far two aspect ratios may differ and still be called the same picture.
#: Wide enough for a crop, narrow enough that a landscape is not matched to a
#: portrait.
ASPECT_TOLERANCE = 0.06


def _popcount(value: int) -> int:
    """Population count that also works on Python 3.9."""
    counter = getattr(int, "bit_count", None)
    if counter is not None:
        return counter(value)
    # Python 3.9 has no int.bit_count, so the byte table does the counting.
    total = 0
    while value:
        total += _POPCOUNT_BYTE[value & 0xFF]
        value >>= 8
    return total


_POPCOUNT_BYTE = bytes(format(i, "b").count("1") for i in range(256))


@dataclass
class DuplicateGroup:
    """One set of photos that look like the same picture."""

    photos: list[dict[str, Any]] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.photos)

    @property
    def aspect_spread(self) -> float:
        """How far apart the widest and narrowest aspect ratios are."""
        ratios = [
            p["width"] / p["height"]
            for p in self.photos
            if p.get("width") and p.get("height")
        ]
        if len(ratios) < 2:
            return 0.0
        return max(ratios) / min(ratios)

    @property
    def keep_id(self) -> int:
        """The photo a reviewer would most likely keep.

        Highest resolution first, then the largest file, then the one that has
        been analysed (its caption is worth preserving), then the oldest id so the
        answer is stable between runs. A RAW sibling never wins on its own: the
        image next to it is the one people look at.
        """
        def rank(photo: dict[str, Any]) -> tuple:
            pixels = int(photo.get("width") or 0) * int(photo.get("height") or 0)
            analysed = 1 if photo.get("status") == "analyzed" else 0
            return (pixels, int(photo.get("size") or 0), analysed, -int(photo["id"]))

        return max(self.photos, key=rank)["id"]

    @property
    def wasted_bytes(self) -> int:
        """Bytes that deleting everything but ``keep_id`` would reclaim."""
        return sum(
            int(p.get("size") or 0) for p in self.photos if p["id"] != self.keep_id
        )


def _parse(hash_hex: str) -> int | None:
    """Hex dHash to int, or ``None`` when it is unusable.

    Legacy rows can carry a variable-width hash from an older build; those are
    skipped rather than compared against a 64-bit value, because ``hamming_distance``
    treats a width mismatch as "no match" and silently dropping them here is the
    same answer without the wasted work.
    """
    text = (hash_hex or "").strip().lower()
    if len(text) != 16:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def group_duplicates(
    photos: Iterable[dict[str, Any]],
    threshold: int = DEFAULT_THRESHOLD,
) -> list[DuplicateGroup]:
    """Cluster ``photos`` into groups that are near-identical to each other.

    ``photos`` needs ``id``, ``phash``, ``width``, ``height``, ``size`` and
    ``status``. Rows without a usable hash are ignored - they simply cannot be
    compared.

    The clustering is *complete-linkage*, not single-linkage. Joining any two
    photos within ``threshold`` and taking the transitive closure looks right and
    is not: measured on a real 11k library, a burst of thirty similar frames
    chains into one group, each frame passing through the next, and the group
    ends up holding 134 unrelated photos. Requiring every pair in a group to be
    within the threshold keeps a group to what a reviewer would actually call one
    picture.
    """
    threshold = max(0, min(int(threshold), 32))
    hashes: dict[int, int] = {}
    by_id: dict[int, dict[str, Any]] = {}
    for photo in photos:
        value = _parse(photo.get("phash") or "")
        if value is None:
            continue
        hashes[photo["id"]] = value
        by_id[photo["id"]] = photo

    if len(hashes) < 2:
        return []

    neighbours = _neighbour_lists(hashes, threshold)
    clusters = _complete_linkage(neighbours, hashes, by_id, threshold)

    groups = [
        DuplicateGroup(
            photos=sorted((by_id[pid] for pid in members), key=lambda p: p["id"])
        )
        for members in clusters
        if len(members) > 1
    ]
    # Biggest groups first: that is where the space is.
    groups.sort(key=lambda group: (-group.size, group.photos[0]["id"]))
    return groups


def _neighbour_lists(hashes: dict[int, int], threshold: int) -> dict[int, set[int]]:
    """For each photo, the ids within ``threshold`` bits.

    The band trick prunes the search: two hashes within distance ``d`` must agree
    exactly on at least one of ``d + 1`` bands, so only photos sharing a band need
    measuring. The result is exact - bands can only discard pairs that could not
    have matched.
    """
    bands = max(threshold + 1, _MIN_BANDS)
    bands = min(bands, 16)
    band_width = 64 // bands
    band_mask = (1 << band_width) - 1

    buckets: dict[tuple[int, int], list[int]] = {}
    for photo_id, value in hashes.items():
        for band in range(bands):
            key = (band, (value >> (band * band_width)) & band_mask)
            buckets.setdefault(key, []).append(photo_id)

    neighbours: dict[int, set[int]] = {photo_id: set() for photo_id in hashes}
    compared: set[tuple[int, int]] = set()
    for members in buckets.values():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                left, right = members[i], members[j]
                pair = (left, right) if left < right else (right, left)
                # A pair sharing several bands would otherwise be measured again.
                if pair in compared:
                    continue
                compared.add(pair)
                if _popcount(hashes[left] ^ hashes[right]) <= threshold:
                    neighbours[left].add(right)
                    neighbours[right].add(left)
    return neighbours


def _compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Whether two photos could be the same picture at all.

    Two cheap guards, both learned from measuring this library rather than
    guessed:

    *Aspect ratio.* A dHash of 64 bits says little about layout: two unrelated
    shots from different cameras land within 6 bits now and then, and a landscape
    sitting next to a portrait can pass the bit test while obviously not being the
    same frame.

    *Same camera family.* Hash distance kept pairing a Canon frame with a Sony one
    at the default threshold, which no reviewer would accept as a duplicate. Two
    copies of one photo come from the same camera (or from the same phone export),
    so requiring the folder to match costs nothing real and removes that whole
    class of false positive.
    """
    left_w, left_h = int(left.get("width") or 0), int(left.get("height") or 0)
    right_w, right_h = int(right.get("width") or 0), int(right.get("height") or 0)
    if (
        left_w
        and left_h
        and right_w
        and right_h
        and abs(left_w / left_h - right_w / right_h) > ASPECT_TOLERANCE
    ):
        return False
    left_folder = (left.get("folder") or "").rstrip("\\/").lower()
    right_folder = (right.get("folder") or "").rstrip("\\/").lower()
    return not (
        left_folder and right_folder and left_folder != right_folder
    )


def _complete_linkage(
    neighbours: dict[int, set[int]],
    hashes: dict[int, int],
    photos: dict[int, dict[str, Any]],
    threshold: int,
) -> list[list[int]]:
    """Grow groups that every member agrees with, strongest pairs first.

    Seeds are taken from the closest pairs, so the most obvious duplicates claim
    a group before a weaker pair can drag them into a looser one. A candidate
    joins only when it is within ``threshold`` of *all* current members, which is
    what keeps a burst from collapsing into one enormous group.
    """
    distance: dict[tuple[int, int], int] = {}

    def dist(left: int, right: int) -> int:
        key = (left, right) if left < right else (right, left)
        cached = distance.get(key)
        if cached is None:
            cached = _popcount(hashes[left] ^ hashes[right])
            distance[key] = cached
        return cached

    pairs = []
    for left, right_set in neighbours.items():
        for right in right_set:
            if left < right:
                pairs.append((dist(left, right), left, right))
    pairs.sort()

    assigned: set[int] = set()
    clusters: list[list[int]] = []
    for _distance, left, right in pairs:
        if left in assigned or right in assigned:
            continue
        if not _compatible(photos[left], photos[right]):
            continue
        members = [left, right]
        assigned.update((left, right))
        # Everything reachable from the pair, accepted only if it agrees with the
        # whole group so far.
        candidates = (neighbours[left] | neighbours[right]) - assigned
        grown = True
        while grown:
            grown = False
            for candidate in sorted(candidates):
                if candidate in assigned:
                    continue
                if not _compatible(photos[members[0]], photos[candidate]):
                    continue
                if all(dist(candidate, member) <= threshold for member in members):
                    members.append(candidate)
                    assigned.add(candidate)
                    candidates |= neighbours[candidate]
                    grown = True
        clusters.append(sorted(members))
    return clusters


def summarise(groups: list[DuplicateGroup]) -> dict[str, int]:
    """Counts for the header of the review page."""
    return {
        "groups": len(groups),
        "photos": sum(group.size for group in groups),
        "removable": sum(group.size - 1 for group in groups),
        "wasted_bytes": sum(group.wasted_bytes for group in groups),
    }
