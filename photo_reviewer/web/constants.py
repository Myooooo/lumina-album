"""Limits and constants shared by the HTTP routes and their helpers."""

from __future__ import annotations

#: How many recently used folders the UI state keeps.
UI_STATE_HISTORY_LIMIT = 20

#: Methods that mutate state and therefore require a same-origin request.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: One year, in seconds: how long a content-addressed thumbnail is cacheable.
_IMMUTABLE_IMAGE_MAX_AGE = 31536000

#: Ceiling on one export. Every picture is embedded inline or stacked into one
#: canvas, so the file grows with the selection and a thousand photos would build a
#: document nobody can open. A keepsake is a day or a small album, not the library.
MAX_EXPORT_PHOTOS = 300

#: How many photos one visual search may show the model, and how many per request.
#: Each image is a real request, so this is a cost ceiling as much as a quality
#: one - the caller is expected to have narrowed the list with the filters first.
VISUAL_SEARCH_MAX = 24
VISUAL_SEARCH_BATCH = 4

#: The search strategies offered to the user: ``visual`` looks at the pixels, the
#: other one reads the index.
#:
#: Two, not four. `keyword` only matched a word that happened to be in a caption
#: (0 results for 4 of 5 ordinary queries), and `semantic` was strictly dominated
#: by `smart` - same model call, no local recall, and a hard 502 instead of a
#: graceful fallback. `visual` stays because it answers a different question by
#: different means.
SEARCH_MODES = ("smart", "keyword", "visual")

#: How long to wait for the model's `/models` before deciding it is down. Short on
#: purpose: this sits between a keypress and a spinner, and a refused connection is
#: the common case.
SEARCH_PROBE_TIMEOUT = 1.0

#: Modes that are no longer offered but are still accepted, so a bookmarked request
#: or an old cached page does not fail with "不支持的搜索方式". They are mapped onto
#: the mode that supersedes them.
_LEGACY_SEARCH_MODES = {"semantic": "smart", "vector": "smart"}
