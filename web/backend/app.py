from __future__ import annotations

import json
import os
import sys
import hashlib
from collections import defaultdict
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
import compute_ward_value_metrics as ward_value  # noqa: E402


DEFAULT_DB = os.environ.get("DOTA_DB_DATABASE", "dota2_analysis")
DEFAULT_OVERVIEW_DB = os.environ.get("DOTA_OVERVIEW_DATABASE", "dwd_dota2")
CACHE_VERSION = "ward-hero-visibility-v8"
WARD_VALUE_CACHE_VERSION = "ward-value-v1"
COMPARISON_CACHE_VERSION = "team-comparison-v1"
CACHE_ROOT = PROJECT_ROOT / "outputs" / "web_cache"


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
    teams: list[ComparisonTeam] = Field(min_length=2, max_length=6)
    start: int | None = None
    end: int | None = None
    forceRefresh: bool = False
    clusterEps: float = Field(default=200.0, gt=0)


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


def ward_value_report(payload: WardValueRequest) -> dict:
    grid, cache = grid_and_cache()
    tree_id_cells = ward_value_tree_cells()
    args = ward_value_args(payload)
    matches = []
    all_instances = []
    with connect() as conn:
        with conn.cursor() as cursor:
            for match_id in payload.matchIds:
                match = ward_value.compute_match(cursor, int(match_id), args, grid, cache, tree_id_cells)
                team_side = resolve_team_side(match["matchInfo"], payload.teamTag)
                filtered_instances = [
                    item for item in match["instances"]
                    if item.get("team") == team_side
                ]
                match_summary = {key: value for key, value in match.items() if key != "instances"}
                match_summary["requestedTeamTag"] = payload.teamTag
                match_summary["requestedTeamSide"] = team_side
                match_summary["wards"] = {
                    "total": len(filtered_instances),
                    "observer": sum(1 for item in filtered_instances if item["wardType"] == "obs"),
                    "sentry": sum(1 for item in filtered_instances if item["wardType"] == "sen"),
                }
                matches.append(match_summary)
                all_instances.extend(filtered_instances)

    invisibility_available = all(match["invisibility"]["available"] for match in matches) if matches else False
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


def cached_ward_value_report(payload: WardValueRequest) -> dict:
    if not payload.forceRefresh:
        cached = read_cached_ward_value_report(payload)
        if cached is not None:
            return cached
    report = ward_value_report(payload)
    write_cached_ward_value_report(payload, report)
    return report


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


def team_comparison_report(payload: TeamComparisonRequest) -> dict:
    grid, cache = grid_and_cache()
    tree_id_cells = ward_value_tree_cells()
    args = comparison_args(payload)
    team_tags = [team.teamTag for team in payload.teams]
    focus_tag = team_tags[0]

    match_cache: dict[int, dict] = {}
    all_instances: list[dict] = []
    invis_flags: list[bool] = []
    per_team_counts: dict[str, int] = {tag: 0 for tag in team_tags}
    team_match_ids: dict[str, list[int]] = {}

    with connect() as conn:
        with conn.cursor() as cursor:
            for team in payload.teams:
                team_match_ids[team.teamTag] = [int(match_id) for match_id in team.matchIds]
                for match_id in team.matchIds:
                    key = int(match_id)
                    if key not in match_cache:
                        match_cache[key] = ward_value.compute_match(
                            cursor, key, args, grid, cache, tree_id_cells
                        )
                    match = match_cache[key]
                    side = resolve_team_side(match["matchInfo"], team.teamTag)
                    invis_flags.append(bool(match["invisibility"]["available"]))
                    for item in match["instances"]:
                        if item.get("team") != side:
                            continue
                        clone = dict(item)
                        clone["teamTag"] = team.teamTag
                        clone["teamSide"] = side
                        all_instances.append(clone)
                        per_team_counts[team.teamTag] += 1

    invisibility_available = all(invis_flags) if invis_flags else False
    ward_value.score_instances(all_instances, invisibility_available)
    instances = ward_value.finalize_instances(all_instances)
    spots = ward_value.cluster_spots(instances, payload.clusterEps)
    projection = ward_value.load_projection(Path(args.projection_calibration))
    for spot in spots:
        spot["pixel"] = ward_value.world_to_pixel(spot["centerWorldX"], spot["centerWorldY"], projection)

    instances_by_spot: dict[str, list[dict]] = defaultdict(list)
    for item in instances:
        instances_by_spot[str(item.get("spotId"))].append(item)

    team_spot_ids: dict[str, set[str]] = {tag: set() for tag in team_tags}
    for item in instances:
        team_spot_ids[item["teamTag"]].add(str(item.get("spotId")))

    team_summaries = []
    for tag in team_tags:
        team_instances = [item for item in instances if item["teamTag"] == tag]
        team_summaries.append(_team_summary(tag, team_instances, team_spot_ids[tag]))

    spot_rows = []
    for spot in spots:
        members = instances_by_spot.get(spot["spotId"], [])
        per_team = {}
        for tag in team_tags:
            team_members = [item for item in members if item["teamTag"] == tag]
            deward_known = [item for item in team_members if item.get("dewarded") is not None]
            per_team[tag] = {
                "placements": len(team_members),
                "usageRate": round(_safe_div(len(team_members), per_team_counts[tag]), 4),
                "avgScore": round(
                    _safe_div(sum(item["valueScore"] for item in team_members), len(team_members)), 2
                )
                if team_members
                else None,
                "avgSeenSeconds": round(
                    _safe_div(sum(item["enemyHeroSeenSeconds"] for item in team_members), len(team_members)), 2
                )
                if team_members
                else None,
                "dewardRate": (
                    round(_safe_div(sum(1 for item in deward_known if item["dewarded"]), len(deward_known)), 4)
                    if deward_known
                    else None
                ),
            }
        usage_rates = [per_team[tag]["usageRate"] for tag in team_tags]
        benchmark_usage = round(_safe_div(sum(usage_rates), len(usage_rates)), 4)
        spot_rows.append(
            {
                "spotId": spot["spotId"],
                "wardType": spot["wardType"],
                "team": spot["team"],
                "centerWorldX": spot["centerWorldX"],
                "centerWorldY": spot["centerWorldY"],
                "pixel": spot["pixel"],
                "sampleCount": spot["sampleCount"],
                "avgScore": spot["avgScore"],
                "avgSeenSeconds": spot["avgSeenSeconds"],
                "dewardRate": spot["dewardRate"],
                "confidence": spot["confidence"],
                "benchmarkUsageRate": benchmark_usage,
                "byTeam": per_team,
            }
        )

    focus_total = per_team_counts.get(focus_tag, 0)
    other_tags = team_tags[1:]

    def focus_usage(row: dict) -> float:
        return row["byTeam"][focus_tag]["usageRate"]

    def benchmark_usage_others(row: dict) -> float:
        rates = [row["byTeam"][tag]["usageRate"] for tag in other_tags]
        return _safe_div(sum(rates), len(rates))

    signature_spots = []
    overused_low_value = []
    underused_high_value = []
    for row in spot_rows:
        focus_rate = focus_usage(row)
        bench_rate = benchmark_usage_others(row)
        diff = round(focus_rate - bench_rate, 4)
        spot_score = row["avgScore"] or 0
        entry = {
            "spotId": row["spotId"],
            "wardType": row["wardType"],
            "team": row["team"],
            "focusUsageRate": focus_rate,
            "benchmarkUsageRate": round(bench_rate, 4),
            "usageDiff": diff,
            "avgScore": row["avgScore"],
            "dewardRate": row["dewardRate"],
            "sampleCount": row["sampleCount"],
        }
        if focus_rate > 0 and diff >= 0.03 and spot_score >= 55:
            signature_spots.append(entry)
        if focus_rate > 0 and diff >= 0.03 and spot_score < 45:
            overused_low_value.append(entry)
        if focus_rate <= bench_rate - 0.03 and spot_score >= 60:
            underused_high_value.append(entry)

    signature_spots.sort(key=lambda item: -item["usageDiff"])
    overused_low_value.sort(key=lambda item: (item["avgScore"] or 0, -item["usageDiff"]))
    underused_high_value.sort(key=lambda item: (-(item["avgScore"] or 0), item["usageDiff"]))

    return {
        "source": {
            "database": DEFAULT_DB,
            "focusTeam": focus_tag,
            "teamTags": team_tags,
            "teamMatchIds": team_match_ids,
            "map": map_config(),
            "mapVersion": ward_value.MAP_VERSION,
            "clusterEpsWorld": payload.clusterEps,
            "invisibilityDataAvailable": invisibility_available,
        },
        "summary": {
            "teamCount": len(team_tags),
            "instanceCount": len(instances),
            "spotCount": len(spots),
            "focusTeam": focus_tag,
        },
        "teams": team_summaries,
        "spots": spot_rows,
        "diagnostics": {
            "signatureSpots": signature_spots[:20],
            "overusedLowValueSpots": overused_low_value[:20],
            "underusedHighValueSpots": underused_high_value[:20],
        },
    }


def cached_team_comparison_report(payload: TeamComparisonRequest) -> dict:
    if not payload.forceRefresh:
        cached = read_cached_comparison_report(payload)
        if cached is not None:
            return cached
    report = team_comparison_report(payload)
    write_cached_comparison_report(payload, report)
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
