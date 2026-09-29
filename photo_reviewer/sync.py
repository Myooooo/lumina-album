"""Run an SD-card import as a background job.

The flow mirrors a scan: create a job, hand the work to a daemon thread, and let
the existing progress UI poll ``/api/scan/status/<id>``. The job phases are

``plan``    walking the card and comparing it with the local folder
``copy``    writing the files
``index``   building proxies and index rows for what was copied
``analyze`` optionally calling the vision model (never the default)
"""

from __future__ import annotations

import logging
import os
import threading
import time

from .config import Config
from .db import Database
from .importer import (
    AFTER_COPY_ONLY,
    AFTER_INDEX,
    AFTER_ORGANIZE,
    FULL,
    IMPORT_MODES,
    INCREMENTAL,
    RAW_POLICIES,
    RAW_POLICY_IMAGE_FIRST,
    ImportError_,
    plan_import,
    run_import,
)
from .scanner import JOBS, ScanJob, start_scoped_scan

logger = logging.getLogger(__name__)

#: How often the import job mirrors a scoped scan's progress.
_FOLLOW_INTERVAL = 0.25


def normalize_options(data: dict) -> dict:
    """Validate a request body into import options."""
    mode = str(data.get("mode") or INCREMENTAL).strip().lower()
    if mode not in IMPORT_MODES:
        raise ImportError_(f"未知的导入模式: {mode}")
    raw_policy = str(data.get("raw_policy") or RAW_POLICY_IMAGE_FIRST).strip().lower()
    if raw_policy not in RAW_POLICIES:
        raise ImportError_(f"未知的 RAW 处理方式: {raw_policy}")
    after = str(data.get("after") or AFTER_INDEX).strip().lower()
    if after not in (AFTER_COPY_ONLY, AFTER_INDEX, AFTER_ORGANIZE):
        raise ImportError_(f"未知的拷贝后操作: {after}")
    return {
        "mode": mode,
        "raw_policy": raw_policy,
        "after": after,
        "source": str(data.get("source") or "").strip(),
        "target": str(data.get("target") or "").strip(),
        "reset_analysis": bool(data.get("reset_analysis", True)),
    }


def build_plan(data: dict, config: Config):
    """Validate a request body and plan the import without copying anything."""
    options = normalize_options(data)
    if not options["source"]:
        raise ImportError_("请提供 SD 卡路径")
    if not options["target"]:
        raise ImportError_("请先选择本地的回忆文件夹")
    plan = plan_import(
        options["source"],
        options["target"],
        config,
        mode=options["mode"],
        raw_policy=options["raw_policy"],
    )
    plan.options = options  # type: ignore[attr-defined]
    return plan


def start_import(data: dict, db: Database, config: Config, plan=None) -> ScanJob:
    """Validate, then run an import in the background."""
    if plan is None:
        plan = build_plan(data, config)
    options = getattr(plan, "options", None) or normalize_options(data)

    for existing in JOBS.list():
        if existing.folder == plan.target and existing.status == "running":
            raise ImportError_("该文件夹已有任务正在运行，请等它完成后再导入")

    job = JOBS.create(plan.target, False)
    job.phase = "copy"
    job.total = len(plan.copies)
    job.detail = f"{options['mode']} / {options['raw_policy']} / {options['after']}"
    thread = threading.Thread(
        target=_import_worker,
        args=(job.id, plan, options, db, config),
        daemon=True,
    )
    thread.start()
    return job


def _cancelled(job_id: str) -> bool:
    job = JOBS.get(job_id)
    return bool(job and job.cancelled)


def _import_worker(job_id: str, plan, options: dict, db: Database, config: Config) -> None:
    job = JOBS.get(job_id)
    if not job:
        return
    try:
        JOBS.update(job_id, status="running", phase="copy", processed=0, current="")

        if plan.copies:
            report = run_import(
                plan,
                should_cancel=lambda: _cancelled(job_id),
                on_progress=lambda index, relative: JOBS.update(
                    job_id, processed=index, current=relative
                ),
            )
        else:
            report = {
                "copied": 0,
                "failed": [],
                "cancelled": False,
                "total": 0,
                "bytes_copied": 0,
            }
        job.report = {
            **report,
            "new_count": len(plan.added),
            "overwrite_count": len(plan.overwritten),
            "skip_count": plan.skipped,
            "local_only_count": plan.local_only,
            "warnings": plan.warnings,
        }

        if report["cancelled"]:
            JOBS.update(job_id, status="cancelled", current="导入已取消")
            return
        if report["failed"]:
            logger.warning("import finished with %d failures", len(report["failed"]))

        # A full import can replace a file's contents; the previous score,
        # tags and comment described a different picture, so they are cleared.
        if options["mode"] == FULL and options["reset_analysis"]:
            replaced = [
                os.path.join(plan.target, c.relative_path)
                for c in plan.overwritten
                if c.content_changed
            ]
            reset = db.reset_analysis_for_paths(replaced)
            if reset:
                logger.info("cleared stale analysis for %d replaced photos", reset)

        after = options["after"]
        if after == AFTER_COPY_ONLY:
            JOBS.update(
                job_id,
                status="completed",
                phase="copy",
                current="已完成拷贝（未建立索引）",
            )
            return

        copied = [
            os.path.join(plan.target, copy.relative_path)
            for copy in plan.copies
            if os.path.exists(os.path.join(plan.target, copy.relative_path))
        ]
        if not copied:
            JOBS.update(
                job_id,
                status="completed",
                phase="copy",
                current="没有需要索引的新照片",
            )
            return

        child = start_scoped_scan(
            plan.target, db, config, copied, analyze=(after == AFTER_ORGANIZE)
        )
        _follow_child(job_id, child.id)
    except Exception as exc:
        logger.warning("import job failed", exc_info=True)
        JOBS.update(job_id, status="error", error=str(exc))


def _follow_child(parent_id: str, child_id: str) -> None:
    """Mirror a scoped scan's progress into the import job until it ends."""
    while True:
        child = JOBS.get(child_id)
        parent = JOBS.get(parent_id)
        if not child or not parent:
            return
        if parent.cancelled:
            JOBS.cancel(child_id)
        JOBS.update(
            parent_id,
            status="running",
            phase=child.phase if child.phase in ("index", "analyze") else "index",
            total=child.total,
            processed=child.processed,
            current=child.current,
        )
        if child.status in ("completed", "cancelled", "error"):
            JOBS.update(
                parent_id,
                status=child.status,
                phase=child.phase,
                error=child.error,
                current=child.current,
            )
            return
        time.sleep(_FOLLOW_INTERVAL)
