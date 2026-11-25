"""SQLite storage for photo metadata and analysis results."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .paths import canonical_path

logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_CAPTURE_FORMATS = (
    "%Y:%m:%d %H:%M:%S",
    "%Y:%m:%d %H:%M",
    "%Y:%m:%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
)


def parse_capture_datetime(value: Any) -> datetime | None:
    """Parse the common EXIF capture-time formats into a naive datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("\x00", "").strip()
    if not text:
        return None
    for fmt in _CAPTURE_FORMATS:
        try:
            return datetime.strptime(text, fmt)  # noqa: DTZ007 - EXIF timestamps have no timezone
        except ValueError:
            continue
    # ISO-ish formats with timezone offsets or T separators.
    normalized = text.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M%z"):
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=None)  # noqa: DTZ007
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def photo_capture_time(photo: dict[str, Any]) -> datetime | None:
    """Return the capture time for a photo payload/row."""
    if photo.get("captured_at"):
        parsed = parse_capture_datetime(photo.get("captured_at"))
        if parsed is not None:
            return parsed
    exif = photo.get("exif") or {}
    if not isinstance(exif, dict):
        exif = {}
    return parse_capture_datetime(
        exif.get("datetime_original") or exif.get("DateTimeOriginal")
    )


def photo_capture_year(photo: dict[str, Any]) -> int | None:
    """Return the 4-digit capture year for a photo payload/row."""
    if photo.get("capture_year"):
        try:
            return int(photo["capture_year"])
        except (TypeError, ValueError):
            pass
    value = photo_capture_time(photo)
    return value.year if value else None


def _extract_capture_fields(exif: Any) -> tuple[str | None, int | None]:
    """Extract materialized ``(captured_at, capture_year)`` from an EXIF dict or JSON string."""
    data = exif
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            data = {}
    if not isinstance(data, dict):
        return None, None
    dt = parse_capture_datetime(
        data.get("datetime_original") or data.get("DateTimeOriginal")
    )
    if dt is None:
        return None, None
    return dt.strftime("%Y-%m-%d %H:%M:%S"), int(dt.year)


def _time_sort_key(photo: dict[str, Any], descending: bool):
    """Photos without capture time always sort after photos that have one."""
    captured = photo_capture_time(photo)
    if captured is None:
        return (1, 0.0)
    timestamp = captured.timestamp()
    return (0, -timestamp) if descending else (0, timestamp)


def sort_photos(photos: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """Sort photo dicts in place using the same sort names as the UI.

    Score sorts break ties on capture time, matching ``list_photos``: a score
    alone left equal-scoring photos in arbitrary order, which read as a shuffled
    shoot.
    """
    if sort == "score_desc":
        photos.sort(
            key=lambda p: (
                p.get("score") is None,
                -(p.get("score") or 0),
                _time_sort_key(p, descending=True),
            )
        )
    elif sort == "score_asc":
        photos.sort(
            key=lambda p: (
                p.get("score") is None,
                p.get("score") or 0,
                _time_sort_key(p, descending=True),
            )
        )
    elif sort == "filename":
        photos.sort(
            key=lambda p: (
                str(p.get("filename") or "").lower(),
                _time_sort_key(p, descending=True),
            )
        )
    elif sort == "recent":
        photos.sort(key=lambda p: p.get("analyzed_at") or "", reverse=True)
    elif sort == "size":
        photos.sort(
            key=lambda p: (
                -(p.get("size") or 0),
                _time_sort_key(p, descending=True),
            )
        )
    elif sort == "time_asc":
        photos.sort(key=lambda p: _time_sort_key(p, descending=False))
    elif sort == "time_desc":
        photos.sort(key=lambda p: _time_sort_key(p, descending=True))
    return photos


# ---------------------------------------------------------------------------
# Capture-time SQL expressions
#
# ``exif`` is stored as JSON text. Reading it through ``json_extract`` in
# SQLite keeps sorting and filtering inside the database, which avoids pulling
# every row into Python (the previous implementation was ~10x slower and used
# memory proportional to the whole library).
# ---------------------------------------------------------------------------

_RAW_CAPTURE_EXPR = (
    "COALESCE("
    "json_extract(exif, '$.datetime_original'),"
    "json_extract(exif, '$.DateTimeOriginal')"
    ")"
)


def _capture_time_sql_expr() -> str:
    """SQL expression yielding a sortable ``YYYY-MM-DD HH:MM:SS`` string.

    EXIF writes ``2023:01:02 03:04:05``; the date part is normalised to ISO so
    plain string comparison orders correctly. Rows without a usable timestamp
    yield NULL and are sorted last by the caller.
    """
    return (
        f"CASE WHEN {_RAW_CAPTURE_EXPR} IS NULL OR {_RAW_CAPTURE_EXPR} = '' "
        f"THEN NULL ELSE replace(substr({_RAW_CAPTURE_EXPR}, 1, 10), ':', '-') "
        f"|| substr({_RAW_CAPTURE_EXPR}, 11) END"
    )


def _capture_year_sql_expr() -> str:
    """SQL expression yielding the 4-digit capture year, or NULL."""
    return (
        f"CASE WHEN {_RAW_CAPTURE_EXPR} IS NULL "
        f"OR length({_RAW_CAPTURE_EXPR}) < 4 THEN NULL "
        f"ELSE substr({_RAW_CAPTURE_EXPR}, 1, 4) END"
    )


class _CachedConnection(sqlite3.Connection):
    """A ``sqlite3.Connection`` whose ``close()`` returns it to the cache.

    Every database method hands a connection back in a ``finally: conn.close()``
    block. Neutering ``close`` keeps those call sites correct while the
    connection stays open for the next call on the same thread; polling a
    photo folder reuses one connection instead of paying ~0.7 ms per query.

    An uncommitted transaction is rolled back, which is what the old
    short-lived connection did implicitly when it was closed after an error.
    ``Database.close()`` performs the real teardown.
    """

    def close(self) -> None:
        if self.in_transaction:
            try:
                self.rollback()
            except sqlite3.Error:
                logger.debug("rollback on release failed", exc_info=True)


class Database:
    """Thin SQLite wrapper.

    Connections are cached per thread: opening one costs ~0.7 ms while reusing
    it costs ~0.006 ms, and a single photo analysis performs several round
    trips. ``self._lock`` serializes writers, which matters because scan
    workers and Flask request threads share this instance.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def __enter__(self) -> Database:  # noqa: PYI034 - Self needs typing_extensions
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, factory=_CachedConnection)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            with self._connections_lock:
                self._connections.append(conn)
        return conn

    def close(self) -> None:
        """Close every cached connection (tests and shutdown paths).

        Background repairs share this instance, so they are waited for first:
        closing the connections underneath a running pass would raise
        ``sqlite3.OperationalError`` in the repair thread and leave rows
        half-fixed.
        """
        from .repair import wait_for_repairs

        wait_for_repairs(timeout=60)
        with self._connections_lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            try:
                sqlite3.Connection.close(conn)
            except sqlite3.Error:
                logger.debug("connection already closed", exc_info=True)
        self._local = threading.local()

    def init(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS photos (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        path TEXT NOT NULL UNIQUE,
                        original_path TEXT,
                        folder TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        size INTEGER DEFAULT 0,
                        width INTEGER,
                        height INTEGER,
                        thumb_path TEXT,
                        proxy_path TEXT,
                        dimensions TEXT,
                        location TEXT,
                        exif TEXT,
                        phash TEXT,
                        score REAL,
                        tags TEXT,
                        reason TEXT,
                        title TEXT,
                        raw_path TEXT,
                        model TEXT,
                        analyzed_at TEXT,
                        status TEXT NOT NULL DEFAULT 'pending',
                        favorite INTEGER DEFAULT 0,
                        geocoded INTEGER DEFAULT 0,
                        error TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_photos_folder ON photos(folder);
                    CREATE INDEX IF NOT EXISTS idx_photos_status ON photos(status);
                    CREATE INDEX IF NOT EXISTS idx_photos_score ON photos(score);
                    CREATE INDEX IF NOT EXISTS idx_photos_path ON photos(path);
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS app_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS ui_state (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS card_albums (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        folder TEXT NOT NULL,
                        source TEXT NOT NULL,
                        volume INTEGER,
                        volume_key TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(folder, source)
                    );
                    CREATE INDEX IF NOT EXISTS idx_card_albums_folder
                        ON card_albums(folder);
                    CREATE TABLE IF NOT EXISTS card_actions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        album TEXT NOT NULL,
                        source TEXT NOT NULL,
                        volume INTEGER,
                        kind TEXT NOT NULL,
                        state TEXT NOT NULL,
                        relative_path TEXT NOT NULL,
                        card_path TEXT,
                        filename TEXT NOT NULL,
                        size INTEGER DEFAULT 0,
                        is_raw INTEGER DEFAULT 0,
                        photo_id INTEGER,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(album, relative_path)
                    );
                    CREATE INDEX IF NOT EXISTS idx_card_actions_source
                        ON card_actions(source, state);
                    """
                )
                conn.commit()
                # Migration for databases created by older versions.
                cols = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(photos)").fetchall()
                }
                if "proxy_path" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN proxy_path TEXT")
                if "dimensions" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN dimensions TEXT")
                if "location" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN location TEXT")
                if "exif" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN exif TEXT")
                if "favorite" not in cols:
                    conn.execute(
                        "ALTER TABLE photos ADD COLUMN favorite INTEGER DEFAULT 0"
                    )
                if "title" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN title TEXT")
                if "raw_path" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN raw_path TEXT")
                if "geocoded" not in cols:
                    conn.execute(
                        "ALTER TABLE photos ADD COLUMN geocoded INTEGER DEFAULT 0"
                    )
                # Video support. ``media_kind`` is 'photo' for everything that
                # existed before this migration, so no row needs rewriting.
                if "media_kind" not in cols:
                    conn.execute(
                        "ALTER TABLE photos ADD COLUMN media_kind TEXT DEFAULT 'photo'"
                    )
                if "duration_ms" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN duration_ms INTEGER")
                if "video_info" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN video_info TEXT")
                if "clip_path" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN clip_path TEXT")
                if "play_path" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN play_path TEXT")
                if "play_state" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN play_state TEXT")
                if "live_path" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN live_path TEXT")
                # Card identity. ``volume_key`` is the platform-independent
                # identifier (a Windows volume GUID or a filesystem UUID); the
                # integer ``volume`` column is what older builds stored and is
                # kept so an existing pairing still has something to check.
                card_cols = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(card_albums)").fetchall()
                }
                if "volume_key" not in card_cols:
                    conn.execute("ALTER TABLE card_albums ADD COLUMN volume_key TEXT")
                if "faces_scanned_model" not in cols:
                    conn.execute(
                        "ALTER TABLE photos ADD COLUMN faces_scanned_model TEXT"
                    )
                if "place_id" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN place_id INTEGER")
                if "lat" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN lat REAL")
                if "lon" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN lon REAL")
                if "location_source" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN location_source TEXT")
                added_capture_cols = False
                if "captured_at" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN captured_at TEXT")
                    added_capture_cols = True
                if "capture_year" not in cols:
                    conn.execute("ALTER TABLE photos ADD COLUMN capture_year INTEGER")
                    added_capture_cols = True
                if added_capture_cols:
                    cap_expr = _capture_time_sql_expr()
                    yr_expr = _capture_year_sql_expr()
                    conn.execute(
                        f"UPDATE photos SET captured_at = {cap_expr}, "
                        f"capture_year = CASE WHEN {yr_expr} GLOB '[0-9][0-9][0-9][0-9]' "
                        f"THEN CAST({yr_expr} AS INTEGER) ELSE NULL END "
                        f"WHERE exif IS NOT NULL AND exif != '' AND captured_at IS NULL"
                    )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_photos_place_id ON photos(place_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_photos_media_kind "
                    "ON photos(media_kind)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_photos_folder_status_score "
                    "ON photos(folder, status, score)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_photos_folder_status_fav "
                    "ON photos(folder, status, favorite)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_photos_folder_captured "
                    "ON photos(folder, status, captured_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_photos_folder_year "
                    "ON photos(folder, status, capture_year)"
                )
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS people (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL DEFAULT '',
                        kind TEXT NOT NULL DEFAULT 'person',
                        cover_face_id INTEGER,
                        is_hidden INTEGER NOT NULL DEFAULT 0,
                        is_favorite INTEGER NOT NULL DEFAULT 0,
                        centroid BLOB,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_people_kind ON people(kind);
                    CREATE TABLE IF NOT EXISTS faces (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                        person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
                        entity_kind TEXT NOT NULL DEFAULT 'person',
                        bbox_x REAL NOT NULL,
                        bbox_y REAL NOT NULL,
                        bbox_w REAL NOT NULL,
                        bbox_h REAL NOT NULL,
                        det_score REAL NOT NULL DEFAULT 0.0,
                        embedding BLOB,
                        model_name TEXT NOT NULL DEFAULT 'buffalo_l',
                        status TEXT NOT NULL DEFAULT 'auto',
                        thumb_path TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_faces_photo ON faces(photo_id);
                    CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);
                    CREATE INDEX IF NOT EXISTS idx_faces_status ON faces(status);
                    CREATE TABLE IF NOT EXISTS places (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        city TEXT NOT NULL DEFAULT '',
                        address TEXT NOT NULL DEFAULT '',
                        lat REAL,
                        lon REAL,
                        source TEXT NOT NULL DEFAULT 'manual',
                        cover_photo_id INTEGER,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_places_name ON places(name);
                    CREATE TABLE IF NOT EXISTS photo_embeddings (
                        photo_id INTEGER NOT NULL,
                        model TEXT NOT NULL,
                        dim INTEGER NOT NULL,
                        embedding BLOB NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (photo_id, model)
                    );
                    CREATE INDEX IF NOT EXISTS idx_photo_embeddings_model ON photo_embeddings(model);
                    CREATE TABLE IF NOT EXISTS smart_albums (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        rules TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def upsert_photo(self, record: dict[str, Any]) -> int:
        """Insert a photo row, or update it if the path already exists."""
        now = utc_now()
        record.setdefault("created_at", now)
        captured_at, capture_year = _extract_capture_fields(record.get("exif"))
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO photos
                        (path, original_path, folder, filename, size, width, height, thumb_path,
                         proxy_path, dimensions, location, exif, captured_at, capture_year,
                         phash, score, tags, reason, title, raw_path, model,
                         analyzed_at, status, favorite, geocoded, error, created_at,
                         media_kind, duration_ms, video_info, clip_path, play_path, play_state, live_path)
                    VALUES
                        (:path, :original_path, :folder, :filename, :size, :width, :height, :thumb_path,
                         :proxy_path, :dimensions, :location, :exif, :captured_at, :capture_year,
                         :phash, :score, :tags, :reason, :title, :raw_path, :model,
                         :analyzed_at, :status, :favorite, :geocoded, :error, :created_at,
                         :media_kind, :duration_ms, :video_info, :clip_path, :play_path, :play_state, :live_path)
                    ON CONFLICT(path) DO UPDATE SET
                        original_path=excluded.original_path,
                        folder=excluded.folder,
                        filename=excluded.filename,
                        size=excluded.size,
                        width=excluded.width,
                        height=excluded.height,
                        thumb_path=excluded.thumb_path,
                        proxy_path=excluded.proxy_path,
                        dimensions=excluded.dimensions,
                        location=CASE
                            WHEN photos.location_source = 'manual' THEN photos.location
                            ELSE excluded.location
                        END,
                        exif=excluded.exif,
                        captured_at=excluded.captured_at,
                        capture_year=excluded.capture_year,
                        phash=excluded.phash,
                        score=excluded.score,
                        tags=excluded.tags,
                        reason=excluded.reason,
                        title=excluded.title,
                        raw_path=excluded.raw_path,
                        model=excluded.model,
                        analyzed_at=excluded.analyzed_at,
                        status=excluded.status,
                        favorite=excluded.favorite,
                        error=excluded.error,
                        created_at=excluded.created_at,
                        media_kind=excluded.media_kind,
                        duration_ms=excluded.duration_ms,
                        video_info=excluded.video_info,
                        clip_path=excluded.clip_path,
                        play_path=excluded.play_path,
                        play_state=excluded.play_state,
                        live_path=excluded.live_path
                    """,
                    {
                        "path": record["path"],
                        "original_path": record.get("original_path"),
                        "folder": record.get("folder", ""),
                        "filename": record.get("filename", Path(record["path"]).name),
                        "size": record.get("size", 0),
                        "width": record.get("width"),
                        "height": record.get("height"),
                        "thumb_path": record.get("thumb_path"),
                        "proxy_path": record.get("proxy_path"),
                        "dimensions": json.dumps(
                            record.get("dimensions") or {}, ensure_ascii=False
                        )
                        if isinstance(record.get("dimensions"), dict)
                        else record.get("dimensions"),
                        "location": record.get("location"),
                        "exif": json.dumps(record.get("exif") or {}, ensure_ascii=False)
                        if isinstance(record.get("exif"), dict)
                        else record.get("exif"),
                        "captured_at": captured_at,
                        "capture_year": capture_year,
                        "phash": record.get("phash"),
                        "score": record.get("score"),
                        "tags": json.dumps(record.get("tags") or [], ensure_ascii=False)
                        if isinstance(record.get("tags"), (list, tuple, dict))
                        else record.get("tags"),
                        "reason": record.get("reason"),
                        "title": record.get("title"),
                        "raw_path": record.get("raw_path"),
                        "model": record.get("model"),
                        "analyzed_at": record.get("analyzed_at"),
                        "status": record.get("status", "pending"),
                        "favorite": record.get("favorite", 0),
                        "geocoded": 1 if record.get("geocoded") else 0,
                        "error": record.get("error"),
                        "created_at": record.get("created_at", now),
                        "media_kind": record.get("media_kind") or "photo",
                        "duration_ms": record.get("duration_ms"),
                        "video_info": json.dumps(
                            record.get("video_info") or {}, ensure_ascii=False
                        )
                        if isinstance(record.get("video_info"), dict)
                        else record.get("video_info"),
                        "clip_path": record.get("clip_path"),
                        "play_path": record.get("play_path"),
                        "play_state": record.get("play_state"),
                        "live_path": record.get("live_path"),
                    },
                )
                conn.commit()
                row = conn.execute(
                    "SELECT id FROM photos WHERE path=?", (record["path"],)
                ).fetchone()
                return int(row["id"]) if row else 0
            finally:
                conn.close()

    def get_photo(self, photo_id: int) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM photos WHERE id=?", (photo_id,)
                ).fetchone()
                return self._row_to_dict(row) if row else None
            finally:
                conn.close()

    def get_photo_by_path(self, path: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM photos WHERE path=?", (path,)
                ).fetchone()
                return self._row_to_dict(row) if row else None
            finally:
                conn.close()

    def list_photos(
        self,
        folder: str | None = None,
        status: str | None = None,
        min_score: float | None = None,
        max_score: float | None = None,
        search: str | None = None,
        tag: str | None = None,
        favorite: bool | None = None,
        year: int | None = None,
        exclude_deleted: bool = False,
        sort: str = "score_asc",
        limit: int = 1000,
        offset: int = 0,
        kind: str | None = None,
        exclude_folders: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if folder:
            clauses.append("folder = ?")
            params.append(folder)
        elif exclude_folders:
            cleaned_ex = list({v for f in exclude_folders if f for v in (str(f), canonical_path(f)) if v})
            if cleaned_ex:
                placeholders = ",".join("?" for _ in cleaned_ex)
                clauses.append(f"folder NOT IN ({placeholders})")
                params.extend(cleaned_ex)
        if status == "pending":
            clauses.append("status IN ('pending','error')")
        elif status:
            clauses.append("status = ?")
            params.append(status)
        if exclude_deleted:
            clauses.append("status != 'deleted'")
        if kind == "video":
            clauses.append("COALESCE(media_kind, 'photo') = 'video'")
        elif kind == "photo":
            clauses.append("(media_kind IS NULL OR media_kind != 'video')")
        elif kind == "live":
            clauses.append(
                "(media_kind IS NULL OR media_kind != 'video') "
                "AND live_path IS NOT NULL AND live_path != ''"
            )
        if min_score is not None:
            clauses.append("score >= ?")
            params.append(min_score)
        if max_score is not None:
            clauses.append("score <= ?")
            params.append(max_score)
        if search:
            clauses.append("(filename LIKE ? OR title LIKE ? OR reason LIKE ? OR tags LIKE ? OR location LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like, like, like])
        if tag:
            # tags is stored as a JSON array, e.g. ["风景","模糊"].
            # Match the JSON string form of the tag to avoid partial word matches.
            clauses.append("tags LIKE ?")
            params.append(f"%{json.dumps(tag, ensure_ascii=False)}%")
        if favorite is not None:
            clauses.append("favorite = ?")
            params.append(1 if favorite else 0)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        # Capture time lives inside the ``exif`` JSON text, either as an EXIF
        # timestamp with a separator (``2023:01:02 03:04:05``) or as an ISO
        # string. Both sorting and year filtering are pushed into SQLite as
        # expressions on that column, so LIMIT/OFFSET still apply and the whole
        # library never has to be materialised in Python.
        if year is not None:
            clauses.append("capture_year = ?")
            params.append(int(year))
            where = "WHERE " + " AND ".join(clauses)
        if sort in ("time_asc", "time_desc"):
            direction = "ASC" if sort == "time_asc" else "DESC"
            order = f"(captured_at IS NULL) ASC, captured_at {direction}"
            sql = f"SELECT * FROM photos {where} ORDER BY {order} LIMIT ? OFFSET ?"
            return self._query_photos(sql, params + [limit, offset])

        # A score on its own is not a tie-breaker. A folder where many photos
        # scored 8.0 came back in whatever order SQLite happened to store them,
        # which is why a shoot sometimes looked shuffled. Capture time settles
        # the tie, so equal scores read chronologically.
        by_time = "(captured_at IS NULL) ASC, captured_at DESC"
        sort_map = {
            "score_asc": f"(score IS NULL) ASC, score ASC, {by_time}",
            "score_desc": f"(score IS NULL) ASC, score DESC, {by_time}",
            "filename": f"filename COLLATE NOCASE ASC, {by_time}",
            "recent": "analyzed_at DESC",
            "size": f"size DESC, {by_time}",
        }
        order = sort_map.get(sort, sort_map["score_asc"])
        sql = f"SELECT * FROM photos {where} ORDER BY {order} LIMIT ? OFFSET ?"
        return self._query_photos(sql, params + [limit, offset])

    def _query_photos(self, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def stats(
        self,
        folder: str | None = None,
        exclude_folders: Sequence[str] | None = None,
    ) -> dict[str, int]:
        clauses = []
        params: list[Any] = []
        if folder:
            clauses.append("folder = ?")
            params.append(folder)
        elif exclude_folders:
            cleaned_ex = list({v for f in exclude_folders if f for v in (str(f), canonical_path(f)) if v})
            if cleaned_ex:
                placeholders = ",".join("?" for _ in cleaned_ex)
                clauses.append(f"folder NOT IN ({placeholders})")
                params.extend(cleaned_ex)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        and_clause = " AND " if where else " WHERE "
        active_clause = (
            f"{where}{and_clause}status != 'deleted'"
            if where
            else "WHERE status != 'deleted'"
        )
        with self._lock:
            conn = self._connect()
            try:
                total = conn.execute(
                    f"SELECT COUNT(*) c FROM photos {active_clause}", params
                ).fetchone()["c"]
                analyzed = conn.execute(
                    f"SELECT COUNT(*) c FROM photos {active_clause} AND status='analyzed'",
                    params,
                ).fetchone()["c"]
                favorite = conn.execute(
                    f"SELECT COUNT(*) c FROM photos {active_clause} AND favorite=1",
                    params,
                ).fetchone()["c"]
                deleted = conn.execute(
                    f"SELECT COUNT(*) c FROM photos {where}{and_clause}status='deleted'",
                    params,
                ).fetchone()["c"]
                return {
                    "total": int(total),
                    "analyzed": int(analyzed),
                    "pending": int(total - analyzed),
                    "favorite": int(favorite),
                    "deleted": int(deleted),
                }
            finally:
                conn.close()

    def update_analysis(
        self,
        photo_id: int,
        score: float,
        tags: list[str],
        reason: str,
        model: str,
        title: str | None = None,
        phash: str | None = None,
        width: int | None = None,
        height: int | None = None,
        thumb_path: str | None = None,
        proxy_path: str | None = None,
        dimensions: dict[str, float] | None = None,
        location: str | None = None,
        exif: dict[str, Any] | None = None,
        status: str = "analyzed",
        error: str | None = None,
    ) -> None:
        captured_at, capture_year = (
            _extract_capture_fields(exif) if isinstance(exif, dict) else (None, None)
        )
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    UPDATE photos SET
                        score=?, tags=?, reason=?, model=?,
                        title=COALESCE(NULLIF(?, ''), title),
                        phash=COALESCE(?, phash), width=COALESCE(?, width),
                        height=COALESCE(?, height), thumb_path=COALESCE(?, thumb_path),
                        proxy_path=COALESCE(?, proxy_path),
                        dimensions=COALESCE(?, dimensions),
                        location=CASE
                            WHEN location_source = 'manual' THEN location
                            ELSE COALESCE(?, location)
                        END,
                        exif=COALESCE(?, exif),
                        captured_at=COALESCE(?, captured_at),
                        capture_year=COALESCE(?, capture_year),
                        status=?, error=?, analyzed_at=?
                    WHERE id=?
                    """,
                    (
                        score,
                        json.dumps(tags, ensure_ascii=False),
                        reason,
                        model,
                        title or "",
                        phash,
                        width,
                        height,
                        thumb_path,
                        proxy_path,
                        json.dumps(dimensions, ensure_ascii=False)
                        if isinstance(dimensions, dict)
                        else None,
                        location,
                        json.dumps(exif, ensure_ascii=False)
                        if isinstance(exif, dict)
                        else None,
                        captured_at,
                        capture_year,
                        status,
                        error,
                        utc_now(),
                        photo_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def update_metadata(
        self,
        photo_id: int,
        score: float,
        dimensions: dict[str, float],
        tags: list[str],
        reason: str,
        title: str,
        location: str | None,
    ) -> None:
        """Update user-editable analysis metadata without touching the file."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT location, place_id, location_source FROM photos WHERE id=?",
                    (photo_id,),
                ).fetchone()
                old_loc = (row["location"] or "").strip() if row else ""
                new_loc = (location or "").strip()
                if old_loc != new_loc:
                    new_place_id = None
                    new_loc_source = "manual" if new_loc else None
                else:
                    new_place_id = row["place_id"] if row else None
                    new_loc_source = row["location_source"] if row else None
                conn.execute(
                    """
                    UPDATE photos SET
                        score=?, dimensions=?, tags=?, reason=?, title=?, location=?,
                        place_id=?, location_source=?, analyzed_at=?
                    WHERE id=?
                    """,
                    (
                        score,
                        json.dumps(dimensions, ensure_ascii=False),
                        json.dumps(tags, ensure_ascii=False),
                        reason,
                        title,
                        new_loc or None,
                        new_place_id,
                        new_loc_source,
                        utc_now(),
                        photo_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def remove_folder(self, folder: str) -> int:
        """Delete every database row that belongs to one photo folder."""
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute("DELETE FROM photos WHERE folder=?", (folder,))
                conn.commit()
                return max(0, cursor.rowcount)
            finally:
                conn.close()

    def update_exif(
        self, photo_id: int, location: str | None, exif: dict[str, Any] | None
    ) -> None:
        captured_at, capture_year = _extract_capture_fields(exif)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE photos SET "
                    "location=CASE WHEN location_source='manual' THEN location ELSE ? END, "
                    "exif=?, captured_at=?, capture_year=? WHERE id=?",
                    (
                        location,
                        json.dumps(exif or {}, ensure_ascii=False)
                        if isinstance(exif, dict)
                        else None,
                        captured_at,
                        capture_year,
                        photo_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def update_raw_path(self, photo_id: int, raw_path: str | None) -> None:
        """Attach (or clear) the same-named raw sibling of one photo."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE photos SET raw_path=? WHERE id=?", (raw_path, photo_id)
                )
                conn.commit()
            finally:
                conn.close()

    def reset_analysis_for_paths(self, paths: Sequence[str]) -> int:
        """Clear model results for files whose contents were replaced.

        A re-imported photo is a different picture, so keeping its old score,
        tags and comment would be wrong. The row itself (and any favourite
        mark) is kept and pushed back to ``pending`` so the next organise run
        picks it up again.
        """
        if not paths:
            return 0
        cleared = 0
        with self._lock:
            conn = self._connect()
            try:
                for path in paths:
                    cursor = conn.execute(
                        """
                        UPDATE photos SET
                            status='pending', score=NULL, dimensions=NULL, tags=NULL,
                            reason=NULL, title=NULL,
                            analyzed_at=NULL, error=NULL
                        WHERE path=? AND status != 'deleted'
                        """,
                        (path,),
                    )
                    cleared += max(0, cursor.rowcount)
                conn.commit()
            finally:
                conn.close()
        return cleared

    def adopt_analysis(self, source_id: int, target_id: int) -> None:
        """Move a model result from one row to another.

        Used when a raw-only row (which the model did analyse) is folded into
        the image row that just appeared next to it: the score was paid for, so
        it moves across instead of being thrown away.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    UPDATE photos SET
                        score=(SELECT score FROM photos WHERE id=?),
                        dimensions=(SELECT dimensions FROM photos WHERE id=?),
                        tags=(SELECT tags FROM photos WHERE id=?),
                        reason=(SELECT reason FROM photos WHERE id=?),
                        title=COALESCE(NULLIF((SELECT title FROM photos WHERE id=?), ''), title),
                        model=(SELECT model FROM photos WHERE id=?),
                        analyzed_at=(SELECT analyzed_at FROM photos WHERE id=?),
                        status=CASE
                            WHEN (SELECT score FROM photos WHERE id=?) IS NOT NULL
                            THEN 'analyzed' ELSE status END
                    WHERE id=?
                    """,
                    (source_id,) * 8 + (target_id,),
                )
                conn.commit()
            finally:
                conn.close()

    def mark_deleted(
        self, ids: Sequence[int], new_paths: dict[int, str] | None = None
    ) -> None:
        if not ids:
            return
        new_paths = new_paths or {}
        with self._lock:
            conn = self._connect()
            try:
                for pid in ids:
                    path = new_paths.get(pid)
                    if path:
                        conn.execute(
                            "UPDATE photos SET status='deleted', path=?, original_path=COALESCE(original_path, path), error=NULL WHERE id=?",
                            (path, pid),
                        )
                    else:
                        conn.execute(
                            "UPDATE photos SET status='deleted', error=NULL WHERE id=?",
                            (pid,),
                        )
                conn.commit()
            finally:
                conn.close()

    def restore_deleted(
        self, ids: Sequence[int], original_paths: dict[int, str]
    ) -> None:
        if not ids:
            return
        with self._lock:
            conn = self._connect()
            try:
                for pid in ids:
                    orig = original_paths.get(pid) or self._get_original_path(pid)
                    # Photos that were never analyzed by the model return to
                    # "pending" (待整理); analyzed ones return to "analyzed".
                    row = conn.execute(
                        "SELECT analyzed_at FROM photos WHERE id=?", (pid,)
                    ).fetchone()
                    status = "analyzed" if (row and row["analyzed_at"]) else "pending"
                    if orig:
                        conn.execute(
                            "UPDATE photos SET status=?, path=? WHERE id=?",
                            (status, orig, pid),
                        )
                    else:
                        conn.execute(
                            "UPDATE photos SET status=? WHERE id=?", (status, pid)
                        )
                conn.commit()
            finally:
                conn.close()

    def _get_original_path(self, photo_id: int) -> str | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT original_path, path FROM photos WHERE id=?", (photo_id,)
                ).fetchone()
                if not row:
                    return None
                return row["original_path"] or row["path"]
            finally:
                conn.close()

    def paths_for_folder(self, folder: str) -> list[str]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT path FROM photos WHERE folder=? AND status != 'deleted'",
                    (folder,),
                ).fetchall()
                return [r["path"] for r in rows]
            finally:
                conn.close()

    def analysis_state_for_folder(self, folder: str) -> dict[str, dict[str, Any]]:
        """The five columns the analysis queue needs, keyed by path.

        One query instead of one per photo: deciding what still has to be
        analysed only needs the status, the score and whether a picture exists,
        and asking for those with ``get_photo_by_path`` meant a SELECT plus a
        full row decode (tags, exif, dimensions, video_info) per file - on a
        four-thousand-photo album that is thousands of JSON parses to answer a
        question this query answers once.
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT path, status, score, media_kind, proxy_path, thumb_path "
                    "FROM photos WHERE folder=?",
                    (folder,),
                ).fetchall()
                return {row["path"]: dict(row) for row in rows}
            finally:
                conn.close()

    def stalled_video_rows(self, folder: str) -> list[dict[str, Any]]:
        """Videos in one folder that were indexed but never read successfully.

        Only the four columns the caller needs are selected, and the filter runs
        in SQL rather than in Python: the previous version loaded every row of
        the folder - EXIF, tags, dimensions and all - to find the few videos that
        still have no metadata. On an album of a few thousand photos that walk
        happened on every scan.

        A row also counts as stalled when it lost its poster: without one there
        is nothing to show and nothing to send to the model, so it needs another
        pass whichever status it currently holds.
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT path, raw_path, status, video_info FROM photos "
                    "WHERE folder=? "
                    # COALESCE, not a bare comparison: rows written before the
                    # column existed carry NULL and would otherwise be invisible
                    # to this query while still counting as videos elsewhere.
                    "AND COALESCE(media_kind, 'photo') = 'video' "
                    "AND ("
                    # Never read: an error or pending row with no metadata.
                    "  (status IN ('error', 'pending') "
                    "   AND (video_info IS NULL OR video_info IN ('', '{}', 'null')))"
                    # Or read once, but the poster it needs is gone.
                    "  OR thumb_path IS NULL OR thumb_path = ''"
                    "  OR proxy_path IS NULL OR proxy_path = ''"
                    ")",
                    (folder,),
                ).fetchall()
                return [
                    {
                        "path": row["path"],
                        "raw_path": row["raw_path"],
                        "status": row["status"],
                        "video_info": row["video_info"],
                    }
                    for row in rows
                ]
            finally:
                conn.close()

    def rows_missing_render(self, folder: str) -> list[dict[str, Any]]:
        """Rows in one folder whose gallery render was never written.

        A copy interrupted by a pulled disk, or an index pass that died halfway,
        leaves a row behind without a thumbnail or a proxy. Its path is already
        known, so the next incremental sync skipped it - the card stayed blank
        for ever even after the disk came back. The rebuild pass asks for these
        rows and finishes what the interrupted pass started.

        ``status='error'`` is included because an interrupted index leaves the
        row in that state; rows that do have a usable render are filtered out by
        the caller, which keeps an ordinary model failure from being re-rendered
        on every sync. Analysed rows are left out entirely: a render cleared by
        "清理缓存" is regenerated on demand, and re-indexing a finished photo
        would rewrite the EXIF and location that analysis already stored.
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, path, raw_path, thumb_path, proxy_path, status "
                    "FROM photos WHERE folder=? AND status != 'deleted' "
                    "AND status != 'analyzed' "
                    "AND COALESCE(media_kind, 'photo') != 'video' "
                    "AND (thumb_path IS NULL OR thumb_path = '' "
                    "     OR proxy_path IS NULL OR proxy_path = '' "
                    "     OR status = 'error')",
                    (folder,),
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def get_settings(self) -> dict[str, str]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT key, value FROM settings").fetchall()
                return {r["key"]: r["value"] for r in rows}
            finally:
                conn.close()

    def save_settings(self, data: dict[str, Any]) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                    [
                        (
                            str(k),
                            json.dumps(v, ensure_ascii=False)
                            if not isinstance(v, str)
                            else v,
                        )
                        for k, v in data.items()
                    ],
                )
                conn.commit()
            finally:
                conn.close()

    def delete_settings(self, keys: Sequence[str]) -> int:
        """Drop settings rows that no longer map to a configuration field."""
        if not keys:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.executemany(
                    "DELETE FROM settings WHERE key=?", [(str(k),) for k in keys]
                )
                conn.commit()
                return max(0, cursor.rowcount)
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Application metadata (migration markers, one-off repair versions)
    # ------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        """Read one ``app_meta`` entry, or ``None`` when it is not set."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT value FROM app_meta WHERE key=?", (str(key),)
                ).fetchone()
                return row["value"] if row else None
            finally:
                conn.close()

    def set_meta(self, key: str, value: str) -> None:
        """Record one ``app_meta`` entry, overwriting any previous value."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO app_meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(key), str(value)),
                )
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # UI state
    # ------------------------------------------------------------------

    def get_ui_state(self) -> dict[str, Any]:
        """Return the browser-independent UI state.

        This used to live in ``localStorage``, which meant clearing the
        browser's site data silently threw away the folder list, the open album
        and the remembered card path. Keeping it beside the photos makes the
        album's memory belong to the album.
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT key, value FROM ui_state").fetchall()
            finally:
                conn.close()
        state: dict[str, Any] = {}
        for row in rows:
            try:
                state[row["key"]] = json.loads(row["value"])
            except (TypeError, ValueError):
                state[row["key"]] = row["value"]
        return state

    def save_ui_state(self, data: dict[str, Any]) -> None:
        """Merge ``data`` into the stored UI state."""
        if not data:
            return
        now = utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO ui_state(key, value, updated_at) VALUES (?, ?, ?)",
                    [
                        (str(k), json.dumps(v, ensure_ascii=False), now)
                        for k, v in data.items()
                    ],
                )
                conn.commit()
            finally:
                conn.close()

    def delete_rows(self, ids: Sequence[int]) -> None:
        """Permanently remove photo rows (used after permanent deletion)."""
        if not ids:
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "DELETE FROM photo_embeddings WHERE photo_id=?",
                    [(pid,) for pid in ids],
                )
                conn.executemany(
                    "DELETE FROM photos WHERE id=?", [(pid,) for pid in ids]
                )
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Camera-album pairings and card action queue
    # ------------------------------------------------------------------

    def upsert_card_album(
        self, folder: str, source: str, volume: int | None, volume_key: str | None = None
    ) -> None:
        """Remember that ``folder`` receives photos from the card at ``source``.

        One album is fed by one card, so a pairing replaces whatever the album
        pointed at before. Without that, every drive-letter change left the old
        row behind: ``card_album_for`` then had to choose between several rows,
        and the choice could land on a letter the card no longer uses.
        """
        now = utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM card_albums WHERE folder=? AND source!=?", (folder, source))
                conn.execute(
                    """
                    INSERT INTO card_albums
                        (folder, source, volume, volume_key, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(folder, source) DO UPDATE SET
                        volume=excluded.volume,
                        volume_key=COALESCE(excluded.volume_key, card_albums.volume_key),
                        updated_at=excluded.updated_at
                    """,
                    (folder, source, volume, volume_key, now, now),
                )
                conn.commit()
            finally:
                conn.close()

    def list_card_albums(self, folder: str | None = None) -> list[dict[str, Any]]:
        """Return recorded camera-album pairings, newest first.

        ``id DESC`` breaks ties: two rows written in the same second used to sort
        arbitrarily, which made the "which card is this?" answer unstable.
        """
        with self._lock:
            conn = self._connect()
            try:
                if folder:
                    rows = conn.execute(
                        "SELECT * FROM card_albums WHERE folder=? "
                        "ORDER BY updated_at DESC, id DESC",
                        (folder,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM card_albums ORDER BY updated_at DESC, id DESC"
                    ).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                conn.close()

    def card_album_for(self, folder: str) -> dict[str, Any] | None:
        """Return the most recently used pairing for one local folder."""
        rows = self.list_card_albums(folder)
        return rows[0] if rows else None

    def remove_card_album(self, folder: str, source: str | None = None) -> int:
        """Forget a pairing (and its queued actions). Never touches files."""
        with self._lock:
            conn = self._connect()
            try:
                if source:
                    cursor = conn.execute(
                        "DELETE FROM card_albums WHERE folder=? AND source=?",
                        (folder, source),
                    )
                    conn.execute(
                        "DELETE FROM card_actions WHERE album=? AND source=?",
                        (folder, source),
                    )
                else:
                    cursor = conn.execute(
                        "DELETE FROM card_albums WHERE folder=?", (folder,)
                    )
                    conn.execute("DELETE FROM card_actions WHERE album=?", (folder,))
                conn.commit()
                return max(0, cursor.rowcount)
            finally:
                conn.close()

    def save_card_action(self, action: dict[str, Any]) -> int:
        """Insert or replace the queued action for one file.

        ``UNIQUE(album, relative_path)`` means a photo can only ever have one
        outstanding action, so a delete followed by a restore overwrites the row
        instead of stacking two contradictory instructions.
        """
        now = utc_now()
        record = {
            "album": action["album"],
            "source": action["source"],
            "volume": action.get("volume"),
            "kind": action["kind"],
            "state": action.get("state", "pending"),
            "relative_path": action["relative_path"],
            "card_path": action.get("card_path"),
            "filename": action.get("filename") or Path(action["relative_path"]).name,
            "size": int(action.get("size") or 0),
            "is_raw": 1 if action.get("is_raw") else 0,
            "photo_id": action.get("photo_id"),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO card_actions
                        (album, source, volume, kind, state, relative_path, card_path,
                         filename, size, is_raw, photo_id, created_at, updated_at)
                    VALUES
                        (:album, :source, :volume, :kind, :state, :relative_path, :card_path,
                         :filename, :size, :is_raw, :photo_id, :created_at, :updated_at)
                    ON CONFLICT(album, relative_path) DO UPDATE SET
                        source=excluded.source,
                        volume=excluded.volume,
                        kind=excluded.kind,
                        state=excluded.state,
                        card_path=excluded.card_path,
                        filename=excluded.filename,
                        size=excluded.size,
                        is_raw=excluded.is_raw,
                        photo_id=excluded.photo_id,
                        updated_at=excluded.updated_at
                    """,
                    record,
                )
                conn.commit()
                return cursor.lastrowid or 0
            finally:
                conn.close()

    def card_actions(
        self,
        source: str | None = None,
        state: str | None = None,
        album: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return queued card actions, oldest first."""
        clauses = []
        params: list[Any] = []
        if source:
            clauses.append("source=?")
            params.append(source)
        if state:
            clauses.append("state=?")
            params.append(state)
        if album:
            clauses.append("album=?")
            params.append(album)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT * FROM card_actions {where} ORDER BY id", params
                ).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                conn.close()

    def card_action_for(self, album: str, relative_path: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM card_actions WHERE album=? AND relative_path=?",
                    (album, relative_path),
                ).fetchone()
                return self._row_to_dict(row) if row else None
            finally:
                conn.close()

    def card_action_for_photo(self, photo_id: int) -> list[dict[str, Any]]:
        """Return the outstanding card actions belonging to one photo row."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM card_actions WHERE photo_id=? ORDER BY id",
                    (photo_id,),
                ).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                conn.close()

    def set_card_action_state(
        self, action_id: int, state: str, card_path: str | None = None
    ) -> None:
        with self._lock:
            conn = self._connect()
            try:
                if card_path is None:
                    conn.execute(
                        "UPDATE card_actions SET state=?, updated_at=? WHERE id=?",
                        (state, utc_now(), action_id),
                    )
                else:
                    conn.execute(
                        "UPDATE card_actions SET state=?, card_path=?, updated_at=? WHERE id=?",
                        (state, card_path, utc_now(), action_id),
                    )
                conn.commit()
            finally:
                conn.close()

    def delete_card_action(self, action_id: int) -> None:
        self.delete_card_actions([action_id])

    def delete_card_actions(self, ids: Sequence[int]) -> None:
        if not ids:
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "DELETE FROM card_actions WHERE id=?", [(int(i),) for i in ids]
                )
                conn.commit()
            finally:
                conn.close()

    def set_favorite(self, photo_id: int, favorite: bool) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE photos SET favorite=? WHERE id=?",
                    (1 if favorite else 0, photo_id),
                )
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _folder_filter_clauses(
        folder: str | None,
        exclude_folders: Sequence[str] | None,
        prefix: str = "",
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        col = f"{prefix}folder"
        if folder:
            clauses.append(f"{col} = ?")
            params.append(folder)
        elif exclude_folders:
            cleaned_ex = list(
                {v for f in exclude_folders if f for v in (str(f), canonical_path(f)) if v}
            )
            if cleaned_ex:
                placeholders = ",".join("?" for _ in cleaned_ex)
                clauses.append(f"{col} NOT IN ({placeholders})")
                params.extend(cleaned_ex)
        return clauses, params

    def memories_on(
        self,
        month: int,
        day: int,
        window: int = 0,
        folder: str | None = None,
        exclude_folders: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Photos taken on this calendar day in earlier years.

        ``window`` widens the net symmetrically - 0 is the exact day, 1 adds the
        day before and after - because a trip rarely falls on one date across
        years, and a memory that comes back empty is worse than one that is a day
        off. Days are compared as text: the capture time is already normalised to
        ``YYYY-MM-DD`` by ``_capture_time_sql_expr``, so slicing the month and day
        out of it is exact and needs no date parsing in Python.

        The current year is excluded: "a year ago today" should be a memory, not
        this morning.
        """
        window = max(0, min(int(window), 30))
        days = set()
        for offset in range(-window, window + 1):
            shifted = date(2000, month, day) + timedelta(days=offset)
            days.add(shifted.strftime("%m-%d"))

        clauses = [
            "captured_at IS NOT NULL",
            "status != 'deleted'",
            f"substr(captured_at, 6, 5) IN ({','.join('?' for _ in days)})",
            "capture_year < ?",
        ]
        params: list[Any] = sorted(days)
        # "Earlier years" is measured on the local calendar: the user's today,
        # not UTC's, which can be a different date for most of the world.
        params.append(datetime.now().astimezone().year)
        f_clauses, f_params = self._folder_filter_clauses(folder, exclude_folders)
        clauses.extend(f_clauses)
        params.extend(f_params)
        sql = (
            "SELECT * FROM photos WHERE "
            + " AND ".join(clauses)
            + " ORDER BY captured_at ASC, id ASC"
        )
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
                return [self._row_to_dict(row) for row in rows]
            finally:
                conn.close()

    def all_tags(
        self,
        folder: str | None = None,
        exclude_folders: Sequence[str] | None = None,
    ) -> list[str]:
        """Return all distinct tags for a folder (or across all visible folders)."""
        clauses = ["status != 'deleted'"]
        params: list[Any] = []
        f_clauses, f_params = self._folder_filter_clauses(folder, exclude_folders)
        clauses.extend(f_clauses)
        params.extend(f_params)
        where = "WHERE " + " AND ".join(clauses)
        tags_set = set()
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT tags FROM photos {where}", params
                ).fetchall()
                for row in rows:
                    if not row["tags"]:
                        continue
                    try:
                        tags = json.loads(row["tags"])
                    except (TypeError, ValueError):
                        continue
                    if isinstance(tags, list):
                        for t in tags:
                            if t:
                                tags_set.add(str(t))
            finally:
                conn.close()
        return sorted(tags_set)

    def capture_years(
        self,
        folder: str | None = None,
        exclude_folders: Sequence[str] | None = None,
    ) -> list[int]:
        """Return distinct capture years for active photos using the indexed column."""
        clauses = ["status != 'deleted'", "capture_year IS NOT NULL"]
        params: list[Any] = []
        f_clauses, f_params = self._folder_filter_clauses(folder, exclude_folders)
        clauses.extend(f_clauses)
        params.extend(f_params)
        where = "WHERE " + " AND ".join(clauses)
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT DISTINCT capture_year FROM photos {where} ORDER BY capture_year DESC",
                    params,
                ).fetchall()
                return [int(r["capture_year"]) for r in rows if r["capture_year"]]
            finally:
                conn.close()

    def all_folders(self) -> list[str]:
        """Return indexed folders, most recently active first."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT folder
                    FROM photos
                    WHERE folder IS NOT NULL AND folder != ''
                    GROUP BY folder
                    ORDER BY MAX(created_at) DESC
                    """
                ).fetchall()
                return [r["folder"] for r in rows]
            finally:
                conn.close()

    def cache_paths_for_folder(self, folder: str) -> list[dict[str, str | None]]:
        """Return every cached file one folder's rows still reference.

        Videos add three more kinds of derived file (the preview clip, the
        remuxed/transcoded copy and the motion part of a Live Photo). Leaving
        them out of this list would let "清理缓存" delete files that are still in
        use - and "移除目录" leave them behind for ever.
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT thumb_path, proxy_path, clip_path, play_path, live_path "
                    "FROM photos WHERE folder=? AND (thumb_path IS NOT NULL "
                    "OR proxy_path IS NOT NULL OR clip_path IS NOT NULL "
                    "OR play_path IS NOT NULL OR live_path IS NOT NULL)",
                    (folder,),
                ).fetchall()
                return [
                    {
                        "thumb_path": r["thumb_path"],
                        "proxy_path": r["proxy_path"],
                        "clip_path": r["clip_path"],
                        "play_path": r["play_path"],
                        "live_path": r["live_path"],
                    }
                    for r in rows
                ]
            finally:
                conn.close()

    def update_proxy_path(self, photo_id: int, proxy_path: str) -> None:
        """Persist the model/gallery proxy path."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE photos SET proxy_path=? WHERE id=?",
                    (proxy_path, photo_id),
                )
                conn.commit()
            finally:
                conn.close()

    def rows_for_orientation_check(
        self,
    ) -> list[tuple[int, str, int, int, str | None, str | None, str | None]]:
        """``(id, path, width, height, thumb_path, proxy_path, phash)``.

        Used by the orientation repair pass, which only needs file headers and
        therefore wants the cheapest possible query.
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, path, width, height, thumb_path, proxy_path, phash "
                    "FROM photos WHERE path IS NOT NULL AND path != ''"
                ).fetchall()
                return [
                    (
                        int(r["id"]),
                        r["path"],
                        int(r["width"] or 0),
                        int(r["height"] or 0),
                        r["thumb_path"],
                        r["proxy_path"],
                        r["phash"],
                    )
                    for r in rows
                ]
            finally:
                conn.close()

    def located_photos(
        self,
        folder: str | None = None,
        exclude_folders: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Every photo that carries a place or custom place binding, for the footprint view."""
        where = [
            "(place_id IS NOT NULL OR (location IS NOT NULL AND location != ''))",
            "status != 'deleted'",
        ]
        params: list[Any] = []
        f_clauses, f_params = self._folder_filter_clauses(folder, exclude_folders)
        where.extend(f_clauses)
        params.extend(f_params)
        sql = (
            "SELECT id, path, raw_path, original_path, location, place_id, lat, lon, "
            "location_source, filename, size, width, height, thumb_path, proxy_path, "
            "phash, exif, tags, reason, title, dimensions, score, status, media_kind, "
            "favorite, analyzed_at, error, model, geocoded, duration_ms, "
            "video_info, live_path "
            f"FROM photos WHERE {' AND '.join(where)}"
        )
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
                return [self._row_to_dict(row) for row in rows]
            finally:
                conn.close()

    def duplicate_candidates(
        self,
        folder: str | None = None,
        exclude_folders: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """The columns duplicate detection needs, and nothing else.

        Deliberately not ``list_photos``: that decodes tags, dimensions, EXIF and
        video_info for every row, and at eleven thousand photos it is the parsing
        that costs, not the distance arithmetic. This query returns seven small
        columns and the grouping runs in about a second.
        """
        where = ["phash IS NOT NULL", "phash != ''", "status != 'deleted'"]
        params: list[Any] = []
        f_clauses, f_params = self._folder_filter_clauses(folder, exclude_folders)
        where.extend(f_clauses)
        params.extend(f_params)
        sql = (
            "SELECT id, phash, width, height, size, status, folder, filename, "
            "thumb_path, media_kind, score, favorite, path, original_path, raw_path "
            f"FROM photos WHERE {' AND '.join(where)}"
        )
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
                return [self._row_to_dict(row) for row in rows]
            finally:
                conn.close()

    def update_dimensions_only(self, updates: Sequence[tuple[int, int, int]]) -> int:
        """Write corrected ``(width, height)`` for a batch of photo ids."""
        if not updates:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "UPDATE photos SET width=?, height=? WHERE id=?",
                    [(w, h, pid) for pid, w, h in updates],
                )
                conn.commit()
                return len(updates)
            finally:
                conn.close()

    def rows_with_dimensions(self) -> list[tuple[int, dict[str, Any]]]:
        """``(id, dimensions)`` for every photo that has a dimensions object.

        Used by the migration that folds the retired "composition" score into
        "technical"; the raw JSON is decoded here so the caller only has to
        decide what the merged object should look like.
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, dimensions FROM photos "
                    "WHERE dimensions IS NOT NULL AND dimensions != '' "
                    "AND dimensions != '{}'"
                ).fetchall()
            finally:
                conn.close()
        result: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            try:
                parsed = json.loads(row["dimensions"])
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                result.append((int(row["id"]), parsed))
        return result

    def rewrite_dimensions(self, updates: Sequence[tuple[int, dict[str, Any]]]) -> int:
        """Replace the dimensions object of a batch of photos."""
        if not updates:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "UPDATE photos SET dimensions=? WHERE id=?",
                    [
                        (json.dumps(dims, ensure_ascii=False), int(pid))
                        for pid, dims in updates
                    ],
                )
                conn.commit()
                return len(updates)
            finally:
                conn.close()

    def clear_rendered_cache(self, photo_ids: Sequence[int]) -> int:
        """Forget thumb/proxy/hash so the next view re-renders from scratch.

        The cache files themselves are removed by the caller; clearing the
        columns is what makes the existing on-demand generation in
        ``/api/thumbnail`` and ``/api/proxy`` kick in again.
        """
        if not photo_ids:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "UPDATE photos SET thumb_path=NULL, proxy_path=NULL, phash=NULL "
                    "WHERE id=?",
                    [(int(pid),) for pid in photo_ids],
                )
                conn.commit()
                return len(photo_ids)
            finally:
                conn.close()

    def update_cache_paths(
        self,
        photo_id: int,
        thumb_path: str,
        proxy_path: str,
        phash: str | None = None,
    ) -> None:
        """Persist gallery/proxy paths generated on demand."""
        with self._lock:
            conn = self._connect()
            try:
                if phash:
                    conn.execute(
                        "UPDATE photos SET thumb_path=?, proxy_path=?, phash=? "
                        "WHERE id=?",
                        (thumb_path, proxy_path, phash, photo_id),
                    )
                else:
                    conn.execute(
                        "UPDATE photos SET thumb_path=?, proxy_path=? WHERE id=?",
                        (thumb_path, proxy_path, photo_id),
                    )
                conn.commit()
            finally:
                conn.close()

    def clear_video_paths(self, photo_id: int) -> None:
        """Forget every derived file for one video, after they were deleted.

        Unlike ``set_video_paths`` this writes NULLs on purpose: the columns must
        stop pointing at files that no longer exist, so the next index pass
        regenerates them instead of serving a 404 from the cache.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE photos SET clip_path=NULL, play_path=NULL, "
                    "play_state=NULL, thumb_path=NULL, proxy_path=NULL "
                    "WHERE id=?",
                    (photo_id,),
                )
                conn.commit()
            finally:
                conn.close()

    def video_rows_for_folder(self, folder: str) -> list[dict[str, Any]]:
        """Every video row in one folder, with just the derived-file columns."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, path, thumb_path, proxy_path, clip_path, play_path "
                    "FROM photos WHERE folder=? AND COALESCE(media_kind, 'photo') = 'video' "
                    "AND status != 'deleted'",
                    (folder,),
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def set_video_paths(
        self,
        photo_id: int,
        clip_path: str | None = None,
        play_path: str | None = None,
        play_state: str | None = None,
    ) -> None:
        """Remember a derived video file, leaving the others alone.

        Every argument defaults to ``None`` so a caller can record just the one
        file it produced - generating the preview must not forget the remuxed
        copy, and vice versa.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE photos SET "
                    "clip_path=COALESCE(?, clip_path), "
                    "play_path=COALESCE(?, play_path), "
                    "play_state=COALESCE(?, play_state) "
                    "WHERE id=?",
                    (clip_path, play_path, play_state, photo_id),
                )
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        try:
            d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
        except (TypeError, ValueError):
            d["tags"] = []
        try:
            d["dimensions"] = json.loads(d["dimensions"]) if d.get("dimensions") else {}
        except (TypeError, ValueError):
            d["dimensions"] = {}
        try:
            d["exif"] = json.loads(d["exif"]) if d.get("exif") else {}
        except (TypeError, ValueError):
            d["exif"] = {}
        try:
            d["video_info"] = (
                json.loads(d["video_info"]) if d.get("video_info") else {}
            )
        except (TypeError, ValueError):
            d["video_info"] = {}
        if not d.get("media_kind"):
            # Rows written before video support existed are photos.
            d["media_kind"] = "photo"
        return d

    # ------------------------------------------------------------------
    # People & Pets (local face + cat/dog recognition & clustering)
    # ------------------------------------------------------------------

    def photos_needing_face_scan(
        self, folder: str | None = None, model_name: str = "buffalo_l", force: bool = False
    ) -> list[dict[str, Any]]:
        clauses = [
            "status != 'deleted'",
            "COALESCE(media_kind, 'photo') != 'video'",
        ]
        params: list[Any] = []
        if folder:
            clauses.append("folder = ?")
            params.append(folder)
        if not force:
            clauses.append("(faces_scanned_model IS NULL OR faces_scanned_model != ?)")
            params.append(model_name)
        sql = (
            "SELECT id, path, proxy_path, thumb_path, folder, filename "
            f"FROM photos WHERE {' AND '.join(clauses)} ORDER BY id ASC"
        )
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def mark_photo_faces_scanned(self, photo_id: int, model_name: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE photos SET faces_scanned_model=? WHERE id=?",
                    (model_name, int(photo_id)),
                )
                conn.commit()
            finally:
                conn.close()

    def replace_auto_faces_for_photo(
        self, photo_id: int, detections: Sequence[dict[str, Any]], model_name: str
    ) -> list[int]:
        """Replace auto-detected faces for a photo while keeping manual/ignored labels."""
        now = utc_now()
        inserted_ids: list[int] = []
        with self._lock:
            conn = self._connect()
            try:
                # Keep manual and ignored detections untouched.
                kept = conn.execute(
                    "SELECT bbox_x, bbox_y, bbox_w, bbox_h, entity_kind "
                    "FROM faces WHERE photo_id=? AND status IN ('manual', 'ignored')",
                    (int(photo_id),),
                ).fetchall()
                conn.execute(
                    "DELETE FROM faces WHERE photo_id=? AND status IN ('auto', 'pending')",
                    (int(photo_id),),
                )
                for det in detections:
                    # Skip detections overlapping an already manual/ignored box.
                    bx, by, bw, bh = (
                        float(det["bbox_x"]),
                        float(det["bbox_y"]),
                        float(det["bbox_w"]),
                        float(det["bbox_h"]),
                    )
                    overlap = False
                    for k in kept:
                        ix1 = max(bx, float(k["bbox_x"]))
                        iy1 = max(by, float(k["bbox_y"]))
                        ix2 = min(bx + bw, float(k["bbox_x"]) + float(k["bbox_w"]))
                        iy2 = min(by + bh, float(k["bbox_y"]) + float(k["bbox_h"]))
                        inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
                        union = bw * bh + float(k["bbox_w"]) * float(k["bbox_h"]) - inter
                        if union > 1e-9 and (inter / union) >= 0.45:
                            overlap = True
                            break
                    if overlap:
                        continue
                    cur = conn.execute(
                        """
                        INSERT INTO faces
                            (photo_id, person_id, entity_kind, bbox_x, bbox_y, bbox_w, bbox_h,
                             det_score, embedding, model_name, status, thumb_path, created_at, updated_at)
                        VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'auto', NULL, ?, ?)
                        """,
                        (
                            int(photo_id),
                            str(det.get("entity_kind") or "person"),
                            bx,
                            by,
                            bw,
                            bh,
                            float(det.get("det_score") or 0.0),
                            det.get("embedding"),
                            str(det.get("model_name") or model_name),
                            now,
                            now,
                        ),
                    )
                    inserted_ids.append(int(cur.lastrowid))
                conn.commit()
            finally:
                conn.close()
        return inserted_ids

    def update_face_thumb(self, face_id: int, thumb_path: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE faces SET thumb_path=?, updated_at=? WHERE id=?",
                    (thumb_path, utc_now(), int(face_id)),
                )
                conn.commit()
            finally:
                conn.close()

    def create_person(
        self,
        name: str = "",
        kind: str = "person",
        cover_face_id: int | None = None,
        centroid: bytes | None = None,
    ) -> int:
        now = utc_now()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    INSERT INTO people (name, kind, cover_face_id, is_hidden, is_favorite, centroid, created_at, updated_at)
                    VALUES (?, ?, ?, 0, 0, ?, ?, ?)
                    """,
                    (name.strip(), kind or "person", cover_face_id, centroid, now, now),
                )
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def get_person(self, person_id: int) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id, name, kind, cover_face_id, is_hidden, is_favorite, created_at, updated_at "
                    "FROM people WHERE id=?",
                    (int(person_id),),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def all_people_raw(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT * FROM people ORDER BY id ASC").fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def find_person_by_name(
        self, name: str, exclude_id: int | None = None
    ) -> dict[str, Any] | None:
        clean = name.strip()
        if not clean:
            return None
        with self._lock:
            conn = self._connect()
            try:
                if exclude_id is not None:
                    row = conn.execute(
                        "SELECT id, name, kind, cover_face_id, is_hidden, is_favorite "
                        "FROM people WHERE LOWER(name)=LOWER(?) AND id!=? LIMIT 1",
                        (clean, int(exclude_id)),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT id, name, kind, cover_face_id, is_hidden, is_favorite "
                        "FROM people WHERE LOWER(name)=LOWER(?) LIMIT 1",
                        (clean,),
                    ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def rename_person(self, person_id: int, name: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE people SET name=?, updated_at=? WHERE id=?",
                    (name.strip(), utc_now(), int(person_id)),
                )
                conn.commit()
            finally:
                conn.close()

    def set_person_flags(
        self,
        person_id: int,
        is_hidden: bool | None = None,
        is_favorite: bool | None = None,
        cover_face_id: int | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if is_hidden is not None:
            sets.append("is_hidden=?")
            params.append(1 if is_hidden else 0)
        if is_favorite is not None:
            sets.append("is_favorite=?")
            params.append(1 if is_favorite else 0)
        if cover_face_id is not None:
            sets.append("cover_face_id=?")
            params.append(int(cover_face_id))
        if not sets:
            return
        sets.append("updated_at=?")
        params.append(utc_now())
        params.append(int(person_id))
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(f"UPDATE people SET {', '.join(sets)} WHERE id=?", params)
                conn.commit()
            finally:
                conn.close()

    def update_person_centroid(self, person_id: int, centroid: bytes | None) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE people SET centroid=?, updated_at=? WHERE id=?",
                    (centroid, utc_now(), int(person_id)),
                )
                conn.commit()
            finally:
                conn.close()

    def merge_people(self, source_ids: Sequence[int], target_id: int) -> None:
        sources = [int(s) for s in source_ids if int(s) != int(target_id)]
        if not sources:
            return
        placeholders = ",".join("?" for _ in sources)
        now = utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    f"UPDATE faces SET person_id=?, status='manual', updated_at=? "
                    f"WHERE person_id IN ({placeholders}) AND status != 'ignored'",
                    [int(target_id), now, *sources],
                )
                # Preserve favorite if any merged person was favorited.
                fav_row = conn.execute(
                    f"SELECT MAX(is_favorite) f FROM people WHERE id IN ({placeholders}, ?)",
                    [*sources, int(target_id)],
                ).fetchone()
                if fav_row and fav_row["f"]:
                    conn.execute(
                        "UPDATE people SET is_favorite=1, updated_at=? WHERE id=?",
                        (now, int(target_id)),
                    )
                conn.execute(
                    f"DELETE FROM people WHERE id IN ({placeholders})", sources
                )
                conn.commit()
            finally:
                conn.close()

    def get_face(self, face_id: int) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM faces WHERE id=?", (int(face_id),)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def get_face_public(self, face_id: int) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT f.id, f.photo_id, f.person_id, f.entity_kind,
                           f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h, f.det_score, f.status,
                           COALESCE(p.name, '') AS person_name,
                           COALESCE(p.kind, f.entity_kind) AS person_kind
                    FROM faces f
                    LEFT JOIN people p ON p.id = f.person_id
                    WHERE f.id = ?
                    """,
                    (int(face_id),),
                ).fetchone()
                if not row:
                    return None
                d = dict(row)
                d["thumb_url"] = f"/api/faces/{d['id']}/thumb"
                return d
            finally:
                conn.close()

    def set_face_assignment(
        self, face_id: int, person_id: int | None, status: str = "auto"
    ) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE faces SET person_id=?, status=?, updated_at=? WHERE id=?",
                    (person_id, status, utc_now(), int(face_id)),
                )
                conn.commit()
            finally:
                conn.close()

    def unassigned_faces(
        self, model_name: str = "buffalo_l", entity_kind: str | None = None
    ) -> list[dict[str, Any]]:
        clauses = [
            "f.person_id IS NULL",
            "f.status = 'auto'",
            "f.embedding IS NOT NULL",
            "p.status != 'deleted'",
        ]
        params: list[Any] = []
        if entity_kind:
            clauses.append("f.entity_kind = ?")
            params.append(entity_kind)
        if entity_kind == "person" and model_name:
            clauses.append("f.model_name = ?")
            params.append(model_name)
        sql = (
            "SELECT f.* FROM faces f JOIN photos p ON p.id = f.photo_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY f.id ASC"
        )
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def faces_for_person(
        self, person_id: int, include_pending: bool = False
    ) -> list[dict[str, Any]]:
        statuses = "('manual', 'auto', 'pending')" if include_pending else "('manual', 'auto')"
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT f.* FROM faces f JOIN photos p ON p.id = f.photo_id "
                    f"WHERE f.person_id=? AND f.status IN {statuses} AND p.status != 'deleted' "
                    f"ORDER BY f.det_score DESC, f.id ASC",
                    (int(person_id),),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def faces_with_thumb_paths(self) -> list[dict[str, Any]]:
        """Return ``id/thumb_path/folder`` for legacy thumbnail migration."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT f.id, f.thumb_path, p.folder, p.path "
                    "FROM faces f JOIN photos p ON p.id = f.photo_id "
                    "WHERE f.thumb_path IS NOT NULL AND f.thumb_path != ''"
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def faces_for_photo(self, photo_id: int) -> list[dict[str, Any]]:
        """Return detected people & pets for one photo (used by detail lightbox)."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT f.id, f.photo_id, f.person_id, f.entity_kind,
                           f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h, f.det_score, f.status,
                           COALESCE(p.name, '') AS person_name,
                           COALESCE(p.kind, f.entity_kind) AS person_kind
                    FROM faces f
                    LEFT JOIN people p ON p.id = f.person_id
                    WHERE f.photo_id = ? AND f.status != 'ignored'
                    ORDER BY f.bbox_x ASC, f.id ASC
                    """,
                    (int(photo_id),),
                ).fetchall()
                result = []
                for r in rows:
                    d = dict(r)
                    d["thumb_url"] = f"/api/faces/{d['id']}/thumb"
                    result.append(d)
                return result
            finally:
                conn.close()

    def list_people(
        self,
        folder: str | None = None,
        include_hidden: bool = False,
        kind: str | None = None,
        min_unnamed_count: int = 2,
        exclude_folders: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["ph.status != 'deleted'", "f.status IN ('manual', 'auto')"]
        params: list[Any] = []
        f_clauses, f_params = self._folder_filter_clauses(folder, exclude_folders, prefix="ph.")
        clauses.extend(f_clauses)
        params.extend(f_params)
        where_faces = " AND ".join(clauses)
        pending_f_clauses, pending_f_params = self._folder_filter_clauses(
            folder, exclude_folders, prefix="php."
        )
        pending_folder_sql = ("AND " + " AND ".join(pending_f_clauses)) if pending_f_clauses else ""
        people_clauses = []
        if not include_hidden:
            people_clauses.append("pe.is_hidden = 0")
        if kind == "pet":
            people_clauses.append("pe.kind IN ('cat', 'dog')")
        elif kind in ("person", "cat", "dog"):
            people_clauses.append("pe.kind = ?")
            params.append(kind)
        where_people = ("WHERE " + " AND ".join(people_clauses)) if people_clauses else ""
        sql = f"""
            SELECT pe.id, pe.name, pe.kind, pe.cover_face_id, pe.is_hidden, pe.is_favorite,
                   COUNT(DISTINCT f.photo_id) AS photo_count,
                   COALESCE((
                       SELECT COUNT(*) FROM faces fp
                       JOIN photos php ON php.id = fp.photo_id
                       WHERE fp.person_id = pe.id AND fp.status = 'pending' AND php.status != 'deleted'
                       {pending_folder_sql}
                   ), 0) AS pending_count,
                   MIN(f.id) AS fallback_face_id
            FROM people pe
            JOIN faces f ON f.person_id = pe.id
            JOIN photos ph ON ph.id = f.photo_id AND {where_faces}
            {where_people}
            GROUP BY pe.id
            HAVING (pe.name != '' OR COUNT(DISTINCT f.photo_id) >= ?)
            ORDER BY pe.is_favorite DESC, (pe.name = '') ASC, photo_count DESC, pe.id ASC
        """
        query_params = pending_f_params + params + [max(1, int(min_unnamed_count))]
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, query_params).fetchall()
                out = []
                for r in rows:
                    cover_id = r["cover_face_id"] or r["fallback_face_id"]
                    out.append(
                        {
                            "id": int(r["id"]),
                            "name": r["name"] or "",
                            "kind": r["kind"] or "person",
                            "cover_face_id": cover_id,
                            "thumb_url": f"/api/faces/{cover_id}/thumb" if cover_id else "",
                            "is_hidden": bool(r["is_hidden"]),
                            "is_favorite": bool(r["is_favorite"]),
                            "count": int(r["photo_count"]),
                            "pending_count": int(r["pending_count"]),
                        }
                    )
                return out
            finally:
                conn.close()

    def person_photos(
        self,
        person_id: int,
        folder: str | None = None,
        pending: bool = False,
        exclude_folders: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        status_clause = "f.status = 'pending'" if pending else "f.status IN ('manual', 'auto')"
        clauses = ["f.person_id = ?", status_clause, "ph.status != 'deleted'"]
        params: list[Any] = [int(person_id)]
        f_clauses, f_params = self._folder_filter_clauses(folder, exclude_folders, prefix="ph.")
        clauses.extend(f_clauses)
        params.extend(f_params)
        sql = (
            "SELECT DISTINCT ph.* FROM photos ph "
            "JOIN faces f ON f.photo_id = ph.id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY (ph.captured_at IS NULL) ASC, ph.captured_at DESC, ph.id DESC"
        )
        return self._query_photos(sql, params)

    def person_pending_faces(
        self,
        person_id: int,
        folder: str | None = None,
        exclude_folders: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["f.person_id = ?", "f.status = 'pending'", "ph.status != 'deleted'"]
        params: list[Any] = [int(person_id)]
        f_clauses, f_params = self._folder_filter_clauses(folder, exclude_folders, prefix="ph.")
        clauses.extend(f_clauses)
        params.extend(f_params)
        sql = (
            "SELECT f.id AS face_id, f.photo_id, f.det_score, f.entity_kind, ph.filename, ph.title "
            "FROM faces f JOIN photos ph ON ph.id = f.photo_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY f.det_score DESC, f.id DESC LIMIT 60"
        )
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
                return [
                    {
                        "face_id": int(r["face_id"]),
                        "photo_id": int(r["photo_id"]),
                        "det_score": float(r["det_score"] or 0.0),
                        "entity_kind": r["entity_kind"] or "person",
                        "filename": r["filename"] or "",
                        "title": r["title"] or "",
                        "thumb_url": f"/api/faces/{r['face_id']}/thumb",
                    }
                    for r in rows
                ]
            finally:
                conn.close()


    # ------------------------------------------------------------------
    # Custom Places (用户自建地点与照片地点覆盖，原图 EXIF 全程不动)
    # ------------------------------------------------------------------

    def list_places(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM places ORDER BY updated_at DESC, id DESC"
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def get_place(self, place_id: int) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM places WHERE id=?", (int(place_id),)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def find_place_by_name(
        self, name: str, exclude_id: int | None = None
    ) -> dict[str, Any] | None:
        clean = name.strip()
        if not clean:
            return None
        with self._lock:
            conn = self._connect()
            try:
                if exclude_id is not None:
                    row = conn.execute(
                        "SELECT * FROM places WHERE LOWER(name)=LOWER(?) AND id!=? LIMIT 1",
                        (clean, int(exclude_id)),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT * FROM places WHERE LOWER(name)=LOWER(?) LIMIT 1",
                        (clean,),
                    ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def create_place(
        self,
        name: str,
        city: str = "",
        address: str = "",
        lat: float | None = None,
        lon: float | None = None,
        source: str = "manual",
        cover_photo_id: int | None = None,
    ) -> int:
        now = utc_now()
        clean_name = name.strip()
        clean_city = (city or clean_name).strip()
        clean_addr = (address or clean_name).strip()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    INSERT INTO places (name, city, address, lat, lon, source, cover_photo_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_name,
                        clean_city,
                        clean_addr,
                        lat,
                        lon,
                        source or "manual",
                        cover_photo_id,
                        now,
                        now,
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def update_place(
        self,
        place_id: int,
        name: str | None = None,
        city: str | None = None,
        address: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        update_coords: bool = False,
        cover_photo_id: int | None = None,
        update_cover: bool = False,
    ) -> dict[str, Any] | None:
        existing = self.get_place(place_id)
        if not existing:
            return None
        new_name = name.strip() if name is not None else existing["name"]
        new_city = city.strip() if city is not None else (existing.get("city") or new_name)
        new_address = address.strip() if address is not None else (existing.get("address") or new_name)
        new_lat = lat if update_coords else existing.get("lat")
        new_lon = lon if update_coords else existing.get("lon")
        new_cover = cover_photo_id if update_cover else existing.get("cover_photo_id")
        now = utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    UPDATE places
                    SET name=?, city=?, address=?, lat=?, lon=?, cover_photo_id=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        new_name,
                        new_city,
                        new_address,
                        new_lat,
                        new_lon,
                        new_cover,
                        now,
                        int(place_id),
                    ),
                )
                # Keep DB location & coords on assigned photos in sync (never touches file EXIF).
                display_loc = new_address or new_name
                conn.execute(
                    "UPDATE photos SET location=?, lat=?, lon=? WHERE place_id=?",
                    (display_loc, new_lat, new_lon, int(place_id)),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get_place(place_id)

    def delete_place(self, place_id: int, clear_location: bool = True) -> bool:
        """Delete a custom place and unlink its photos without deleting any files."""
        existing = self.get_place(place_id)
        if not existing:
            return False
        with self._lock:
            conn = self._connect()
            try:
                if clear_location:
                    rows = conn.execute(
                        "SELECT id, exif FROM photos WHERE place_id=?", (int(place_id),)
                    ).fetchall()
                    for r in rows:
                        exif = {}
                        if r["exif"]:
                            try:
                                exif = json.loads(r["exif"])
                            except (TypeError, ValueError):
                                exif = {}
                        elat = exif.get("latitude") if isinstance(exif, dict) else None
                        elon = exif.get("longitude") if isinstance(exif, dict) else None
                        conn.execute(
                            "UPDATE photos SET place_id=NULL, location=NULL, lat=?, lon=?, location_source=NULL WHERE id=?",
                            (elat, elon, int(r["id"])),
                        )
                else:
                    conn.execute(
                        "UPDATE photos SET place_id=NULL, location_source=NULL WHERE place_id=?",
                        (int(place_id),),
                    )
                conn.execute("DELETE FROM places WHERE id=?", (int(place_id),))
                conn.commit()
                return True
            finally:
                conn.close()

    def assign_photos_to_place(
        self, place_id: int, photo_ids: Sequence[int]
    ) -> int:
        """Assign photos to a custom place, overwriting DB location/coords (never touches EXIF)."""
        place = self.get_place(place_id)
        if not place:
            return 0
        clean_ids = [int(pid) for pid in photo_ids]
        if not clean_ids:
            return 0
        display_loc = (place.get("address") or place.get("name") or "").strip()
        lat = place.get("lat")
        lon = place.get("lon")
        updated = 0
        with self._lock:
            conn = self._connect()
            try:
                for pid in clean_ids:
                    cur = conn.execute(
                        """
                        UPDATE photos
                        SET place_id=?, location=?, lat=?, lon=?, location_source='manual'
                        WHERE id=? AND status != 'deleted'
                        """,
                        (int(place_id), display_loc, lat, lon, pid),
                    )
                    updated += max(0, cur.rowcount)
                if not place.get("cover_photo_id") and clean_ids:
                    conn.execute(
                        "UPDATE places SET cover_photo_id=?, updated_at=? WHERE id=?",
                        (clean_ids[0], utc_now(), int(place_id)),
                    )
                else:
                    conn.execute(
                        "UPDATE places SET updated_at=? WHERE id=?",
                        (utc_now(), int(place_id)),
                    )
                conn.commit()
            finally:
                conn.close()
        return updated

    def remove_photos_from_place(
        self, place_id: int, photo_ids: Sequence[int]
    ) -> int:
        """Unlink photos from a custom place and clear their manual DB location (never touches EXIF)."""
        clean_ids = [int(pid) for pid in photo_ids]
        if not clean_ids:
            return 0
        removed = 0
        with self._lock:
            conn = self._connect()
            try:
                for pid in clean_ids:
                    row = conn.execute(
                        "SELECT id, exif FROM photos WHERE id=? AND place_id=?",
                        (pid, int(place_id)),
                    ).fetchone()
                    if not row:
                        continue
                    exif = {}
                    if row["exif"]:
                        try:
                            exif = json.loads(row["exif"])
                        except (TypeError, ValueError):
                            exif = {}
                    elat = exif.get("latitude") if isinstance(exif, dict) else None
                    elon = exif.get("longitude") if isinstance(exif, dict) else None
                    cur = conn.execute(
                        """
                        UPDATE photos
                        SET place_id=NULL, location=NULL, lat=?, lon=?, location_source=NULL
                        WHERE id=? AND place_id=?
                        """,
                        (elat, elon, pid, int(place_id)),
                    )
                    removed += max(0, cur.rowcount)
                # Refresh cover if the previous cover was removed.
                place = conn.execute(
                    "SELECT cover_photo_id FROM places WHERE id=?", (int(place_id),)
                ).fetchone()
                if place and place["cover_photo_id"] in clean_ids:
                    nxt = conn.execute(
                        "SELECT id FROM photos WHERE place_id=? AND status != 'deleted' ORDER BY id ASC LIMIT 1",
                        (int(place_id),),
                    ).fetchone()
                    conn.execute(
                        "UPDATE places SET cover_photo_id=?, updated_at=? WHERE id=?",
                        (int(nxt["id"]) if nxt else None, utc_now(), int(place_id)),
                    )
                conn.commit()
            finally:
                conn.close()
        return removed

    # ------------------------------------------------------------------
    # Multimodal photo embeddings (CLIP / SigLIP & visual descriptors)
    # ------------------------------------------------------------------

    def upsert_photo_embedding(
        self, photo_id: int, model: str, dim: int, embedding: bytes
    ) -> None:
        now = utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO photo_embeddings (photo_id, model, dim, embedding, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(photo_id, model) DO UPDATE SET
                        dim=excluded.dim,
                        embedding=excluded.embedding,
                        updated_at=excluded.updated_at
                    """,
                    (int(photo_id), str(model), int(dim), embedding, now),
                )
                conn.commit()
            finally:
                conn.close()

    def get_photo_embedding(self, photo_id: int, model: str) -> bytes | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT embedding FROM photo_embeddings WHERE photo_id=? AND model=?",
                    (int(photo_id), str(model)),
                ).fetchone()
                return bytes(row["embedding"]) if row and row["embedding"] is not None else None
            finally:
                conn.close()

    def list_photo_embeddings(
        self, model: str, photo_ids: Sequence[int] | None = None
    ) -> dict[int, bytes]:
        with self._lock:
            conn = self._connect()
            try:
                if photo_ids is not None:
                    ids = [int(pid) for pid in photo_ids]
                    if not ids:
                        return {}
                    out: dict[int, bytes] = {}
                    for i in range(0, len(ids), 500):
                        chunk = ids[i : i + 500]
                        placeholders = ",".join("?" for _ in chunk)
                        rows = conn.execute(
                            f"SELECT photo_id, embedding FROM photo_embeddings WHERE model=? AND photo_id IN ({placeholders})",
                            [str(model), *chunk],
                        ).fetchall()
                        for r in rows:
                            if r["embedding"] is not None:
                                out[int(r["photo_id"])] = bytes(r["embedding"])
                    return out
                rows = conn.execute(
                    "SELECT photo_id, embedding FROM photo_embeddings WHERE model=?",
                    (str(model),),
                ).fetchall()
                return {
                    int(r["photo_id"]): bytes(r["embedding"])
                    for r in rows
                    if r["embedding"] is not None
                }
            finally:
                conn.close()

    def count_photo_embeddings(
        self,
        model: str,
        folder: str | None = None,
        exclude_folders: Sequence[str] | None = None,
    ) -> int:
        clauses = ["e.model = ?", "p.status != 'deleted'"]
        params: list[Any] = [str(model)]
        norm_folder = canonical_path(folder) if folder else None
        f_clauses, f_params = self._folder_filter_clauses(norm_folder, exclude_folders, prefix="p.")
        clauses.extend(f_clauses)
        params.extend(f_params)
        sql = (
            "SELECT COUNT(*) AS c FROM photo_embeddings e "
            "JOIN photos p ON p.id = e.photo_id "
            f"WHERE {' AND '.join(clauses)}"
        )
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(sql, params).fetchone()
                return int(row["c"]) if row else 0
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Virtual Smart Albums (saved filter/search rules)
    # ------------------------------------------------------------------

    def list_smart_albums(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, name, rules, created_at, updated_at FROM smart_albums ORDER BY id DESC"
                ).fetchall()
                out: list[dict[str, Any]] = []
                for r in rows:
                    try:
                        rules = json.loads(r["rules"]) if r["rules"] else {}
                    except (TypeError, ValueError):
                        rules = {}
                    out.append(
                        {
                            "id": int(r["id"]),
                            "name": r["name"],
                            "rules": rules if isinstance(rules, dict) else {},
                            "created_at": r["created_at"],
                            "updated_at": r["updated_at"],
                        }
                    )
                return out
            finally:
                conn.close()

    def create_smart_album(self, name: str, rules: dict[str, Any]) -> dict[str, Any]:
        clean_name = (name or "").strip()[:40]
        if not clean_name:
            raise ValueError("影集名称不能为空")
        now = utc_now()
        rules_json = json.dumps(rules or {}, ensure_ascii=False)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "INSERT INTO smart_albums (name, rules, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (clean_name, rules_json, now, now),
                )
                conn.commit()
                return {
                    "id": int(cur.lastrowid),
                    "name": clean_name,
                    "rules": dict(rules or {}),
                    "created_at": now,
                    "updated_at": now,
                }
            finally:
                conn.close()

    def delete_smart_album(self, album_id: int) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM smart_albums WHERE id=?", (int(album_id),))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()


# Global database instance, initialized when the app starts.
DB: Database | None = None


def get_db(path: str | None = None) -> Database:
    global DB
    if DB is None:
        DB = Database(path or "data/library.db")
    return DB
