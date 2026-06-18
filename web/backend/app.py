from __future__ import annotations

import json
import os
import sys
import hashlib
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import pymysql
from fastapi import FastAPI, HTTPException, Query
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


DEFAULT_DB = os.environ.get("DOTA_DB_DATABASE", "dota2_stats")
DEFAULT_OVERVIEW_DB = os.environ.get("DOTA_OVERVIEW_DATABASE", "dwd_dota2")
CACHE_VERSION = "ward-hero-visibility-v8"
CACHE_ROOT = PROJECT_ROOT / "outputs" / "web_cache"


class VisibilityRequest(BaseModel):
    teamTag: str = Field(min_length=1)
    matchIds: list[int] = Field(min_length=1)
    start: int | None = None
    end: int | None = None
    forceRefresh: bool = False
    compareBothSides: bool = False


def db_config() -> dict:
    return {
        "host": os.environ.get("DOTA_DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("DOTA_DB_PORT", "9030")),
        "user": os.environ.get("DOTA_DB_USER", ""),
        "password": os.environ.get("DOTA_DB_PASSWORD", os.environ.get("DB_PASS", "")),
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


@lru_cache(maxsize=1)
def grid_and_cache() -> tuple[VisibilityGrid, CacheFow]:
    grid_path = RESOURCE_ROOT / "native-fow" / "dota_static_fow_grid.json"
    cache_path = RESOURCE_ROOT / "native-fow" / "cache.fow"
    return VisibilityGrid.load(grid_path), CacheFow.load(cache_path)


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


app = FastAPI(title="Dota Ward Vision Query")


@app.get("/api/health")
def health() -> dict:
    cfg = db_config()
    return {
        "ok": True,
        "database": cfg["database"],
        "overviewDatabase": DEFAULT_OVERVIEW_DB,
        "dbHost": cfg["host"],
        "dbPort": cfg["port"],
        "hasCredentials": bool(cfg["user"] and cfg["password"]),
        "cacheRoot": project_path(CACHE_ROOT),
        "cacheVersion": CACHE_VERSION,
    }


@app.get("/api/matches")
def list_matches(
    team_tag: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
    opponent_tag: str | None = Query(None),
    patch_version: str | None = Query(None),
    league: str | None = Query(None),
) -> dict:
    filters = [
        "(LOWER(mi.radiant_team_tag)=LOWER(%s) OR LOWER(mi.dire_team_tag)=LOWER(%s))"
    ]
    params: list = [team_tag, team_tag, team_tag, team_tag]
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
    params.append(limit)
    where_sql = " AND ".join(filters)
    query = f"""
SELECT
  CAST(mi.match_id AS BIGINT) AS matchId,
  mi.radiant_team_tag AS radiantTeamTag,
  mi.dire_team_tag AS direTeamTag,
  CASE
    WHEN LOWER(mi.radiant_team_tag)=LOWER(%s) THEN 'radiant'
    WHEN LOWER(mi.dire_team_tag)=LOWER(%s) THEN 'dire'
    ELSE ''
  END AS teamSide,
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
            cursor.execute(query, params)
            rows = list(cursor.fetchall())
    for row in rows:
        if row.get("startDate") is not None:
            row["startDate"] = str(row["startDate"])
    return {"teamTag": team_tag, "matches": rows}


@app.post("/api/visibility")
def compute_visibility(request: VisibilityRequest) -> dict:
    try:
        return cached_visibility_report(request)
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
