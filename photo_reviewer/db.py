"""SQLite storage for photo metadata and analysis results."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    exif = photo.get("exif") or {}
    if not isinstance(exif, dict):
        exif = {}
    return parse_capture_datetime(
        exif.get("datetime_original") or exif.get("DateTimeOriginal")
    )


def photo_capture_year(photo: dict[str, Any]) -> int | None:
    """Return the 4-digit capture year for a photo payload/row."""
    value = photo_capture_time(photo)
    return value.year if value else None


def _time_sort_key(photo: dict[str, Any], descending: bool):
    """Photos without capture time always sort after photos that have one."""
    captured = photo_capture_time(photo)
    if captured is None:
        return (1, 0.0)
    timestamp = captured.timestamp()
    return (0, -timestamp) if descending else (0, timestamp)


def sort_photos(photos: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """Sort photo dicts in place using the same sort names as the UI."""
    if sort == "score_desc":
        photos.sort(key=lambda p: (p.get("score") is None, -(p.get("score") or 0)))
    elif sort == "score_asc":
        photos.sort(key=lambda p: (p.get("score") is None, p.get("score") or 0))
    elif sort == "filename":
        photos.sort(key=lambda p: str(p.get("filename") or "").lower())
    elif sort == "recent":
        photos.sort(key=lambda p: p.get("analyzed_at") or "", reverse=True)
    elif sort == "size":
        photos.sort(key=lambda p: p.get("size") or 0, reverse=True)
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
        """Close every cached connection (tests and shutdown paths)."""
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
                        recommendation TEXT,
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
                    CREATE INDEX IF NOT EXISTS idx_photos_recommendation ON photos(recommendation);
                    CREATE INDEX IF NOT EXISTS idx_photos_score ON photos(score);
                    CREATE INDEX IF NOT EXISTS idx_photos_path ON photos(path);
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
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
                conn.commit()
            finally:
                conn.close()

    def upsert_photo(self, record: dict[str, Any]) -> int:
        """Insert a photo row, or update it if the path already exists."""
        now = utc_now()
        record.setdefault("created_at", now)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO photos
                        (path, original_path, folder, filename, size, width, height, thumb_path,
                         proxy_path, dimensions, location, exif, phash, score, recommendation, tags, reason, title, raw_path, model,
                         analyzed_at, status, favorite, geocoded, error, created_at)
                    VALUES
                        (:path, :original_path, :folder, :filename, :size, :width, :height, :thumb_path,
                         :proxy_path, :dimensions, :location, :exif, :phash, :score, :recommendation, :tags, :reason, :title, :raw_path, :model,
                         :analyzed_at, :status, :favorite, :geocoded, :error, :created_at)
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
                        location=excluded.location,
                        exif=excluded.exif,
                        phash=excluded.phash,
                        score=excluded.score,
                        recommendation=excluded.recommendation,
                        tags=excluded.tags,
                        reason=excluded.reason,
                        title=excluded.title,
                        raw_path=excluded.raw_path,
                        model=excluded.model,
                        analyzed_at=excluded.analyzed_at,
                        status=excluded.status,
                        favorite=excluded.favorite,
                        error=excluded.error,
                        created_at=excluded.created_at
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
                        "phash": record.get("phash"),
                        "score": record.get("score"),
                        "recommendation": record.get("recommendation"),
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
        recommendation: str | None = None,
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
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if folder:
            clauses.append("folder = ?")
            params.append(folder)
        if status == "pending":
            clauses.append("status IN ('pending','error')")
        elif status:
            clauses.append("status = ?")
            params.append(status)
        if exclude_deleted:
            clauses.append("status != 'deleted'")
        if recommendation:
            clauses.append("recommendation = ?")
            params.append(recommendation)
        if min_score is not None:
            clauses.append("score >= ?")
            params.append(min_score)
        if max_score is not None:
            clauses.append("score <= ?")
            params.append(max_score)
        if search:
            clauses.append("(filename LIKE ? OR reason LIKE ? OR tags LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])
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
            clauses.append(f"{_capture_year_sql_expr()} = ?")
            params.append(str(year))
            where = "WHERE " + " AND ".join(clauses)
        if sort in ("time_asc", "time_desc"):
            direction = "ASC" if sort == "time_asc" else "DESC"
            capture_time = _capture_time_sql_expr()
            order = f"({capture_time}) IS NULL ASC, {capture_time} {direction}"
            sql = f"SELECT * FROM photos {where} ORDER BY {order} LIMIT ? OFFSET ?"
            return self._query_photos(sql, params + [limit, offset])

        sort_map = {
            "score_asc": "(score IS NULL) ASC, score ASC",
            "score_desc": "(score IS NULL) ASC, score DESC",
            "filename": "filename COLLATE NOCASE ASC",
            "recent": "analyzed_at DESC",
            "size": "size DESC",
        }
        order = sort_map.get(sort, sort_map["score_asc"])
        sql = f"SELECT * FROM photos {where} ORDER BY {order} LIMIT ? OFFSET ?"
        return self._query_photos(sql, params + [limit, offset])

    def _query_photos(self, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count_photos(
        self,
        folder: str | None = None,
        status: str | None = None,
        recommendation: str | None = None,
    ) -> int:
        clauses = []
        params: list[Any] = []
        if folder:
            clauses.append("folder = ?")
            params.append(folder)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if recommendation:
            clauses.append("recommendation = ?")
            params.append(recommendation)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) AS c FROM photos {where}", params
                ).fetchone()
                return int(row["c"])
            finally:
                conn.close()

    def stats(self, folder: str | None = None) -> dict[str, int]:
        clauses = []
        params: list[Any] = []
        if folder:
            clauses.append("folder = ?")
            params.append(folder)
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
        recommendation: str,
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
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    UPDATE photos SET
                        score=?, recommendation=?, tags=?, reason=?, model=?,
                        title=COALESCE(NULLIF(?, ''), title),
                        phash=COALESCE(?, phash), width=COALESCE(?, width),
                        height=COALESCE(?, height), thumb_path=COALESCE(?, thumb_path),
                        proxy_path=COALESCE(?, proxy_path),
                        dimensions=COALESCE(?, dimensions),
                        location=COALESCE(?, location),
                        exif=COALESCE(?, exif),
                        status=?, error=?, analyzed_at=?
                    WHERE id=?
                    """,
                    (
                        score,
                        recommendation,
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
                conn.execute(
                    """
                    UPDATE photos SET
                        score=?, dimensions=?, tags=?, reason=?, title=?, location=?,
                        analyzed_at=?
                    WHERE id=?
                    """,
                    (
                        score,
                        json.dumps(dimensions, ensure_ascii=False),
                        json.dumps(tags, ensure_ascii=False),
                        reason,
                        title,
                        location,
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
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE photos SET location=?, exif=? WHERE id=?",
                    (
                        location,
                        json.dumps(exif or {}, ensure_ascii=False)
                        if isinstance(exif, dict)
                        else None,
                        photo_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def mark_error(self, photo_id: int, error: str) -> None:
        """Record a processing failure for one photo.

        Superseded by ``scanner._record_photo_error`` (which preserves an
        existing good analysis); kept because it is the narrow, explicit way to
        flag a row and is used by tests.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE photos SET status='error', error=?, analyzed_at=? WHERE id=?",
                    (error, utc_now(), photo_id),
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
                            reason=NULL, title=NULL, recommendation=NULL,
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
                        recommendation=(SELECT recommendation FROM photos WHERE id=?),
                        model=(SELECT model FROM photos WHERE id=?),
                        analyzed_at=(SELECT analyzed_at FROM photos WHERE id=?),
                        status=CASE
                            WHEN (SELECT score FROM photos WHERE id=?) IS NOT NULL
                            THEN 'analyzed' ELSE status END
                    WHERE id=?
                    """,
                    (source_id,) * 9 + (target_id,),
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

    def analyzed_rows_for_folder(self, folder: str) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM photos WHERE folder=? AND status='analyzed' AND phash IS NOT NULL",
                    (folder,),
                ).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                conn.close()

    def update_duplicate_tag(
        self, photo_id: int, score: float, recommendation: str, reason: str
    ) -> None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT tags FROM photos WHERE id=?", (photo_id,)
                ).fetchone()
                tags = []
                if row and row["tags"]:
                    try:
                        tags = json.loads(row["tags"])
                    except (TypeError, ValueError):
                        tags = []
                if "duplicate" not in tags:
                    tags.append("duplicate")
                conn.execute(
                    "UPDATE photos SET tags=?, score=?, recommendation=?, reason=? WHERE id=?",
                    (
                        json.dumps(tags, ensure_ascii=False),
                        score,
                        recommendation,
                        reason,
                        photo_id,
                    ),
                )
                conn.commit()
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

    def delete_rows(self, ids: Sequence[int]) -> None:
        """Permanently remove photo rows (used after permanent deletion)."""
        if not ids:
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "DELETE FROM photos WHERE id=?", [(pid,) for pid in ids]
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

    def all_tags(self, folder: str | None = None) -> list[str]:
        """Return all distinct tags for a folder (or across all folders)."""
        clauses = []
        params: list[Any] = []
        if folder:
            clauses.append("folder = ?")
            params.append(folder)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
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

    def capture_years(self, folder: str | None = None) -> list[int]:
        """Return distinct capture years for active photos."""
        clauses = ["status != 'deleted'"]
        params: list[Any] = []
        if folder:
            clauses.append("folder = ?")
            params.append(folder)
        where = "WHERE " + " AND ".join(clauses)
        years = set()
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT exif FROM photos {where}", params
                ).fetchall()
                for row in rows:
                    if not row["exif"]:
                        continue
                    try:
                        exif = json.loads(row["exif"])
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(exif, dict):
                        continue
                    year = photo_capture_year({"exif": exif})
                    if year:
                        years.add(year)
            finally:
                conn.close()
        return sorted(years, reverse=True)

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
        """Return referenced proxy paths for one folder's cache directory."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT thumb_path, proxy_path FROM photos WHERE folder=? AND (thumb_path IS NOT NULL OR proxy_path IS NOT NULL)",
                    (folder,),
                ).fetchall()
                return [
                    {"thumb_path": r["thumb_path"], "proxy_path": r["proxy_path"]}
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

    def update_cache_paths(
        self, photo_id: int, thumb_path: str, proxy_path: str
    ) -> None:
        """Persist gallery/proxy paths generated on demand."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE photos SET thumb_path=?, proxy_path=? WHERE id=?",
                    (thumb_path, proxy_path, photo_id),
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
        return d


# Global database instance, initialized when the app starts.
DB: Database | None = None


def get_db(path: str | None = None) -> Database:
    global DB
    if DB is None:
        DB = Database(path or "data/library.db")
    return DB


def set_db(db: Database) -> None:
    global DB
    DB = db
