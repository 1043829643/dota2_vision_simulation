from __future__ import annotations

import json
import os
import sys
import hashlib
import uuid
import urllib.request
import urllib.error
import urllib.parse
import multiprocessing as mp
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import pymysql
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from compute_ward_hero_visibility import (  # noqa: E402
    CacheFow,
    RESOURCE_ROOT,
    VisibilityGrid,
    compute_match,
    load_match_info,
    project_path,
    resolve_team_side,
)
import compute_ward_value_metrics as ward_value  # noqa: E402

# 让 backend 目录可被 import（含子进程 worker 通过限定名反序列化时的 import）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import match_worker  # noqa: E402


# 从 db_settings.json 加载默认数据库配置（当环境变量未设置时使用）
_DB_SETTINGS_FILE = Path(__file__).resolve().parents[2] / "db_settings.json"
_db_settings: dict = {}
if _DB_SETTINGS_FILE.exists():
    try:
        _db_settings = json.loads(_DB_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        _db_settings = {}


def _env_or_config(key: str, default: str = "") -> str:
    """优先读环境变量，再读 db_settings.json，最后用默认值"""
    val = os.environ.get(key)
    if val:
        return val
    val = _db_settings.get(key)
    if val:
        return str(val)
    return default


DEFAULT_DB = _env_or_config("DOTA_DB_DATABASE", "dota2_analysis")
DEFAULT_OVERVIEW_DB = _env_or_config("DOTA_OVERVIEW_DATABASE", "dwd_dota2")
CACHE_VERSION = "ward-hero-visibility-v8"
WARD_VALUE_CACHE_VERSION = "ward-value-v1"
COMPARISON_CACHE_VERSION = "team-comparison-v4"
# 缓存目录必须持久：/tmp 会在系统重启/清理时丢失全部单场缓存与预热历史。
DEFAULT_CACHE_ROOT = str(PROJECT_ROOT / "var" / "web_cache") if os.environ.get("DEPLOY_RUN_PORT") else str(PROJECT_ROOT / "outputs" / "web_cache")
CACHE_ROOT = Path(_env_or_config("DOTA_CACHE_ROOT", DEFAULT_CACHE_ROOT))
JOB_EXECUTOR = ThreadPoolExecutor(max_workers=int(_env_or_config("DOTA_JOB_WORKERS", "2")))
JOB_LOCK = Lock()

# 多进程计算池：单场眼位计算是纯 Python（受 GIL 限制），用多进程绕过 GIL 吃满多核。
# DOTA_COMPUTE_PROCS<=1 时退回单进程串行路径。
DOTA_COMPUTE_PROCS = int(_env_or_config("DOTA_COMPUTE_PROCS", "3"))
_COMPUTE_POOL: ProcessPoolExecutor | None = None
_COMPUTE_POOL_LOCK = Lock()


def get_compute_pool() -> ProcessPoolExecutor | None:
    """惰性创建全局进程池；创建失败或禁用时返回 None（调用方回退串行）。"""
    global _COMPUTE_POOL
    if DOTA_COMPUTE_PROCS <= 1:
        return None
    if _COMPUTE_POOL is not None:
        return _COMPUTE_POOL
    with _COMPUTE_POOL_LOCK:
        if _COMPUTE_POOL is None:
            for method in ("forkserver", "spawn", "fork"):
                try:
                    ctx = mp.get_context(method)
                    _COMPUTE_POOL = ProcessPoolExecutor(max_workers=DOTA_COMPUTE_PROCS, mp_context=ctx)
                    break
                except (ValueError, OSError):
                    continue
    return _COMPUTE_POOL
JOBS: dict[str, dict] = {}
MAX_JOBS = 100

TEAM_LOGO_CACHE_FILE = "team_logos.json"
TEAM_LOGO_LOCK = Lock()
_TEAM_LOGO_CACHE: dict | None = None

TEAM_DIRECTORY_CACHE_FILE = "team_directory.json"
TEAM_DIRECTORY_LOCK = Lock()
_TEAM_DIRECTORY_CACHE: dict | None = None
TEAM_DIRECTORY_TTL = 24 * 3600

PREWARM_HISTORY_FILE = "prewarm_history.json"
PREWARMED_TEAMS_MANIFEST_FILE = "prewarmed_teams.json"
PREWARM_HISTORY_LOCK = Lock()
# 单独的锁保护战队清单 manifest：save_prewarm_record 会在持有 PREWARM_HISTORY_LOCK
# 时调用 upsert_prewarmed_team_entry，若两者共用同一把非重入锁会造成同线程自死锁。
PREWARMED_TEAMS_LOCK = Lock()
MAX_PREWARM_HISTORY = 30
MATCH_WARD_CACHE_VERSION = "match-ward-v1"
_MATCH_WARD_LOCKS: dict[str, Lock] = {}
_MATCH_WARD_LOCKS_GUARD = Lock()


class VisibilityRequest(BaseModel):
    teamTag: str = Field(min_length=1)
    matchIds: list[int] = Field(min_length=1)
    start: int | None = None
    end: int | None = None
    forceRefresh: bool = False
    compareBothSides: bool = False


class WardValueRequest(BaseModel):
    teamTag: str = Field(min_length=1)
    matchIds: list[int] = Field(min_length=1)
    start: int | None = None
    end: int | None = None
    forceRefresh: bool = False
    clusterEps: float = Field(default=200.0, gt=0)


class ComparisonTeam(BaseModel):
    teamTag: str = Field(min_length=1)
    matchIds: list[int] = Field(min_length=1)


class TeamComparisonRequest(BaseModel):
    teams: list[ComparisonTeam] = Field(min_length=2, max_length=20)
    start: int | None = None
    end: int | None = None
    forceRefresh: bool = False
    clusterEps: float = Field(default=200.0, gt=0)


class PrewarmRequest(BaseModel):
    teams: list[str] = Field(min_length=1, max_length=12)
    teamIds: list[str | None] | None = None
    recent: int = Field(default=10, ge=1, le=50)
    start: int | None = None
    end: int | None = None
    clusterEps: float = Field(default=200.0, gt=0)
    forceRefresh: bool = False
    includeWardValue: bool = True
    includeComparison: bool = True


class RefreshPrewarmTeamRequest(BaseModel):
    teamTag: str = Field(min_length=1)
    forceRefresh: bool = False


def db_config() -> dict:
    return {
        "host": _env_or_config("DOTA_DB_HOST", "127.0.0.1"),
        "port": int(_env_or_config("DOTA_DB_PORT", "9030")),
        "user": _env_or_config("DOTA_DB_USER", ""),
        "password": _env_or_config("DOTA_DB_PASSWORD", os.environ.get("DB_PASS", "")),
        "database": DEFAULT_DB,
    }


def connect():
    cfg = db_config()
    if not cfg["user"] or not cfg["password"]:
        raise HTTPException(
            status_code=500,
            detail="DOTA_DB_USER and DOTA_DB_PASSWORD must be set on the server.",
        )
    try:
        return pymysql.connect(
            **cfg,
            charset="utf8mb4",
            connect_timeout=10,
            read_timeout=90,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except pymysql.MySQLError as exc:
        raise HTTPException(status_code=500, detail=f"database connection failed: {exc}") from exc


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def public_job(job: dict) -> dict:
    return {
        "jobId": job["jobId"],
        "kind": job["kind"],
        "status": job["status"],
        "createdAt": job["createdAt"],
        "startedAt": job.get("startedAt"),
        "finishedAt": job.get("finishedAt"),
        "error": job.get("error"),
        "progress": job.get("progress"),
    }


def trim_jobs() -> None:
    if len(JOBS) <= MAX_JOBS:
        return
    removable = sorted(
        [job for job in JOBS.values() if job["status"] in {"succeeded", "failed"}],
        key=lambda item: item.get("finishedAt") or item["createdAt"],
    )
    for job in removable[: max(0, len(JOBS) - MAX_JOBS)]:
        JOBS.pop(job["jobId"], None)


def set_job_progress(job_id: str, progress: dict) -> None:
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if job:
            job["progress"] = {
                **(job.get("progress") or {}),
                **progress,
                "updatedAt": utc_now_iso(),
            }


def run_job(job_id: str, runner, payload) -> None:
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["startedAt"] = utc_now_iso()
        job["progress"] = {"phase": "starting", "message": "任务启动中", "updatedAt": job["startedAt"]}
    try:
        result = runner(payload, lambda progress: set_job_progress(job_id, progress))
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, ensure_ascii=False)
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if job:
                job["status"] = "failed"
                job["error"] = detail
                job["finishedAt"] = utc_now_iso()
        return
    except Exception as exc:
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if job:
                job["status"] = "failed"
                job["error"] = f"{type(exc).__name__}: {exc}"
                job["finishedAt"] = utc_now_iso()
        return
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if job:
            job["status"] = "succeeded"
            job["result"] = result
            job["finishedAt"] = utc_now_iso()


def create_job(kind: str, runner, payload) -> dict:
    job_id = uuid.uuid4().hex
    job = {
        "jobId": job_id,
        "kind": kind,
        "status": "queued",
        "createdAt": utc_now_iso(),
        "startedAt": None,
        "finishedAt": None,
        "error": None,
        "result": None,
        "progress": {"phase": "queued", "message": "等待执行", "updatedAt": utc_now_iso()},
    }
    with JOB_LOCK:
        JOBS[job_id] = job
        trim_jobs()
    JOB_EXECUTOR.submit(run_job, job_id, runner, payload)
    return public_job(job)


@lru_cache(maxsize=1)
def grid_and_cache() -> tuple[VisibilityGrid, CacheFow]:
    grid_path = RESOURCE_ROOT / "native-fow" / "dota_static_fow_grid.json"
    cache_path = RESOURCE_ROOT / "native-fow" / "cache.fow"
    return VisibilityGrid.load(grid_path), CacheFow.load(cache_path)


@lru_cache(maxsize=1)
def ward_value_tree_cells() -> dict[int, tuple[int, int]]:
    grid, _ = grid_and_cache()
    return ward_value.load_tree_id_cells(RESOURCE_ROOT / "source" / "dota-map-trees.csv", grid)


def compute_args(start: int | None = None, end: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        database=DEFAULT_DB,
        overview_database=DEFAULT_OVERVIEW_DB,
        grid=str(RESOURCE_ROOT / "native-fow" / "dota_static_fow_grid.json"),
        cache=str(RESOURCE_ROOT / "native-fow" / "cache.fow"),
        occlusion_cells=None,
        start=start,
        end=end,
    )


@lru_cache(maxsize=1)
def map_config() -> dict:
    calibration_path = RESOURCE_ROOT / "calibration" / "projection_741_aerial_14pt.json"
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    grid_path = RESOURCE_ROOT / "native-fow" / "dota_static_fow_grid.json"
    grid_payload = json.loads(grid_path.read_text(encoding="utf-8"))
    origin = grid_payload.get("origin", [-9472.0, -9472.0])
    return {
        "imageUrl": "/resources/maps/7.41_map.png",
        "projectionCalibration": calibration,
        "visionGrid": {
            "originX": float(origin[0]),
            "originY": float(origin[1]),
            "cellSize": float(grid_payload.get("cell_size", 64.0)),
            "cellCenterOffset": 0.5,
        },
    }


def cache_input(payload: VisibilityRequest) -> dict:
    return {
        "version": CACHE_VERSION,
        "database": DEFAULT_DB,
        "overviewDatabase": DEFAULT_OVERVIEW_DB,
        "teamTag": payload.teamTag.strip().lower(),
        "matchIds": [int(match_id) for match_id in payload.matchIds],
        "start": payload.start,
        "end": payload.end,
        "compareBothSides": payload.compareBothSides,
    }


def cache_key(payload: VisibilityRequest) -> str:
    raw = json.dumps(cache_input(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def cache_path_for(payload: VisibilityRequest) -> Path:
    return CACHE_ROOT / f"{cache_key(payload)}.json"


def ward_value_cache_input(payload: WardValueRequest) -> dict:
    return {
        "version": WARD_VALUE_CACHE_VERSION,
        "database": DEFAULT_DB,
        "teamTag": payload.teamTag.strip().lower(),
        "matchIds": [int(match_id) for match_id in payload.matchIds],
        "start": payload.start,
        "end": payload.end,
        "clusterEps": payload.clusterEps,
    }


def ward_value_cache_key(payload: WardValueRequest) -> str:
    raw = json.dumps(ward_value_cache_input(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def ward_value_cache_path_for(payload: WardValueRequest) -> Path:
    return CACHE_ROOT / f"ward_value_{ward_value_cache_key(payload)}.json"


def ward_value_cache_meta(payload: WardValueRequest, hit: bool, path: Path) -> dict:
    return {
        "hit": hit,
        "key": ward_value_cache_key(payload),
        "path": project_path(path),
        "version": WARD_VALUE_CACHE_VERSION,
    }


def cache_meta(payload: VisibilityRequest, hit: bool, path: Path) -> dict:
    return {
        "hit": hit,
        "key": cache_key(payload),
        "path": project_path(path),
        "version": CACHE_VERSION,
    }


def read_cached_report(payload: VisibilityRequest) -> dict | None:
    path = cache_path_for(payload)
    if not path.exists():
        return None
    report = json.loads(path.read_text(encoding="utf-8"))
    report["cache"] = {
        **cache_meta(payload, True, path),
        "computedAt": report.get("cache", {}).get("computedAt"),
    }
    return report


def read_cached_ward_value_report(payload: WardValueRequest) -> dict | None:
    path = ward_value_cache_path_for(payload)
    if not path.exists():
        return None
    report = json.loads(path.read_text(encoding="utf-8"))
    report["cache"] = {
        **ward_value_cache_meta(payload, True, path),
        "computedAt": report.get("cache", {}).get("computedAt"),
    }
    return report


def write_cached_report(payload: VisibilityRequest, report: dict) -> None:
    path = cache_path_for(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    report["cache"] = {
        **cache_meta(payload, False, path),
        "computedAt": datetime.now(timezone.utc).isoformat(),
    }
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def write_cached_ward_value_report(payload: WardValueRequest, report: dict) -> None:
    path = ward_value_cache_path_for(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    report["cache"] = {
        **ward_value_cache_meta(payload, False, path),
        "computedAt": datetime.now(timezone.utc).isoformat(),
    }
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def match_ward_cache_input(match_id: int, start: int | None, end: int | None) -> dict:
    return {
        "version": MATCH_WARD_CACHE_VERSION,
        "matchId": int(match_id),
        "start": start,
        "end": end,
    }


def match_ward_cache_key(match_id: int, start: int | None, end: int | None) -> str:
    raw = json.dumps(
        match_ward_cache_input(match_id, start, end),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def match_ward_cache_path(match_id: int, start: int | None, end: int | None) -> Path:
    return CACHE_ROOT / f"match_ward_{match_ward_cache_key(match_id, start, end)}.json"


def match_ward_cache_exists(match_id: int, start: int | None, end: int | None) -> bool:
    return match_ward_cache_path(match_id, start, end).exists()


def _match_ward_lock(match_id: int, start: int | None, end: int | None) -> Lock:
    """按单场缓存 key 取锁，避免多个任务并发计算同一场比赛。"""
    key = match_ward_cache_key(match_id, start, end)
    with _MATCH_WARD_LOCKS_GUARD:
        lock = _MATCH_WARD_LOCKS.get(key)
        if lock is None:
            lock = Lock()
            _MATCH_WARD_LOCKS[key] = lock
        return lock


def read_match_ward_cache(match_id: int, start: int | None, end: int | None) -> dict | None:
    path = match_ward_cache_path(match_id, start, end)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# score_instances 需要、但 compact_instance 未保留的额外字段，单场缓存必须一并存下。
_MATCH_SCORE_EXTRA_KEYS = ("uniqueInvisibleHeroesCovered", "antiInvisEfficiency", "fastDewarded60")


def _serialize_match_instance(item: dict) -> dict:
    """单场缓存序列化：compact_instance + score 所需的额外字段。"""
    data = ward_value.compact_instance(item)
    for key in _MATCH_SCORE_EXTRA_KEYS:
        if key in item:
            data[key] = item[key]
    return data


def _ensure_score_fields(item: dict) -> dict:
    """兼容旧的残缺单场缓存：补齐 score_instances 所需字段，避免 KeyError。
    antiInvisEfficiency / fastDewarded60 可从已有字段精确重建；
    uniqueInvisibleHeroesCovered 无法从 compact 恢复，缺失时按 0 处理。"""
    if item.get("fastDewarded60") is None:
        life = item.get("lifetimeSeconds")
        item["fastDewarded60"] = bool(
            item.get("dewarded") is True and life is not None and life <= 60
        )
    if item.get("antiInvisEfficiency") is None:
        opportunity = item.get("antiInvisOpportunitySeconds") or 0
        true_sight = item.get("invisibleHeroTrueSightSeconds") or 0
        item["antiInvisEfficiency"] = (true_sight / opportunity) if opportunity else 0.0
    if item.get("uniqueInvisibleHeroesCovered") is None:
        item["uniqueInvisibleHeroesCovered"] = 0
    return item


def write_match_ward_cache(match_id: int, start: int | None, end: int | None, match: dict) -> None:
    write_match_ward_cache_payload(
        match_id,
        start,
        end,
        {
            "invisibility": match.get("invisibility"),
            "timeWindow": match.get("timeWindow"),
            "instances": [_serialize_match_instance(item) for item in match.get("instances") or []],
        },
    )


def write_match_ward_cache_payload(
    match_id: int,
    start: int | None,
    end: int | None,
    chunk: dict,
    *,
    migrated_from: str | None = None,
) -> None:
    path = match_ward_cache_path(match_id, start, end)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "matchId": int(match_id),
        "start": start,
        "end": end,
        "invisibility": chunk.get("invisibility") or {"available": False},
        "timeWindow": chunk.get("timeWindow") or {},
        "instances": chunk.get("instances") or [],
        "computedAt": datetime.now(timezone.utc).isoformat(),
    }
    if migrated_from:
        payload["migratedFrom"] = migrated_from
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(path)


def _instance_dedupe_key(item: dict) -> tuple:
    return (
        int(item.get("matchId") or 0),
        int(item.get("ehandle") or 0),
        int(item.get("start") or 0),
        str(item.get("team") or ""),
    )


def _strip_instance_for_match_cache(item: dict) -> dict:
    compact = _serialize_match_instance(item)
    for key in ("valueScore", "scoreBreakdown", "spotId"):
        compact.pop(key, None)
    return _ensure_score_fields(compact)


def extract_match_chunks_from_ward_value_report(report: dict) -> dict[int, dict]:
    match_meta = {
        int(match["matchId"]): match
        for match in (report.get("matches") or [])
        if match.get("matchId") is not None
    }
    by_match: dict[int, list[dict]] = defaultdict(list)
    for item in report.get("instances") or []:
        match_id = int(item.get("matchId") or 0)
        if match_id:
            by_match[match_id].append(_strip_instance_for_match_cache(item))
    chunks: dict[int, dict] = {}
    for match_id, instances in by_match.items():
        meta = match_meta.get(match_id, {})
        chunks[match_id] = {
            "invisibility": meta.get("invisibility") or {"available": False},
            "timeWindow": meta.get("timeWindow") or {},
            "instances": instances,
        }
    return chunks


def _merge_match_ward_chunks(existing: dict | None, chunk: dict) -> dict:
    merged: dict[tuple, dict] = {}
    for item in (existing or {}).get("instances", []) + (chunk.get("instances") or []):
        merged[_instance_dedupe_key(item)] = _strip_instance_for_match_cache(item)
    existing_inv = (existing or {}).get("invisibility") or {}
    chunk_inv = chunk.get("invisibility") or {}
    invisibility = chunk_inv if chunk_inv.get("available") else existing_inv or {"available": False}
    return {
        "invisibility": invisibility,
        "timeWindow": (existing or {}).get("timeWindow") or chunk.get("timeWindow") or {},
        "instances": list(merged.values()),
    }


def iter_team_ward_value_report_paths() -> list[tuple[int | None, int | None, Path, str]]:
    seen: set[str] = set()
    results: list[tuple[int | None, int | None, Path, str]] = []

    def add(start: int | None, end: int | None, path: Path, label: str) -> None:
        key = str(path)
        if key in seen or not path.exists():
            return
        seen.add(key)
        results.append((start, end, path, label))

    for team in list_prewarmed_teams():
        match_ids = team.get("matchIds") or []
        if not team.get("teamTag") or not match_ids:
            continue
        req = WardValueRequest(
            teamTag=str(team["teamTag"]),
            matchIds=[int(match_id) for match_id in match_ids],
            start=team.get("start"),
            end=team.get("end"),
            clusterEps=float(team.get("clusterEps") or 200),
        )
        add(req.start, req.end, ward_value_cache_path_for(req), f"manifest:{team['teamTag']}")

    for record in _load_prewarm_history().get("records", []):
        params = record.get("params") or {}
        teams = {t.get("teamTag"): t for t in (record.get("teams") or []) if t.get("teamTag")}
        for item in record.get("wardValue") or []:
            if item.get("status") != "ok":
                continue
            tag = item.get("teamTag")
            if not tag:
                continue
            team_info = teams.get(tag) or {}
            match_ids = item.get("matchIds") or team_info.get("matchIds") or []
            if not match_ids:
                continue
            req = WardValueRequest(
                teamTag=str(tag),
                matchIds=[int(match_id) for match_id in match_ids],
                start=params.get("start"),
                end=params.get("end"),
                clusterEps=float(params.get("clusterEps") or 200),
            )
            add(req.start, req.end, ward_value_cache_path_for(req), f"history:{tag}")

    for path in sorted(CACHE_ROOT.glob("ward_value_*.json")):
        add(None, None, path, "orphan")

    return results


def migrate_match_ward_cache_from_team_reports(*, dry_run: bool = False) -> dict:
    """把现有战队整包 ward_value 缓存拆成单场 match_ward 缓存，供增量复用。"""
    merged_pending: dict[tuple[int, int | None, int | None], dict] = {}
    sources_scanned = 0
    orphan_scanned = 0

    for start, end, path, label in iter_team_ward_value_report_paths():
        sources_scanned += 1
        if label == "orphan":
            orphan_scanned += 1
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for match_id, chunk in extract_match_chunks_from_ward_value_report(report).items():
            key = (match_id, start, end)
            merged_pending[key] = _merge_match_ward_chunks(merged_pending.get(key), chunk)

    created = 0
    updated = 0
    skipped = 0
    details: list[dict] = []
    for match_id, start, end in sorted(merged_pending.keys(), key=lambda item: (item[0], item[1] is not None, item[1] or 0, item[2] is not None, item[2] or 0)):
        chunk = merged_pending[(match_id, start, end)]
        existing = read_match_ward_cache(match_id, start, end)
        before_count = len((existing or {}).get("instances") or [])
        merged = _merge_match_ward_chunks(existing, chunk) if existing else chunk
        after_count = len(merged.get("instances") or [])
        if existing and after_count <= before_count:
            skipped += 1
            continue
        action = "updated" if existing else "created"
        if not dry_run:
            write_match_ward_cache_payload(
                match_id,
                start,
                end,
                merged,
                migrated_from="ward_value_split",
            )
        if existing:
            updated += 1
        else:
            created += 1
        if len(details) < 100:
            details.append({
                "matchId": match_id,
                "start": start,
                "end": end,
                "instances": after_count,
                "action": action,
            })

    manifest_bootstrapped = False
    if not dry_run and not (_load_prewarmed_teams_manifest().get("teams") or {}):
        for record in _load_prewarm_history().get("records", []):
            _sync_manifest_from_prewarm_report(record)
        manifest_bootstrapped = True

    return {
        "dryRun": dry_run,
        "sourcesScanned": sources_scanned,
        "orphanFilesScanned": orphan_scanned,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "totalMatches": len(merged_pending),
        "manifestBootstrapped": manifest_bootstrapped,
        "details": details,
    }


def load_or_compute_match_raw(
    cursor,
    match_id: int,
    args,
    grid,
    cache,
    tree_id_cells,
    *,
    force_refresh: bool = False,
    progress_callback=None,
) -> tuple[dict, bool]:
    """加载或计算单场眼位原始数据（含双方 instances，评分前）。单场结果可跨战队复用。"""
    if not force_refresh:
        cached = read_match_ward_cache(match_id, args.start, args.end)
        if cached is not None:
            return cached, True
    lock = _match_ward_lock(match_id, args.start, args.end)
    with lock:
        # double-check：等锁期间可能已有其他任务算完并写入缓存。
        if not force_refresh:
            cached = read_match_ward_cache(match_id, args.start, args.end)
            if cached is not None:
                return cached, True
        match_args = SimpleNamespace(
            **vars(args),
            team_side_filter=None,
            progress_callback=progress_callback,
            progress_step=getattr(args, "progress_step", 15),
        )
        match = ward_value.compute_match(cursor, int(match_id), match_args, grid, cache, tree_id_cells)
        write_match_ward_cache(match_id, args.start, args.end, match)
        fresh = read_match_ward_cache(match_id, args.start, args.end) or {
            "matchId": int(match_id),
            "invisibility": match.get("invisibility"),
            "timeWindow": match.get("timeWindow"),
            "instances": [_serialize_match_instance(item) for item in match.get("instances") or []],
        }
        return fresh, False


def _ward_match_summary(match_raw: dict, match_id: int, team_tag: str, team_side: str) -> dict:
    instances = match_raw.get("instances") or []
    filtered = [item for item in instances if item.get("team") == team_side]
    return {
        "matchId": int(match_id),
        "requestedTeamTag": team_tag,
        "requestedTeamSide": team_side,
        "invisibility": match_raw.get("invisibility") or {"available": False},
        "timeWindow": match_raw.get("timeWindow") or {},
        "wards": {
            "total": len(filtered),
            "observer": sum(1 for item in filtered if item.get("wardType") == "obs"),
            "sentry": sum(1 for item in filtered if item.get("wardType") == "sen"),
        },
    }


def visibility_report(payload: VisibilityRequest) -> dict:
    grid, cache = grid_and_cache()
    args = compute_args(payload.start, payload.end)
    with connect() as conn:
        with conn.cursor() as cursor:
            matches = []
            for match_id in payload.matchIds:
                requested = compute_match(cursor, match_id, payload.teamTag, args, grid, cache)
                matches.append(requested)
                if payload.compareBothSides:
                    match_info = load_match_info(cursor, match_id)
                    team_side = resolve_team_side(match_info, payload.teamTag)
                    opponent_tag = (
                        match_info.get("dire_team_tag")
                        if team_side == "radiant"
                        else match_info.get("radiant_team_tag")
                    )
                    matches.append(
                        compute_match(cursor, match_id, str(opponent_tag), args, grid, cache)
                    )
            comparisons = build_comparisons(matches) if payload.compareBothSides else []
    return {
        "source": {
            "database": DEFAULT_DB,
            "overviewDatabase": DEFAULT_OVERVIEW_DB,
            "grid": project_path(Path(args.grid)),
            "cache": project_path(Path(args.cache)),
            "map": map_config(),
            "rules": {
                "secondsMetric": "hero-seconds",
                "appearanceCount": "continuous visible segment per enemy hero; a one-second gap starts a new appearance",
                "aliveFilter": "latest hero_status_update.hp > 0 per match/time/slot",
                "normalHeroVision": "enemy hero native FoW cell is inside allied observer ward visible cells",
                "invisibleHeroVision": "normalHeroVision plus allied sentry 1000-unit radius",
                "invisibilityEvents": "combat_logs ADD is inclusive, REMOVE is exclusive",
                "timeWindowDefault": "-80 to match duration inclusive",
            },
        },
        "matches": matches,
        "comparisons": comparisons,
        "totals": {
            "heroSeconds": sum(match["metrics"]["heroSeconds"] for match in matches),
            "uniqueSeconds": sum(match["metrics"]["uniqueSeconds"] for match in matches),
            "appearances": sum(match["metrics"]["appearances"] for match in matches),
            "invisibleHeroSeconds": sum(match["metrics"]["invisibleHeroSeconds"] for match in matches),
            "observerContributionHeroSeconds": sum(
                match["metrics"].get("observerContributionHeroSeconds", 0)
                for match in matches
            ),
        },
    }


def ward_value_args(payload: WardValueRequest) -> SimpleNamespace:
    return SimpleNamespace(
        database=DEFAULT_DB,
        grid=str(RESOURCE_ROOT / "native-fow" / "dota_static_fow_grid.json"),
        cache=str(RESOURCE_ROOT / "native-fow" / "cache.fow"),
        tree_points=str(RESOURCE_ROOT / "source" / "dota-map-trees.csv"),
        map=str(RESOURCE_ROOT / "maps" / "7.41_map.png"),
        projection_calibration=str(RESOURCE_ROOT / "calibration" / "projection_741_aerial_14pt.json"),
        start=payload.start,
        end=payload.end,
        cluster_eps=payload.clusterEps,
    )


def ward_value_report(payload: WardValueRequest, progress=None) -> dict:
    """点位库计算入口：优先用多进程并行算各场比赛，进程池不可用时回退串行。"""
    pool = get_compute_pool()
    if pool is None:
        return _ward_value_report_serial(payload, progress)
    try:
        return _ward_value_report_parallel(payload, pool, progress)
    except BrokenProcessPool as exc:
        # 仅在进程池本身损坏（子进程被杀等）时降级串行，避免掩盖真实数据错误。
        # 损坏的池不会自愈，重置全局引用，让后续请求重建一个新池。
        global _COMPUTE_POOL
        with _COMPUTE_POOL_LOCK:
            if _COMPUTE_POOL is pool:
                try:
                    pool.shutdown(wait=False)
                except Exception:
                    pass
                _COMPUTE_POOL = None
        if progress:
            progress({"phase": "match", "message": f"并行计算不可用，回退串行：{exc}"})
        return _ward_value_report_serial(payload, progress)


def _persist_and_load_match_raw(match_id: int, args, worker_result: dict) -> dict:
    """把 worker 计算出的单场原始结果写入缓存，并读回序列化后的标准结构。

    与 load_or_compute_match_raw 的落盘/读回逻辑保持一致，确保并行/串行结果同构。
    """
    match = {
        "invisibility": worker_result.get("invisibility"),
        "timeWindow": worker_result.get("timeWindow"),
        "instances": worker_result.get("instances") or [],
    }
    write_match_ward_cache(match_id, args.start, args.end, match)
    return read_match_ward_cache(match_id, args.start, args.end) or {
        "matchId": int(match_id),
        "invisibility": match.get("invisibility"),
        "timeWindow": match.get("timeWindow"),
        "instances": [_serialize_match_instance(item) for item in match.get("instances") or []],
    }


def _finalize_ward_value_report(
    payload: WardValueRequest, args, matches: list[dict], all_instances: list[dict], progress=None
) -> dict:
    """串行/并行路径共用的收尾：评分、聚类、排行榜与报告组装。"""
    invisibility_available = all(
        (match.get("invisibility") or {}).get("available", False) for match in matches
    ) if matches else False
    if progress:
        progress({"phase": "scoring", "message": "正在评分和聚类点位", "percent": 96})
    ward_value.score_instances(all_instances, invisibility_available)
    instances = ward_value.finalize_instances(all_instances)
    spots = ward_value.cluster_spots(instances, payload.clusterEps)
    projection = ward_value.load_projection(Path(args.projection_calibration))
    for spot in spots:
        spot["pixel"] = ward_value.world_to_pixel(spot["centerWorldX"], spot["centerWorldY"], projection)
    compact_instances = [ward_value.compact_instance(item) for item in instances]
    leaderboards = ward_value.build_leaderboards(compact_instances, spots)
    spot_details = ward_value.build_spot_details(spots, instances)
    spot_leaderboards = build_spot_leaderboards(spot_details)
    if progress:
        progress({"phase": "finished", "message": "点位库计算完成", "percent": 100})
    return {
        "source": {
            "database": DEFAULT_DB,
            "teamTag": payload.teamTag,
            "grid": project_path(Path(args.grid)),
            "cache": project_path(Path(args.cache)),
            "treePoints": project_path(Path(args.tree_points)),
            "map": map_config(),
            "mapVersion": ward_value.MAP_VERSION,
            "observerRadius": ward_value.OBSERVER_RADIUS,
            "sentryRadius": ward_value.SENTRY_RADIUS,
            "clusterEpsWorld": payload.clusterEps,
            "invisibilityRule": "invisibility_modifier=true requires allied sentry coverage before observer sight counts",
        },
        "summary": {
            "matchCount": len(matches),
            "matches": [int(match_id) for match_id in payload.matchIds],
            "matchCacheHits": sum(1 for item in matches if item.get("matchCacheHit")),
            "instanceCount": len(instances),
            "observerCount": sum(1 for item in instances if item["wardType"] == "obs"),
            "sentryCount": sum(1 for item in instances if item["wardType"] == "sen"),
            "spotCount": len(spots),
            "invisibilityDataAvailable": invisibility_available,
        },
        "matches": matches,
        "instances": compact_instances,
        "spots": spots,
        "spotDetails": spot_details,
        "leaderboards": leaderboards,
        "spotLeaderboards": spot_leaderboards,
    }


def _ward_value_report_parallel(payload: WardValueRequest, pool: ProcessPoolExecutor, progress=None) -> dict:
    """多进程并行：各场比赛在独立进程内计算，主进程负责缓存读写与汇总。"""
    args = ward_value_args(payload)
    args_dict = dict(vars(args))
    db_cfg = db_config()
    match_ids = [int(match_id) for match_id in payload.matchIds]
    total_matches = len(match_ids)

    match_infos: dict[int, dict] = {}
    match_raw_by_id: dict[int, dict] = {}
    cache_hit_by_id: dict[int, bool] = {}
    misses: list[int] = []

    # 主进程一次性取各场 match_info（解析 team_side 用）并区分缓存命中/未命中。
    with connect() as conn:
        with conn.cursor() as cursor:
            for match_id in match_ids:
                try:
                    conn.ping(reconnect=True)
                except Exception:
                    pass
                match_infos[match_id] = ward_value.load_match_info(cursor, match_id)
                cached = None if payload.forceRefresh else read_match_ward_cache(match_id, args.start, args.end)
                if cached is not None:
                    match_raw_by_id[match_id] = cached
                    cache_hit_by_id[match_id] = True
                else:
                    misses.append(match_id)

    done = total_matches - len(misses)

    def emit_progress() -> None:
        if not progress:
            return
        progress({
            "phase": "match",
            "message": f"已完成 {done}/{total_matches} 场",
            "currentMatch": done,
            "totalMatches": total_matches,
            "percent": round(done / max(1, total_matches) * 100, 1),
        })

    emit_progress()

    if misses:
        futures = {
            pool.submit(match_worker.compute_match_raw, match_id, args_dict, db_cfg): match_id
            for match_id in misses
        }
        try:
            for future in as_completed(futures):
                match_id = futures[future]
                result = future.result()
                match_raw_by_id[match_id] = _persist_and_load_match_raw(match_id, args, result)
                cache_hit_by_id[match_id] = False
                done += 1
                emit_progress()
        except BaseException:
            for future in futures:
                future.cancel()
            raise

    matches: list[dict] = []
    all_instances: list[dict] = []
    for match_id in match_ids:
        match_raw = match_raw_by_id[match_id]
        team_side = resolve_team_side(match_infos[match_id], payload.teamTag)
        filtered_instances = [
            _ensure_score_fields({**item})
            for item in (match_raw.get("instances") or [])
            if item.get("team") == team_side
        ]
        match_summary = _ward_match_summary(match_raw, match_id, payload.teamTag, team_side)
        match_summary["matchCacheHit"] = cache_hit_by_id[match_id]
        matches.append(match_summary)
        all_instances.extend(filtered_instances)

    return _finalize_ward_value_report(payload, args, matches, all_instances, progress)


def _ward_value_report_serial(payload: WardValueRequest, progress=None) -> dict:
    grid, cache = grid_and_cache()
    tree_id_cells = ward_value_tree_cells()
    args = ward_value_args(payload)
    matches = []
    all_instances = []
    total_matches = len(payload.matchIds)
    with connect() as conn:
        with conn.cursor() as cursor:
            for match_index, match_id in enumerate(payload.matchIds):
                # 长时间预热会让 DB 连接被服务器/网络超时关闭（InterfaceError: (0, '')）。
                # 每场开始前 ping 一次，断线自动重连，保证长任务稳定。
                try:
                    conn.ping(reconnect=True)
                except Exception:
                    pass
                match_info = ward_value.load_match_info(cursor, int(match_id))
                team_side = resolve_team_side(match_info, payload.teamTag)
                if progress:
                    progress({
                        "phase": "match",
                        "message": f"正在计算第 {match_index + 1}/{total_matches} 场比赛",
                        "currentMatch": match_index + 1,
                        "totalMatches": total_matches,
                        "matchId": int(match_id),
                    })

                def match_progress(_match_id: int, second: int, start: int, end: int, *, index=match_index) -> None:
                    if not progress:
                        return
                    match_total = max(1, end - start + 1)
                    match_done = max(0, min(match_total, second - start + 1))
                    percent = round(((index + match_done / match_total) / max(1, total_matches)) * 100, 1)
                    progress({
                        "phase": "match",
                        "message": f"第 {index + 1}/{total_matches} 场，时间 {second}/{end}",
                        "currentMatch": index + 1,
                        "totalMatches": total_matches,
                        "matchId": int(_match_id),
                        "second": second,
                        "start": start,
                        "end": end,
                        "percent": percent,
                    })

                match_raw, cache_hit = load_or_compute_match_raw(
                    cursor,
                    int(match_id),
                    args,
                    grid,
                    cache,
                    tree_id_cells,
                    force_refresh=payload.forceRefresh,
                    progress_callback=match_progress,
                )
                filtered_instances = [
                    _ensure_score_fields({**item})
                    for item in (match_raw.get("instances") or [])
                    if item.get("team") == team_side
                ]
                match_summary = _ward_match_summary(match_raw, match_id, payload.teamTag, team_side)
                match_summary["matchCacheHit"] = cache_hit
                matches.append(match_summary)
                all_instances.extend(filtered_instances)

    return _finalize_ward_value_report(payload, args, matches, all_instances, progress)


def build_spot_leaderboards(spots: list[dict]) -> dict:
    def sample_count(spot: dict) -> int:
        return int(spot.get("sampleCount") or 0)

    def avg_score(spot: dict) -> float:
        return float(spot.get("avgScore") or 0)

    def deward_rate(spot: dict) -> float:
        value = spot.get("dewardRate")
        return -1.0 if value is None else float(value)

    def avg_life(spot: dict) -> float:
        return float(spot.get("avgLifetimeSeconds") or 0)

    def avg_true_sight(spot: dict) -> float:
        instances = spot.get("instances") or []
        if not instances:
            return 0.0
        return sum(float(item.get("invisibleHeroTrueSightSeconds") or 0) for item in instances) / len(instances)

    def avg_start(spot: dict) -> float:
        starts = [float(item.get("start") or 0) for item in spot.get("instances") or []]
        return sum(starts) / len(starts) if starts else 999999.0

    enough = [spot for spot in spots if sample_count(spot) >= 2]
    observer = [spot for spot in spots if spot.get("wardType") == "obs"]
    sentry = [spot for spot in spots if spot.get("wardType") == "sen"]
    opening = [spot for spot in spots if avg_start(spot) <= 600]
    return {
        "mostUsed": sorted(spots, key=lambda spot: (-sample_count(spot), -avg_score(spot), spot.get("spotId", "")))[:20],
        "stableHighValue": sorted(enough, key=lambda spot: (-avg_score(spot), -sample_count(spot), spot.get("spotId", "")))[:20],
        "highRisk": sorted(
            [spot for spot in enough if deward_rate(spot) >= 0],
            key=lambda spot: (-deward_rate(spot), avg_life(spot), -sample_count(spot)),
        )[:20],
        "opening": sorted(opening, key=lambda spot: (avg_start(spot), -sample_count(spot), -avg_score(spot)))[:20],
        "longLived": sorted(spots, key=lambda spot: (-avg_life(spot), -avg_score(spot), spot.get("spotId", "")))[:20],
        "observer": sorted(observer, key=lambda spot: (-avg_score(spot), -sample_count(spot), spot.get("spotId", "")))[:20],
        "sentryTrueSight": sorted(sentry, key=lambda spot: (-avg_true_sight(spot), -avg_score(spot), spot.get("spotId", "")))[:20],
    }


def build_comparisons(matches: list[dict]) -> list[dict]:
    by_match: dict[int, list[dict]] = {}
    for match in matches:
        by_match.setdefault(int(match["matchId"]), []).append(match)

    comparisons = []
    for match_id, items in by_match.items():
        if len(items) < 2:
            continue
        first, second = items[0], items[1]
        comparisons.append(
            {
                "matchId": match_id,
                "teams": [
                    {
                        "teamTag": first["teamTag"],
                        "side": first["teamSide"],
                        "metrics": first["metrics"],
                    },
                    {
                        "teamTag": second["teamTag"],
                        "side": second["teamSide"],
                        "metrics": second["metrics"],
                    },
                ],
                "delta": {
                    "heroSeconds": first["metrics"]["heroSeconds"] - second["metrics"]["heroSeconds"],
                    "uniqueSeconds": first["metrics"]["uniqueSeconds"] - second["metrics"]["uniqueSeconds"],
                    "appearances": first["metrics"]["appearances"] - second["metrics"]["appearances"],
                    "invisibleHeroSeconds": first["metrics"]["invisibleHeroSeconds"] - second["metrics"]["invisibleHeroSeconds"],
                    "observerContributionHeroSeconds": first["metrics"].get("observerContributionHeroSeconds", 0)
                    - second["metrics"].get("observerContributionHeroSeconds", 0),
                },
            }
        )
    return comparisons


def cached_visibility_report(payload: VisibilityRequest) -> dict:
    if not payload.forceRefresh:
        cached = read_cached_report(payload)
        if cached is not None:
            return cached
    report = visibility_report(payload)
    write_cached_report(payload, report)
    return report


def cached_ward_value_report(payload: WardValueRequest, progress=None) -> dict:
    if not payload.forceRefresh:
        cached = read_cached_ward_value_report(payload)
        if cached is not None:
            if progress:
                progress({"phase": "cache", "message": "已命中缓存", "percent": 100})
            return cached
    report = ward_value_report(payload, progress)
    write_cached_ward_value_report(payload, report)
    return report


def visibility_job_runner(payload: VisibilityRequest, progress) -> dict:
    progress({"phase": "computing", "message": "正在计算单场时间轴"})
    report = cached_visibility_report(payload)
    progress({"phase": "finished", "message": "单场时间轴计算完成", "percent": 100})
    return report


def ward_value_job_runner(payload: WardValueRequest, progress) -> dict:
    return cached_ward_value_report(payload, progress)


def comparison_job_runner(payload: TeamComparisonRequest, progress) -> dict:
    return cached_team_comparison_report(payload, progress)


def comparison_args(payload: TeamComparisonRequest) -> SimpleNamespace:
    return SimpleNamespace(
        database=DEFAULT_DB,
        grid=str(RESOURCE_ROOT / "native-fow" / "dota_static_fow_grid.json"),
        cache=str(RESOURCE_ROOT / "native-fow" / "cache.fow"),
        tree_points=str(RESOURCE_ROOT / "source" / "dota-map-trees.csv"),
        map=str(RESOURCE_ROOT / "maps" / "7.41_map.png"),
        projection_calibration=str(RESOURCE_ROOT / "calibration" / "projection_741_aerial_14pt.json"),
        start=payload.start,
        end=payload.end,
        cluster_eps=payload.clusterEps,
    )


def comparison_cache_input(payload: TeamComparisonRequest) -> dict:
    return {
        "version": COMPARISON_CACHE_VERSION,
        "database": DEFAULT_DB,
        "teams": [
            {
                "teamTag": team.teamTag.strip().lower(),
                "matchIds": sorted(int(match_id) for match_id in team.matchIds),
            }
            for team in payload.teams
        ],
        "start": payload.start,
        "end": payload.end,
        "clusterEps": payload.clusterEps,
    }


def comparison_cache_key(payload: TeamComparisonRequest) -> str:
    raw = json.dumps(comparison_cache_input(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def comparison_cache_path_for(payload: TeamComparisonRequest) -> Path:
    return CACHE_ROOT / f"team_comparison_{comparison_cache_key(payload)}.json"


def comparison_cache_meta(payload: TeamComparisonRequest, hit: bool, path: Path) -> dict:
    return {
        "hit": hit,
        "key": comparison_cache_key(payload),
        "path": project_path(path),
        "version": COMPARISON_CACHE_VERSION,
    }


def read_cached_comparison_report(payload: TeamComparisonRequest) -> dict | None:
    path = comparison_cache_path_for(payload)
    if not path.exists():
        return None
    report = json.loads(path.read_text(encoding="utf-8"))
    report["cache"] = {
        **comparison_cache_meta(payload, True, path),
        "computedAt": report.get("cache", {}).get("computedAt"),
    }
    return report


def write_cached_comparison_report(payload: TeamComparisonRequest, report: dict) -> None:
    path = comparison_cache_path_for(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    report["cache"] = {
        **comparison_cache_meta(payload, False, path),
        "computedAt": datetime.now(timezone.utc).isoformat(),
    }
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _team_summary(team_tag: str, instances: list[dict], spot_ids: set[str]) -> dict:
    observers = [item for item in instances if item["wardType"] == "obs"]
    sentries = [item for item in instances if item["wardType"] == "sen"]
    deward_known = [item for item in instances if item.get("dewarded") is not None]
    count = len(instances)
    return {
        "teamTag": team_tag,
        "instanceCount": count,
        "observerCount": len(observers),
        "sentryCount": len(sentries),
        "spotCount": len(spot_ids),
        "avgValueScore": round(_safe_div(sum(item["valueScore"] for item in instances), count), 2),
        "avgSeenSeconds": round(_safe_div(sum(item["enemyHeroSeenSeconds"] for item in instances), count), 2),
        "avgLifetimeSeconds": round(_safe_div(sum(item["lifetimeSeconds"] for item in instances), count), 2),
        "avgTrueSightSeconds": round(
            _safe_div(sum(item["invisibleHeroTrueSightSeconds"] for item in sentries), len(sentries)), 2
        ),
        "dewardRate": (
            round(_safe_div(sum(1 for item in deward_known if item["dewarded"]), len(deward_known)), 4)
            if deward_known
            else None
        ),
    }


def _summary_from_ward_value(team_tag: str, wv_report: dict, side: str | None = None) -> dict:
    """从单队点位库报告提取 KPI；side 为 radiant/dire 时只统计该阵营。"""
    instances = list(wv_report.get("instances") or [])
    spots = list(wv_report.get("spots") or [])
    if side in {"radiant", "dire"}:
        instances = [item for item in instances if item.get("team") == side]
        spots = [spot for spot in spots if spot.get("team") == side]
    spot_ids = {str(spot.get("spotId")) for spot in spots if spot.get("spotId")}
    summary = _team_summary(team_tag, instances, spot_ids)
    wv_summary = wv_report.get("summary") or {}
    summary["matchCount"] = wv_summary.get("matchCount")
    if side in {"radiant", "dire"}:
        summary["side"] = side
    return summary


def _top_spots_from_ward_value(wv_report: dict, side: str | None = None, limit: int = 10) -> list[dict]:
    spots = list(wv_report.get("spots") or [])
    if side in {"radiant", "dire"}:
        spots = [spot for spot in spots if spot.get("team") == side]
    ranked = sorted(
        spots,
        key=lambda spot: (-int(spot.get("sampleCount") or 0), -(float(spot.get("avgScore") or 0))),
    )
    return [
        {
            "spotId": spot.get("spotId"),
            "wardType": spot.get("wardType"),
            "team": spot.get("team"),
            "sampleCount": spot.get("sampleCount"),
            "matchCount": spot.get("matchCount"),
            "avgScore": spot.get("avgScore"),
            "avgSeenSeconds": spot.get("avgSeenSeconds"),
            "dewardRate": spot.get("dewardRate"),
            "pixel": spot.get("pixel"),
            "centerWorldX": spot.get("centerWorldX"),
            "centerWorldY": spot.get("centerWorldY"),
        }
        for spot in ranked[:limit]
    ]


def _instances_by_team(ward_value_by_team: dict[str, dict], team_tags: list[str]) -> dict[str, list[dict]]:
    """提取各队眼位实例（含布置时间 start），供前端按下眼时间/眼类型筛选。"""
    spot_pixels: dict[tuple[str, str], dict] = {}
    result: dict[str, list[dict]] = {}
    for index, tag in enumerate(team_tags):
        wv = ward_value_by_team[tag]
        for spot in wv.get("spots") or []:
            sid = spot.get("spotId")
            if sid:
                spot_pixels[(tag, str(sid))] = spot.get("pixel")
        items: list[dict] = []
        for inst in wv.get("instances") or []:
            sid = inst.get("spotId")
            items.append({
                "teamTag": tag,
                "teamIndex": index,
                "wardType": inst.get("wardType"),
                "team": inst.get("team"),
                "start": inst.get("start"),
                "spotId": sid,
                "valueScore": inst.get("valueScore"),
                "enemyHeroSeenSeconds": inst.get("enemyHeroSeenSeconds"),
                "lifetimeSeconds": inst.get("lifetimeSeconds"),
                "dewarded": inst.get("dewarded"),
                "invisibleHeroTrueSightSeconds": inst.get("invisibleHeroTrueSightSeconds"),
                "pixel": spot_pixels.get((tag, str(sid))) if sid else None,
            })
        result[tag] = items
    return result


def team_comparison_report(payload: TeamComparisonRequest, progress=None) -> dict:
    """横向对比：复用各队独立点位库，汇总 KPI / Top 点位 / 地图叠加。"""
    team_tags = [team.teamTag for team in payload.teams]
    team_match_ids: dict[str, list[int]] = {}
    ward_value_by_team: dict[str, dict] = {}
    total = len(payload.teams)

    for index, team in enumerate(payload.teams):
        team_match_ids[team.teamTag] = [int(match_id) for match_id in team.matchIds]
        if progress:
            progress({
                "phase": "team",
                "message": f"加载 {team.teamTag.upper()} 点位库 ({index + 1}/{total})",
                "percent": round(index / max(1, total) * 90, 1),
                "currentTeam": index + 1,
                "totalTeams": total,
            })
        wv_req = WardValueRequest(
            teamTag=team.teamTag,
            matchIds=team.matchIds,
            start=payload.start,
            end=payload.end,
            forceRefresh=payload.forceRefresh,
            clusterEps=payload.clusterEps,
        )
        ward_value_by_team[team.teamTag] = cached_ward_value_report(wv_req)

    team_summaries = [_summary_from_ward_value(tag, ward_value_by_team[tag]) for tag in team_tags]
    team_side_summaries: dict[str, list[dict]] = {
        side: [_summary_from_ward_value(tag, ward_value_by_team[tag], side) for tag in team_tags]
        for side in ("radiant", "dire")
    }

    top_spots_by_team = {
        tag: [dict(spot, teamTag=tag) for spot in _top_spots_from_ward_value(ward_value_by_team[tag])]
        for tag in team_tags
    }
    top_spots_by_team_side: dict[str, dict[str, list[dict]]] = {
        side: {
            tag: [dict(spot, teamTag=tag) for spot in _top_spots_from_ward_value(ward_value_by_team[tag], side)]
            for tag in team_tags
        }
        for side in ("radiant", "dire")
    }

    map_spots: list[dict] = []
    for team_index, tag in enumerate(team_tags):
        for spot in ward_value_by_team[tag].get("spots") or []:
            if not spot.get("pixel"):
                continue
            map_spots.append({
                "teamTag": tag,
                "teamIndex": team_index,
                "spotId": spot.get("spotId"),
                "wardType": spot.get("wardType"),
                "team": spot.get("team"),
                "sampleCount": spot.get("sampleCount"),
                "avgScore": spot.get("avgScore"),
                "pixel": spot.get("pixel"),
            })

    invisibility_available = all(
        bool((ward_value_by_team[tag].get("summary") or {}).get("invisibilityDataAvailable"))
        for tag in team_tags
    )
    instances_by_team = _instances_by_team(ward_value_by_team, team_tags)
    all_starts = [
        int(inst["start"])
        for items in instances_by_team.values()
        for inst in items
        if inst.get("start") is not None
    ]
    time_bounds = (
        {"min": min(all_starts), "max": max(all_starts)}
        if all_starts
        else {"min": 0, "max": 3600}
    )
    if progress:
        progress({"phase": "finished", "message": "横向对比汇总完成", "percent": 100})

    return {
        "source": {
            "database": DEFAULT_DB,
            "mode": "horizontal",
            "teamTags": team_tags,
            "teamMatchIds": team_match_ids,
            "map": map_config(),
            "mapVersion": ward_value.MAP_VERSION,
            "clusterEpsWorld": payload.clusterEps,
            "invisibilityDataAvailable": invisibility_available,
            "timeBounds": time_bounds,
            "timeFilterMode": "placement",
        },
        "summary": {
            "teamCount": len(team_tags),
            "mode": "horizontal",
            "totalMapSpots": len(map_spots),
        },
        "teams": team_summaries,
        "teamSideSummaries": team_side_summaries,
        "topSpotsByTeam": top_spots_by_team,
        "topSpotsByTeamSide": top_spots_by_team_side,
        "mapSpots": map_spots,
        "instancesByTeam": instances_by_team,
    }


def cached_team_comparison_report(payload: TeamComparisonRequest, progress=None) -> dict:
    if not payload.forceRefresh:
        cached = read_cached_comparison_report(payload)
        if cached is not None:
            if progress:
                progress({"phase": "cache", "message": "已命中缓存", "percent": 100})
            return cached
    report = team_comparison_report(payload, progress)
    write_cached_comparison_report(payload, report)
    return report


def resolve_recent_match_ids(cursor, team_tag: str, limit: int) -> list[int]:
    """复现 /api/matches?team_tag=X&limit=N 的排序，返回该战队最近 N 场 match_id。
    保持与前端“多战队对比”取比赛完全一致，从而命中缓存。"""
    query = f"""
SELECT CAST(mi.match_id AS BIGINT) AS matchId
FROM match_info mi
LEFT JOIN `{DEFAULT_OVERVIEW_DB}`.`dwd_match_overview` ov
  ON CAST(mi.match_id AS BIGINT)=ov.match_id
WHERE (LOWER(mi.radiant_team_tag)=LOWER(%s) OR LOWER(mi.dire_team_tag)=LOWER(%s))
ORDER BY COALESCE(ov.start_time, mi.end_time) DESC
LIMIT %s
"""
    cursor.execute(query, [team_tag, team_tag, limit])
    rows = list(cursor.fetchall())
    return [int(row["matchId"]) for row in rows]


def resolve_recent_matches_by_side(cursor, team_tag: str, limit: int, team_id: str | None = None) -> dict:
    """分别取该战队作为天辉/夜魇的最近 N 场，再按时间合并去重。
    保证两个阵营各有最多 N 场样本（天辉夜魇做眼逻辑不同）。
    若提供 team_id，则按 team_id 精确匹配（tag 不可靠时更准确）。"""
    tid = str(team_id or "").strip()
    use_id = tid not in ("", "0")

    def _query(side: str) -> list[dict]:
        if side == "radiant":
            id_col, tag_col = "radiant_team_id", "radiant_team_tag"
        else:
            id_col, tag_col = "dire_team_id", "dire_team_tag"
        if use_id:
            where = f"CAST(mi.{id_col} AS CHAR)=%s"
            arg = tid
        else:
            where = f"LOWER(mi.{tag_col})=LOWER(%s)"
            arg = team_tag
        query = f"""
SELECT CAST(mi.match_id AS BIGINT) AS matchId,
       COALESCE(ov.start_time, mi.end_time) AS ts
FROM match_info mi
LEFT JOIN `{DEFAULT_OVERVIEW_DB}`.`dwd_match_overview` ov
  ON CAST(mi.match_id AS BIGINT)=ov.match_id
WHERE {where}
ORDER BY COALESCE(ov.start_time, mi.end_time) DESC
LIMIT %s
"""
        cursor.execute(query, [arg, limit])
        return list(cursor.fetchall())

    radiant_rows = _query("radiant")
    dire_rows = _query("dire")
    radiant = [int(r["matchId"]) for r in radiant_rows]
    dire = [int(r["matchId"]) for r in dire_rows]

    merged: dict[int, int] = {}
    for r in radiant_rows + dire_rows:
        mid = int(r["matchId"])
        ts = int(r["ts"]) if r.get("ts") is not None else 0
        if mid not in merged or ts > merged[mid]:
            merged[mid] = ts
    combined = [mid for mid, _ in sorted(merged.items(), key=lambda kv: (-kv[1], -kv[0]))]

    return {
        "radiant": radiant,
        "dire": dire,
        "combined": combined,
        "radiantCount": len(radiant),
        "direCount": len(dire),
    }


def prewarm_report(payload: PrewarmRequest, progress=None) -> dict:
    team_ids = payload.teamIds or []
    tag_to_id: dict[str, str] = {}
    tags = []
    for idx, raw in enumerate(payload.teams):
        tag = (raw or "").strip()
        if not tag:
            continue
        tags.append(tag)
        tid = str(team_ids[idx]).strip() if idx < len(team_ids) and team_ids[idx] else ""
        if tid and tid not in ("0", "None"):
            tag_to_id.setdefault(tag.lower(), tid)
    seen: set[str] = set()
    unique_tags: list[str] = []
    for tag in tags:
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            unique_tags.append(tag)
    if not unique_tags:
        raise ValueError("请至少提供一个有效的战队 Tag。")

    if progress:
        progress({"phase": "resolving", "message": "正在解析各战队最近比赛", "percent": 0})

    resolved: list[dict] = []
    with connect() as conn:
        with conn.cursor() as cursor:
            for tag in unique_tags:
                tid = tag_to_id.get(tag.lower())
                sides = resolve_recent_matches_by_side(cursor, tag, payload.recent, tid)
                resolved.append({
                    "teamTag": tag,
                    "teamId": tid,
                    "matchIds": sides["combined"],
                    "radiantCount": sides["radiantCount"],
                    "direCount": sides["direCount"],
                })

    do_ward_value = payload.includeWardValue
    do_comparison = payload.includeComparison and len([t for t in resolved if t["matchIds"]]) >= 2
    total_steps = (len(resolved) if do_ward_value else 0) + (1 if do_comparison else 0)
    total_steps = max(1, total_steps)
    step_index = 0

    def sub_progress(label: str):
        def cb(p: dict) -> None:
            if not progress:
                return
            inner = p.get("percent")
            frac = (float(inner) / 100.0) if isinstance(inner, (int, float)) else 0.0
            overall = round((step_index + min(1.0, max(0.0, frac))) / total_steps * 100, 1)
            progress({
                "phase": "prewarm",
                "message": f"{label}｜{p.get('message', '')}",
                "percent": overall,
                "step": step_index + 1,
                "totalSteps": total_steps,
            })
        return cb

    ward_value_results: list[dict] = []
    if do_ward_value:
        for team in resolved:
            label = f"点位库 {team['teamTag'].upper()}"
            if not team["matchIds"]:
                ward_value_results.append({
                    "teamTag": team["teamTag"],
                    "matchIds": [],
                    "status": "skipped",
                    "reason": "没有查到该战队的比赛",
                })
                step_index += 1
                continue
            if progress:
                progress({
                    "phase": "prewarm",
                    "message": f"开始 {label}",
                    "percent": round(step_index / total_steps * 100, 1),
                    "step": step_index + 1,
                    "totalSteps": total_steps,
                })
            wv_req = WardValueRequest(
                teamTag=team["teamTag"],
                matchIds=team["matchIds"],
                start=payload.start,
                end=payload.end,
                forceRefresh=payload.forceRefresh,
                clusterEps=payload.clusterEps,
            )
            try:
                report = cached_ward_value_report(wv_req, sub_progress(label))
            except Exception as exc:
                # 单队失败不拖垮整批：记录错误并继续下一队，已完成的队保持不变。
                ward_value_results.append({
                    "teamTag": team["teamTag"],
                    "matchIds": team["matchIds"],
                    "status": "error",
                    "reason": f"{type(exc).__name__}: {exc}",
                })
                step_index += 1
                continue
            summary = report.get("summary") or {}
            ward_value_results.append({
                "teamTag": team["teamTag"],
                "matchIds": team["matchIds"],
                "status": "ok",
                "cacheHit": bool((report.get("cache") or {}).get("hit")),
                "spotCount": summary.get("spotCount"),
                "instanceCount": summary.get("instanceCount"),
            })
            # 每队算完立即写入 manifest，中途失败也不会丢失已完成的战队。
            upsert_prewarmed_team_entry({
                "teamTag": team["teamTag"],
                "teamId": team.get("teamId"),
                "matchIds": team["matchIds"],
                "radiantCount": team.get("radiantCount"),
                "direCount": team.get("direCount"),
                "recent": payload.recent,
                "start": payload.start,
                "end": payload.end,
                "clusterEps": payload.clusterEps,
                "spotCount": summary.get("spotCount"),
                "instanceCount": summary.get("instanceCount"),
                "updatedAt": utc_now_iso(),
            })
            step_index += 1

    comparison_result = None
    if do_comparison:
        cmp_teams = [team for team in resolved if team["matchIds"]]
        label = "多战队对比"
        if progress:
            progress({
                "phase": "prewarm",
                "message": f"开始 {label}",
                "percent": round(step_index / total_steps * 100, 1),
                "step": step_index + 1,
                "totalSteps": total_steps,
            })
        cmp_req = TeamComparisonRequest(
            teams=[ComparisonTeam(teamTag=team["teamTag"], matchIds=team["matchIds"]) for team in cmp_teams],
            start=payload.start,
            end=payload.end,
            forceRefresh=payload.forceRefresh,
            clusterEps=payload.clusterEps,
        )
        try:
            report = cached_team_comparison_report(cmp_req)
            comparison_result = {
                "teams": [{"teamTag": team["teamTag"], "matchIds": team["matchIds"]} for team in cmp_teams],
                "status": "ok",
                "cacheHit": bool((report.get("cache") or {}).get("hit")),
                "spotCount": (report.get("summary") or {}).get("totalMapSpots"),
            }
        except Exception as exc:
            # 对比失败不影响已保存的各队点位库。
            comparison_result = {
                "teams": [{"teamTag": team["teamTag"], "matchIds": team["matchIds"]} for team in cmp_teams],
                "status": "error",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        step_index += 1

    if progress:
        progress({"phase": "finished", "message": "预热完成", "percent": 100})

    report = {
        "params": {
            "recent": payload.recent,
            "start": payload.start,
            "end": payload.end,
            "clusterEps": payload.clusterEps,
            "forceRefresh": payload.forceRefresh,
            "includeWardValue": payload.includeWardValue,
            "includeComparison": payload.includeComparison,
        },
        "teams": resolved,
        "wardValue": ward_value_results,
        "comparison": comparison_result,
    }
    saved = save_prewarm_record(report)
    report["recordId"] = saved.get("id")
    report["createdAt"] = saved.get("createdAt")
    return report


def prewarm_job_runner(payload: PrewarmRequest, progress) -> dict:
    return prewarm_report(payload, progress)


def _prewarm_history_path() -> Path:
    return CACHE_ROOT / PREWARM_HISTORY_FILE


def _load_prewarm_history() -> dict:
    path = _prewarm_history_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("records"), list):
                return data
        except Exception:
            pass
    return {"records": []}


def _save_prewarm_history(data: dict) -> None:
    path = _prewarm_history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)
    except Exception:
        pass


def _prewarm_signature(report: dict) -> str:
    params = report.get("params") or {}
    teams = report.get("teams") or []
    team_keys = sorted(
        f"{(t.get('teamTag') or '').lower()}:{t.get('teamId') or ''}"
        for t in teams
    )
    payload = {
        "teams": team_keys,
        "recent": params.get("recent"),
        "start": params.get("start"),
        "end": params.get("end"),
        "clusterEps": params.get("clusterEps"),
        "includeWardValue": params.get("includeWardValue"),
        "includeComparison": params.get("includeComparison"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def save_prewarm_record(report: dict) -> dict:
    """持久化预热结果，相同战队+参数组合会更新而非重复追加。"""
    with PREWARM_HISTORY_LOCK:
        data = _load_prewarm_history()
        records: list[dict] = data["records"]
        sig = _prewarm_signature(report)
        record = {
            "id": uuid.uuid4().hex[:12],
            "signature": sig,
            "createdAt": utc_now_iso(),
            **report,
        }
        records = [r for r in records if r.get("signature") != sig]
        records.insert(0, record)
        data["records"] = records[:MAX_PREWARM_HISTORY]
        _save_prewarm_history(data)
        _sync_manifest_from_prewarm_report(report)
        return record


def _prewarmed_teams_manifest_path() -> Path:
    return CACHE_ROOT / PREWARMED_TEAMS_MANIFEST_FILE


def _load_prewarmed_teams_manifest() -> dict:
    path = _prewarmed_teams_manifest_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                teams = data.get("teams")
                if isinstance(teams, dict):
                    return {"teams": teams}
        except Exception:
            pass
    return {"teams": {}}


def _save_prewarmed_teams_manifest(data: dict) -> None:
    path = _prewarmed_teams_manifest_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)
    except Exception:
        pass


def upsert_prewarmed_team_entry(entry: dict) -> None:
    tag = (entry.get("teamTag") or "").strip()
    if not tag:
        return
    with PREWARMED_TEAMS_LOCK:
        data = _load_prewarmed_teams_manifest()
        data.setdefault("teams", {})[tag.lower()] = entry
        _save_prewarmed_teams_manifest(data)


def _sync_manifest_from_prewarm_report(report: dict) -> None:
    params = report.get("params") or {}
    teams_map = {t.get("teamTag"): t for t in (report.get("teams") or []) if t.get("teamTag")}
    for item in report.get("wardValue") or []:
        if item.get("status") != "ok":
            continue
        tag = item.get("teamTag")
        if not tag:
            continue
        team_info = teams_map.get(tag) or {}
        upsert_prewarmed_team_entry({
            "teamTag": tag,
            "teamId": team_info.get("teamId"),
            "matchIds": item.get("matchIds") or team_info.get("matchIds") or [],
            "radiantCount": team_info.get("radiantCount"),
            "direCount": team_info.get("direCount"),
            "recent": params.get("recent"),
            "start": params.get("start"),
            "end": params.get("end"),
            "clusterEps": params.get("clusterEps"),
            "spotCount": item.get("spotCount"),
            "instanceCount": item.get("instanceCount"),
            "updatedAt": utc_now_iso(),
        })


def _find_prewarmed_team_entry(team_tag: str) -> dict | None:
    key = team_tag.strip().lower()
    manifest = _load_prewarmed_teams_manifest()
    entry = (manifest.get("teams") or {}).get(key)
    if entry:
        return entry
    for item in list_prewarmed_teams_from_history():
        if str(item.get("teamTag") or "").lower() == key:
            return item
    return None


def fetch_match_overviews(cursor, match_ids: list[int]) -> dict[int, dict]:
    if not match_ids:
        return {}
    placeholders = ",".join(["%s"] * len(match_ids))
    query = f"""
SELECT
  CAST(mi.match_id AS BIGINT) AS matchId,
  mi.radiant_team_tag AS radiantTeamTag,
  mi.dire_team_tag AS direTeamTag,
  ov.league_name AS leagueName,
  ov.start_date AS startDate
FROM match_info mi
LEFT JOIN `{DEFAULT_OVERVIEW_DB}`.`dwd_match_overview` ov
  ON CAST(mi.match_id AS BIGINT)=ov.match_id
WHERE mi.match_id IN ({placeholders})
"""
    cursor.execute(query, match_ids)
    result: dict[int, dict] = {}
    for row in cursor.fetchall():
        mid = int(row["matchId"])
        if row.get("startDate") is not None:
            row["startDate"] = str(row["startDate"])
        result[mid] = row
    return result


def get_prewarm_team_detail(team_tag: str) -> dict:
    entry = _find_prewarmed_team_entry(team_tag)
    if not entry:
        raise ValueError(f"找不到战队 {team_tag} 的预热记录")

    recent = int(entry.get("recent") or 10)
    start = entry.get("start")
    end = entry.get("end")
    team_id = entry.get("teamId")
    stored_ids = {int(mid) for mid in (entry.get("matchIds") or [])}

    with connect() as conn:
        with conn.cursor() as cursor:
            sides = resolve_recent_matches_by_side(cursor, entry["teamTag"], recent, team_id)
            overviews = fetch_match_overviews(cursor, sides["combined"])

    matches: list[dict] = []
    for side in ("radiant", "dire"):
        for mid in sides[side]:
            computed = match_ward_cache_exists(mid, start, end)
            ov = overviews.get(mid, {})
            opponent = ov.get("direTeamTag") if side == "radiant" else ov.get("radiantTeamTag")
            matches.append({
                "matchId": mid,
                "teamSide": side,
                "status": "computed" if computed else "pending",
                "computed": computed,
                "inStoredBatch": mid in stored_ids,
                "startDate": ov.get("startDate"),
                "leagueName": ov.get("leagueName"),
                "opponentTag": opponent,
            })

    computed_count = sum(1 for item in matches if item["computed"])
    pending_count = sum(1 for item in matches if not item["computed"])
    return {
        **entry,
        "currentMatchIds": sides["combined"],
        "currentRadiantCount": sides["radiantCount"],
        "currentDireCount": sides["direCount"],
        "computedCount": computed_count,
        "pendingCount": pending_count,
        "matches": matches,
    }


def refresh_prewarmed_team(payload: RefreshPrewarmTeamRequest, progress=None) -> dict:
    entry = _find_prewarmed_team_entry(payload.teamTag)
    if not entry:
        raise ValueError(f"找不到战队 {payload.teamTag} 的预热记录")

    recent = int(entry.get("recent") or 10)
    start = entry.get("start")
    end = entry.get("end")
    team_id = entry.get("teamId")
    cluster_eps = float(entry.get("clusterEps") or 200.0)
    team_tag = entry["teamTag"]

    if progress:
        progress({"phase": "resolving", "message": "正在解析当前最近比赛", "percent": 5})

    with connect() as conn:
        with conn.cursor() as cursor:
            sides = resolve_recent_matches_by_side(cursor, team_tag, recent, team_id)

    if not sides["combined"]:
        raise ValueError(f"战队 {team_tag} 当前没有可用比赛")

    if progress:
        progress({"phase": "ward-value", "message": "正在增量计算点位库", "percent": 10})

    wv_req = WardValueRequest(
        teamTag=team_tag,
        matchIds=sides["combined"],
        start=start,
        end=end,
        forceRefresh=payload.forceRefresh,
        clusterEps=cluster_eps,
    )
    report = cached_ward_value_report(wv_req, progress)
    summary = report.get("summary") or {}
    cache_meta = report.get("cache") or {}

    upsert_prewarmed_team_entry({
        "teamTag": team_tag,
        "teamId": team_id,
        "matchIds": sides["combined"],
        "radiantCount": sides["radiantCount"],
        "direCount": sides["direCount"],
        "recent": recent,
        "start": start,
        "end": end,
        "clusterEps": cluster_eps,
        "spotCount": summary.get("spotCount"),
        "instanceCount": summary.get("instanceCount"),
        "updatedAt": utc_now_iso(),
    })

    detail = get_prewarm_team_detail(team_tag)
    detail["refresh"] = {
        "teamCacheHit": bool(cache_meta.get("hit")),
        "matchCacheHits": summary.get("matchCacheHits"),
        "matchCount": summary.get("matchCount"),
    }
    if progress:
        progress({"phase": "finished", "message": "补算完成", "percent": 100})
    return detail


def refresh_prewarm_team_runner(payload: RefreshPrewarmTeamRequest, progress) -> dict:
    return refresh_prewarmed_team(payload, progress)


def summarize_prewarm_record(record: dict) -> dict:
    teams = record.get("teams") or []
    params = record.get("params") or {}
    ward_value = record.get("wardValue") or []
    comparison = record.get("comparison")
    return {
        "id": record.get("id"),
        "createdAt": record.get("createdAt"),
        "teamTags": [t.get("teamTag") for t in teams if t.get("teamTag")],
        "teamIds": [t.get("teamId") for t in teams],
        "recent": params.get("recent"),
        "start": params.get("start"),
        "end": params.get("end"),
        "clusterEps": params.get("clusterEps"),
        "wardValueOk": sum(1 for item in ward_value if item.get("status") == "ok"),
        "hasComparison": bool(comparison and comparison.get("status") == "ok"),
        "comparisonTeams": [
            t.get("teamTag") for t in (comparison.get("teams") or []) if t.get("teamTag")
        ] if comparison else [],
    }


def list_prewarmed_teams() -> list[dict]:
    """已预热战队列表，优先读 manifest，否则从历史记录聚合。"""
    manifest = _load_prewarmed_teams_manifest()
    teams = list((manifest.get("teams") or {}).values())
    if teams:
        return sorted(teams, key=lambda e: str(e.get("updatedAt") or ""), reverse=True)
    return list_prewarmed_teams_from_history()


def list_prewarmed_teams_from_history() -> list[dict]:
    """跨所有历史记录聚合「点位库已算好」的战队，同一队保留最新一次。"""
    data = _load_prewarm_history()
    by_team: dict[str, dict] = {}
    for record in data.get("records", []):
        params = record.get("params") or {}
        teams = {t.get("teamTag"): t for t in (record.get("teams") or []) if t.get("teamTag")}
        created_at = record.get("createdAt")
        for item in record.get("wardValue") or []:
            if item.get("status") != "ok":
                continue
            tag = item.get("teamTag")
            if not tag:
                continue
            team_info = teams.get(tag) or {}
            entry = {
                "teamTag": tag,
                "teamId": team_info.get("teamId"),
                "matchIds": item.get("matchIds") or team_info.get("matchIds") or [],
                "radiantCount": team_info.get("radiantCount"),
                "direCount": team_info.get("direCount"),
                "recent": params.get("recent"),
                "start": params.get("start"),
                "end": params.get("end"),
                "clusterEps": params.get("clusterEps"),
                "spotCount": item.get("spotCount"),
                "instanceCount": item.get("instanceCount"),
                "updatedAt": created_at,
            }
            key = tag.lower()
            existing = by_team.get(key)
            if existing is None or str(entry["updatedAt"] or "") > str(existing["updatedAt"] or ""):
                by_team[key] = entry
    return sorted(by_team.values(), key=lambda e: str(e["updatedAt"] or ""), reverse=True)


def _team_logo_cache_path() -> Path:
    return CACHE_ROOT / TEAM_LOGO_CACHE_FILE


def _load_team_logo_cache() -> dict:
    global _TEAM_LOGO_CACHE
    if _TEAM_LOGO_CACHE is not None:
        return _TEAM_LOGO_CACHE
    path = _team_logo_cache_path()
    data = {"byTag": {}, "byId": {}}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data["byTag"] = loaded.get("byTag", {}) or {}
                data["byId"] = loaded.get("byId", {}) or {}
        except Exception:
            pass
    _TEAM_LOGO_CACHE = data
    return data


def _save_team_logo_cache() -> None:
    if _TEAM_LOGO_CACHE is None:
        return
    path = _team_logo_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(_TEAM_LOGO_CACHE, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(path)
    except Exception:
        pass


def resolve_team_id(cursor, team_tag: str) -> str | None:
    """从 match_info 里按 team_tag 找一个非空 team_id（取出现最多的）。"""
    query = """
SELECT team_id, COUNT(*) AS c FROM (
  SELECT radiant_team_id AS team_id FROM match_info
    WHERE LOWER(radiant_team_tag)=LOWER(%s) AND radiant_team_id IS NOT NULL AND radiant_team_id NOT IN ('0','')
  UNION ALL
  SELECT dire_team_id AS team_id FROM match_info
    WHERE LOWER(dire_team_tag)=LOWER(%s) AND dire_team_id IS NOT NULL AND dire_team_id NOT IN ('0','')
) t
GROUP BY team_id ORDER BY c DESC LIMIT 1
"""
    cursor.execute(query, [team_tag, team_tag])
    row = cursor.fetchone()
    if not row:
        return None
    team_id = str(row.get("team_id") or "").strip()
    return team_id or None


def fetch_team_info(team_id: str) -> dict | None:
    """调用 Valve 官方 webapi 获取战队信息。失败返回 None。"""
    url = f"https://www.dota2.com/webapi/IDOTA2Teams/GetSingleTeamInfo/v001?team_id={urllib.parse.quote(str(team_id))}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (dota2-vision)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def get_team_logos(tags: list[str]) -> dict:
    """返回 {tag_lower: {teamTag, teamId, name, logoUrl}}，带文件缓存。"""
    result: dict[str, dict] = {}
    tags_clean: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        norm = str(tag or "").strip()
        low = norm.lower()
        if norm and low not in seen:
            seen.add(low)
            tags_clean.append(norm)
    if not tags_clean:
        return result

    with TEAM_LOGO_LOCK:
        cache = _load_team_logo_cache()
        by_tag = cache["byTag"]
        by_id = cache["byId"]
        dirty = False
        need_resolve: list[str] = []

        for tag in tags_clean:
            low = tag.lower()
            entry = by_tag.get(low)
            if entry is not None:
                team_id = entry.get("teamId")
                info = by_id.get(str(team_id)) if team_id else None
                result[low] = {
                    "teamTag": tag,
                    "teamId": team_id,
                    "name": (info or {}).get("name") if info else entry.get("name"),
                    "logoUrl": (info or {}).get("logoUrl") if info else entry.get("logoUrl"),
                }
            else:
                need_resolve.append(tag)

        if need_resolve:
            conn = None
            cursor = None
            try:
                conn = connect()
                cursor = conn.cursor()
            except Exception:
                cursor = None
            for tag in need_resolve:
                low = tag.lower()
                team_id = None
                if cursor is not None:
                    try:
                        team_id = resolve_team_id(cursor, tag)
                    except Exception:
                        team_id = None
                name = None
                logo_url = None
                if team_id:
                    cached_info = by_id.get(str(team_id))
                    if cached_info:
                        name = cached_info.get("name")
                        logo_url = cached_info.get("logoUrl")
                    else:
                        info = fetch_team_info(team_id)
                        if info:
                            name = info.get("name") or None
                            logo_url = info.get("url_logo") or None
                            by_id[str(team_id)] = {
                                "name": name,
                                "logoUrl": logo_url,
                                "tag": info.get("tag") or tag,
                            }
                            dirty = True
                by_tag[low] = {"teamId": team_id, "name": name, "logoUrl": logo_url}
                dirty = True
                result[low] = {"teamTag": tag, "teamId": team_id, "name": name, "logoUrl": logo_url}
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        if dirty:
            _save_team_logo_cache()

    return result


def _team_directory_cache_path() -> Path:
    return CACHE_ROOT / TEAM_DIRECTORY_CACHE_FILE


def _load_team_directory_cache() -> dict | None:
    global _TEAM_DIRECTORY_CACHE
    if _TEAM_DIRECTORY_CACHE is not None:
        return _TEAM_DIRECTORY_CACHE
    path = _team_directory_cache_path()
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("teams"), list):
                _TEAM_DIRECTORY_CACHE = loaded
                return _TEAM_DIRECTORY_CACHE
        except Exception:
            pass
    return None


def _save_team_directory_cache(data: dict) -> None:
    global _TEAM_DIRECTORY_CACHE
    _TEAM_DIRECTORY_CACHE = data
    path = _team_directory_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(path)
    except Exception:
        pass


def _aggregate_teams(cursor) -> list[dict]:
    """从 match_info 汇总所有战队：按 team_id 归并，取出现最多的 tag 与比赛场数。
    team_id 为 0/空的（个人/无战队局）按 tag 归并。"""
    query = """
SELECT team_id, tag, COUNT(*) AS c FROM (
  SELECT CAST(radiant_team_id AS CHAR) AS team_id, radiant_team_tag AS tag FROM match_info
  UNION ALL
  SELECT CAST(dire_team_id AS CHAR) AS team_id, dire_team_tag AS tag FROM match_info
) t
GROUP BY team_id, tag
"""
    cursor.execute(query)
    rows = list(cursor.fetchall())
    by_id: dict[str, dict] = {}
    for row in rows:
        team_id = str(row.get("team_id") or "").strip()
        tag = (row.get("tag") or "").strip()
        count = int(row.get("c") or 0)
        has_id = team_id not in ("", "0")
        key = team_id if has_id else f"tag::{tag.lower()}"
        if not has_id and not tag:
            continue
        entry = by_id.get(key)
        if entry is None:
            entry = {"teamId": team_id if has_id else None, "matchCount": 0, "_tags": {}}
            by_id[key] = entry
        entry["matchCount"] += count
        if tag:
            entry["_tags"][tag] = entry["_tags"].get(tag, 0) + count
    teams: list[dict] = []
    for entry in by_id.values():
        tags = entry.pop("_tags", {})
        dominant_tag = max(tags.items(), key=lambda kv: kv[1])[0] if tags else ""
        entry["tag"] = dominant_tag
        teams.append(entry)
    teams.sort(key=lambda e: e["matchCount"], reverse=True)
    return teams


def build_team_directory(force: bool = False, enrich_top: int = 160) -> dict:
    """构建战队目录（tag / team_id / 场数 / 官方名 / logo），带磁盘缓存。
    官方名来自 Valve webapi（复用 logo 缓存），只为出场较多的战队补齐，避免过多外部请求。"""
    now = int(datetime.now(timezone.utc).timestamp())
    with TEAM_DIRECTORY_LOCK:
        if not force:
            cached = _load_team_directory_cache()
            if cached and now - int(cached.get("builtAt") or 0) < TEAM_DIRECTORY_TTL:
                return cached

        conn = connect()
        try:
            with conn.cursor() as cursor:
                teams = _aggregate_teams(cursor)
        finally:
            conn.close()

        # 复用 logo 缓存里的官方名，缺失的（仅出场较多的）并行补齐
        with TEAM_LOGO_LOCK:
            logo_cache = _load_team_logo_cache()
            by_id_logo = logo_cache["byId"]

        to_fetch: list[str] = []
        for idx, team in enumerate(teams):
            tid = team.get("teamId")
            info = by_id_logo.get(str(tid)) if tid else None
            if info:
                team["name"] = info.get("name") or team["tag"]
                team["logoUrl"] = info.get("logoUrl")
            elif tid and idx < enrich_top:
                to_fetch.append(str(tid))
                team["name"] = team["tag"]
                team["logoUrl"] = None
            else:
                team["name"] = team["tag"]
                team["logoUrl"] = None

        if to_fetch:
            def _fetch(tid: str):
                info = fetch_team_info(tid)
                if not info:
                    return tid, None
                return tid, {
                    "name": info.get("name") or None,
                    "logoUrl": info.get("url_logo") or None,
                    "tag": info.get("tag") or None,
                }

            fetched: dict[str, dict | None] = {}
            with ThreadPoolExecutor(max_workers=8) as pool:
                for tid, info in pool.map(_fetch, to_fetch):
                    fetched[tid] = info

            with TEAM_LOGO_LOCK:
                logo_cache = _load_team_logo_cache()
                by_id_logo = logo_cache["byId"]
                for tid, info in fetched.items():
                    if info:
                        by_id_logo[str(tid)] = info
                _save_team_logo_cache()

            id_to_team = {str(t.get("teamId")): t for t in teams if t.get("teamId")}
            for tid, info in fetched.items():
                team = id_to_team.get(str(tid))
                if team and info:
                    if info.get("name"):
                        team["name"] = info["name"]
                    team["logoUrl"] = info.get("logoUrl")

        data = {"builtAt": now, "teams": teams}
        _save_team_directory_cache(data)
        return data


def search_teams(query_text: str, limit: int = 20) -> list[dict]:
    q = (query_text or "").strip().lower()
    directory = build_team_directory()
    teams = directory.get("teams", [])
    if not q:
        return teams[:limit]
    exact: list[dict] = []
    starts: list[dict] = []
    contains: list[dict] = []
    for team in teams:
        name = (team.get("name") or "").lower()
        tag = (team.get("tag") or "").lower()
        if q == name or q == tag:
            exact.append(team)
        elif name.startswith(q) or tag.startswith(q):
            starts.append(team)
        elif q in name or q in tag:
            contains.append(team)
    return (exact + starts + contains)[:limit]


app = FastAPI(title="Dota Ward Vision Query")


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "detail": f"internal server error: {type(exc).__name__}: {exc}",
        },
    )


@app.get("/api/health")
def health() -> dict:
    cfg = db_config()
    # 检查关键资源文件
    critical_files = {
        "cache_fow": RESOURCE_ROOT / "native-fow" / "cache.fow",
        "fow_grid": RESOURCE_ROOT / "native-fow" / "dota_static_fow_grid.json",
        "map": RESOURCE_ROOT / "maps" / "7.41_map.png",
        "calibration": RESOURCE_ROOT / "calibration" / "projection_741_aerial_14pt.json",
        "tree_csv": RESOURCE_ROOT / "source" / "dota-map-trees.csv",
    }
    files = {}
    all_ok = True
    for name, path in critical_files.items():
        exists = path.exists()
        size = path.stat().st_size if exists else 0
        files[name] = {
            "exists": exists,
            "size": size,
            "path": project_path(path),
            "lfs_pointer": exists and size < 200 and size > 0,
        }
        if not exists:
            all_ok = False
    # 测试数据库连接
    db_ok = False
    db_error = None
    if cfg["user"] and cfg["password"]:
        try:
            conn = connect()
            conn.close()
            db_ok = True
        except Exception as e:
            db_error = str(e)
    cache_writable = False
    cache_error = None
    try:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        probe = CACHE_ROOT / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        cache_writable = True
    except Exception as e:
        cache_error = str(e)
    return {
        "ok": all_ok and db_ok and cache_writable,
        "env": {
            "DOTA_DB_HOST": cfg["host"],
            "DOTA_DB_PORT": cfg["port"],
            "DOTA_DB_USER": cfg["user"],
            "hasCredentials": bool(cfg["user"] and cfg["password"]),
            "DOTA_DB_DATABASE": DEFAULT_DB,
            "DOTA_OVERVIEW_DATABASE": DEFAULT_OVERVIEW_DB,
        },
        "resources": files,
        "database": {
            "ok": db_ok,
            "error": db_error,
        },
        "cacheRoot": project_path(CACHE_ROOT),
        "cacheWritable": cache_writable,
        "cacheError": cache_error,
        "cacheVersion": CACHE_VERSION,
    }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return public_job(job)


@app.get("/api/jobs/{job_id}/result")
def get_job_result(job_id: str) -> dict:
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if job["status"] == "failed":
            raise HTTPException(status_code=500, detail=job.get("error") or "job failed")
        if job["status"] != "succeeded":
            raise HTTPException(status_code=202, detail=f"job is {job['status']}")
        return job["result"]


@app.post("/api/visibility/jobs")
def create_visibility_job(request: VisibilityRequest) -> dict:
    return create_job("visibility", visibility_job_runner, request)


@app.post("/api/ward-value/jobs")
def create_ward_value_job(request: WardValueRequest) -> dict:
    return create_job("ward-value", ward_value_job_runner, request)


@app.post("/api/teams/ward-comparison/jobs")
def create_team_comparison_job(request: TeamComparisonRequest) -> dict:
    return create_job("team-comparison", comparison_job_runner, request)


@app.post("/api/prewarm/jobs")
def create_prewarm_job(request: PrewarmRequest) -> dict:
    return create_job("prewarm", prewarm_job_runner, request)


@app.get("/api/prewarm/history")
def prewarm_history_list(limit: int = Query(20, ge=1, le=50)) -> dict:
    data = _load_prewarm_history()
    records = data.get("records", [])[:limit]
    return {"records": [summarize_prewarm_record(record) for record in records]}


@app.get("/api/prewarm/teams")
def prewarm_teams_list() -> dict:
    return {"teams": list_prewarmed_teams()}


@app.get("/api/prewarm/teams/{team_tag}/detail")
def prewarm_team_detail(team_tag: str) -> dict:
    try:
        return get_prewarm_team_detail(team_tag)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except pymysql.MySQLError as exc:
        raise HTTPException(status_code=500, detail=f"database query failed: {exc}") from exc


@app.post("/api/prewarm/teams/refresh/jobs")
def create_refresh_prewarm_team_job(request: RefreshPrewarmTeamRequest) -> dict:
    return create_job("prewarm-refresh", refresh_prewarm_team_runner, request)


@app.post("/api/prewarm/migrate-match-cache")
def migrate_match_cache(dry_run: bool = Query(False)) -> dict:
    try:
        return migrate_match_ward_cache_from_team_reports(dry_run=dry_run)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/prewarm/history/{record_id}")
def prewarm_history_detail(record_id: str) -> dict:
    data = _load_prewarm_history()
    for record in data.get("records", []):
        if record.get("id") == record_id:
            return record
    raise HTTPException(status_code=404, detail="预热记录不存在")


@app.get("/api/teams/search")
def teams_search(
    q: str = Query("", max_length=64),
    limit: int = Query(20, ge=1, le=50),
    rebuild: bool = Query(False),
) -> dict:
    if rebuild:
        build_team_directory(force=True)
    results = search_teams(q, limit)
    return {
        "query": q,
        "teams": [
            {
                "teamTag": t.get("tag"),
                "teamId": t.get("teamId"),
                "name": t.get("name"),
                "matchCount": t.get("matchCount"),
                "logoUrl": t.get("logoUrl"),
            }
            for t in results
        ],
    }


@app.get("/api/team/recent-matches")
def team_recent_matches(
    team_tag: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
    team_id: str | None = Query(None),
) -> dict:
    with connect() as conn:
        with conn.cursor() as cursor:
            data = resolve_recent_matches_by_side(cursor, team_tag, limit, team_id)
    return {"teamTag": team_tag, "teamId": team_id, **data}


@app.get("/api/teams/logos")
def team_logos(tags: str = Query(..., min_length=1)) -> dict:
    tag_list = [t for t in (tags or "").split(",") if t.strip()]
    if not tag_list:
        raise HTTPException(status_code=400, detail="tags 不能为空")
    if len(tag_list) > 24:
        tag_list = tag_list[:24]
    return {"logos": get_team_logos(tag_list)}


@app.get("/api/matches")
def list_matches(
    team_tag: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
    opponent_tag: str | None = Query(None),
    patch_version: str | None = Query(None),
    league: str | None = Query(None),
    team_id: str | None = Query(None),
) -> dict:
    tid = str(team_id or "").strip()
    use_id = tid not in ("", "0")
    if use_id:
        side_case = (
            "CASE WHEN CAST(mi.radiant_team_id AS CHAR)=%s THEN 'radiant' "
            "WHEN CAST(mi.dire_team_id AS CHAR)=%s THEN 'dire' ELSE '' END"
        )
        side_params: list = [tid, tid]
        filters = ["(CAST(mi.radiant_team_id AS CHAR)=%s OR CAST(mi.dire_team_id AS CHAR)=%s)"]
        params: list = [tid, tid]
    else:
        side_case = (
            "CASE WHEN LOWER(mi.radiant_team_tag)=LOWER(%s) THEN 'radiant' "
            "WHEN LOWER(mi.dire_team_tag)=LOWER(%s) THEN 'dire' ELSE '' END"
        )
        side_params = [team_tag, team_tag]
        filters = ["(LOWER(mi.radiant_team_tag)=LOWER(%s) OR LOWER(mi.dire_team_tag)=LOWER(%s))"]
        params = [team_tag, team_tag]
    if opponent_tag:
        filters.append(
            "((LOWER(mi.radiant_team_tag)=LOWER(%s) AND LOWER(mi.dire_team_tag)=LOWER(%s)) "
            "OR (LOWER(mi.dire_team_tag)=LOWER(%s) AND LOWER(mi.radiant_team_tag)=LOWER(%s)))"
        )
        params.extend([team_tag, opponent_tag, team_tag, opponent_tag])
    if patch_version:
        filters.append("ov.patch_version=%s")
        params.append(patch_version)
    if league:
        filters.append("LOWER(ov.league_name) LIKE LOWER(%s)")
        params.append(f"%{league}%")
    exec_params = side_params + params + [limit]
    where_sql = " AND ".join(filters)
    query = f"""
SELECT
  CAST(mi.match_id AS BIGINT) AS matchId,
  mi.radiant_team_tag AS radiantTeamTag,
  mi.dire_team_tag AS direTeamTag,
  {side_case} AS teamSide,
  ov.duration,
  ov.patch_version AS patchVersion,
  ov.league_name AS leagueName,
  ov.start_date AS startDate,
  mi.win
FROM match_info mi
LEFT JOIN `{DEFAULT_OVERVIEW_DB}`.`dwd_match_overview` ov
  ON CAST(mi.match_id AS BIGINT)=ov.match_id
WHERE {where_sql}
ORDER BY COALESCE(ov.start_time, mi.end_time) DESC
LIMIT %s
"""
    with connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, exec_params)
            rows = list(cursor.fetchall())
    for row in rows:
        if row.get("startDate") is not None:
            row["startDate"] = str(row["startDate"])
    return {"teamTag": team_tag, "teamId": tid or None, "matches": rows}


@app.post("/api/visibility")
def compute_visibility(request: VisibilityRequest) -> dict:
    try:
        return cached_visibility_report(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except pymysql.MySQLError as exc:
        raise HTTPException(status_code=500, detail=f"database query failed: {exc}") from exc


@app.post("/api/ward-value")
def compute_ward_value(request: WardValueRequest) -> dict:
    try:
        return cached_ward_value_report(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except pymysql.MySQLError as exc:
        raise HTTPException(status_code=500, detail=f"database query failed: {exc}") from exc


@app.post("/api/teams/ward-comparison")
def compute_team_comparison(request: TeamComparisonRequest) -> dict:
    try:
        return cached_team_comparison_report(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except pymysql.MySQLError as exc:
        raise HTTPException(status_code=500, detail=f"database query failed: {exc}") from exc


@app.post("/api/visibility/html")
def compute_visibility_html(request: VisibilityRequest) -> dict:
    report = compute_visibility(request)
    return {"htmlData": json.dumps(report, ensure_ascii=False)}


app.mount("/resources", StaticFiles(directory=RESOURCE_ROOT), name="resources")
app.mount("/", StaticFiles(directory=PROJECT_ROOT / "web" / "static", html=True), name="static")
