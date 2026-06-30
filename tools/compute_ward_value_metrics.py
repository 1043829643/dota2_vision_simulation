from __future__ import annotations

import argparse
import base64
import json
import math
import os
from collections import defaultdict
from pathlib import Path

import pymysql

from compute_ward_occlusion_native import (
    load_tree_id_cells,
    normalize_tree_events,
    parser_to_world,
    summarize_rejected_tree_events,
)
from native_fow import CacheFow, VisibilityGrid, visible_cells


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = PROJECT_ROOT / "resources"
OBSERVER_RADIUS = 1600.0
SENTRY_RADIUS = 1000.0
MAP_VERSION = "7.41"


def image_data_url(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    suffix = path.suffix.lower()
    mime = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def load_projection(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def world_to_pixel(world_x: float, world_y: float, projection: dict | None) -> dict | None:
    if not projection:
        return None
    affine = projection.get("affine") or {}
    return {
        "x": affine["a"] * world_x + affine["b"] * world_y + affine["c"],
        "y": affine["d"] * world_x + affine["e"] * world_y + affine["f"],
    }


def project_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def json_safe(value):
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted(json_safe(item) for item in value)
    return value


def connect(args):
    return pymysql.connect(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=args.db_password,
        database=args.database,
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=120,
        cursorclass=pymysql.cursors.DictCursor,
    )


def fetch_all(cursor, query: str, params=()) -> list[dict]:
    cursor.execute(query, params)
    return list(cursor.fetchall())


def side_from_slot(slot) -> str | None:
    if slot is None:
        return None
    return "radiant" if int(slot) < 5 else "dire"


def side_from_team(team) -> str | None:
    if team is None:
        return None
    value = int(team)
    if value == 2:
        return "radiant"
    if value == 3:
        return "dire"
    return None


def enemy_side(side: str) -> str:
    return "dire" if side == "radiant" else "radiant"


def normalize_ward_type(value) -> str:
    return str(value or "").replace("_left", "")


def load_players(cursor, match_id: int) -> dict[int, dict]:
    rows = fetch_all(
        cursor,
        """
SELECT slot,steamid,hero_name,hero_id,persona,team
FROM players
WHERE CAST(match_id AS BIGINT)=%s
ORDER BY slot
""",
        (match_id,),
    )
    players = {}
    for row in rows:
        slot = int(row["slot"])
        players[slot] = {
            "slot": slot,
            "steamid": None if row.get("steamid") is None else int(row["steamid"]),
            "heroName": row.get("hero_name"),
            "heroId": None if row.get("hero_id") is None else int(row["hero_id"]),
            "persona": row.get("persona"),
            "team": side_from_team(row.get("team")) or side_from_slot(slot),
        }
    if len(players) != 10:
        raise ValueError(f"expected 10 players for match {match_id}, got {len(players)}")
    return players


def load_match_info(cursor, match_id: int) -> dict:
    rows = fetch_all(
        cursor,
        """
SELECT match_id,radiant_team_tag,dire_team_tag
FROM match_info
WHERE CAST(match_id AS BIGINT)=%s
""",
        (match_id,),
    )
    if not rows:
        return {"match_id": match_id, "radiant_team_tag": "", "dire_team_tag": ""}
    return rows[0]


def load_ward_intervals(cursor, match_id: int, players: dict[int, dict], end_second: int) -> list[dict]:
    rows = fetch_all(
        cursor,
        """
SELECT time,slot,type,attackername,x,y,z,entityleft,ehandle,log_index
FROM ward_placed_left_fact
WHERE CAST(match_id AS BIGINT)=%s
ORDER BY time,log_index
""",
        (match_id,),
    )
    by_handle: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        by_handle[int(row["ehandle"])].append(row)

    hero_to_side = {
        str(player.get("heroName") or "").lower(): player["team"]
        for player in players.values()
    }
    intervals = []
    for ehandle, events in by_handle.items():
        events.sort(key=lambda row: (int(row["time"]), int(row["log_index"])))
        placements = [row for row in events if str(row.get("entityleft")).lower() == "false"]
        lefts = [row for row in events if str(row.get("entityleft")).lower() == "true"]
        if not placements:
            continue
        place = placements[0]
        ward_type = normalize_ward_type(place.get("type"))
        left = next((row for row in lefts if normalize_ward_type(row.get("type")) == ward_type), None)
        team = side_from_slot(place.get("slot"))
        attacker = str(left.get("attackername") or "").lower() if left else ""
        attacker_side = hero_to_side.get(attacker)
        if left and attacker_side and team and attacker_side != team:
            removed_reason = "dewarded"
            removed_confidence = "high"
            dewarded = True
        elif left and attacker_side == team:
            removed_reason = "expired_or_allied_removed"
            removed_confidence = "medium"
            dewarded = False
        elif left and attacker:
            removed_reason = "removed_by_unknown_side_hero"
            removed_confidence = "low"
            dewarded = None
        elif left:
            removed_reason = "expired_or_removed_unknown"
            removed_confidence = "low"
            dewarded = None
        else:
            removed_reason = "no_left_event"
            removed_confidence = "low"
            dewarded = None
        start = int(place["time"])
        end = int(left["time"]) if left else end_second + 1
        world_x, world_y = parser_to_world(float(place["x"]), float(place["y"]))
        intervals.append(
            {
                "matchId": match_id,
                "ehandle": int(ehandle),
                "wardType": ward_type,
                "team": team,
                "slot": None if place.get("slot") is None else int(place["slot"]),
                "placerHero": None if place.get("slot") is None else players[int(place["slot"])]["heroName"],
                "start": start,
                "end": end,
                "duration": max(0, end - start),
                "parserX": float(place["x"]),
                "parserY": float(place["y"]),
                "worldX": world_x,
                "worldY": world_y,
                "z": None if place.get("z") is None else float(place["z"]),
                "removedReason": removed_reason,
                "removedReasonConfidence": removed_confidence,
                "dewarded": dewarded,
                "leftAttacker": "" if not left else str(left.get("attackername") or ""),
            }
        )
    intervals.sort(key=lambda ward: (ward["matchId"], ward["start"], ward["team"] or "", ward["wardType"], ward["ehandle"]))
    return intervals


def load_positions(cursor, match_id: int, start: int, end: int, players: dict[int, dict], team_filter: str | None = None) -> dict[int, list[dict]]:
    rows = fetch_all(
        cursor,
        """
SELECT time,slot,unit,life_state,x,y,log_index
FROM player_intervals2
WHERE CAST(match_id AS BIGINT)=%s
  AND time BETWEEN %s AND %s
  AND life_state='0'
  AND x <> ''
  AND y <> ''
ORDER BY time,slot,log_index
""",
        (match_id, start, end),
    )
    by_second: dict[int, list[dict]] = defaultdict(list)
    seen = set()
    for row in rows:
        key = (int(row["time"]), int(row["slot"]))
        if key in seen or key[1] not in players:
            continue
        if team_filter in {"radiant", "dire"} and players[key[1]]["team"] != team_filter:
            continue
        seen.add(key)
        world_x, world_y = parser_to_world(float(row["x"]), float(row["y"]))
        by_second[key[0]].append(
            {
                "slot": key[1],
                "unit": row.get("unit"),
                "team": players[key[1]]["team"],
                "heroName": players[key[1]]["heroName"],
                "persona": players[key[1]]["persona"],
                "parserX": float(row["x"]),
                "parserY": float(row["y"]),
                "worldX": world_x,
                "worldY": world_y,
            }
        )
    return by_second


def load_time_bounds(cursor, match_id: int) -> tuple[int, int]:
    row = fetch_all(
        cursor,
        """
SELECT MIN(time) AS min_time, MAX(time) AS max_time
FROM player_intervals2
WHERE CAST(match_id AS BIGINT)=%s AND x <> '' AND y <> ''
""",
        (match_id,),
    )[0]
    return int(row["min_time"]), int(row["max_time"])


def load_tree_events(cursor, match_id: int, grid: VisibilityGrid, tree_id_cells: dict[int, tuple[int, int]]) -> tuple[list[dict], list[dict], dict]:
    rows = fetch_all(
        cursor,
        """
SELECT time,log_index,state,treeId
FROM dota_tree_state_change
WHERE CAST(match_id AS BIGINT)=%s
ORDER BY time,log_index
""",
        (match_id,),
    )
    events, rejected = normalize_tree_events(rows, grid, tree_id_cells)
    return events, rejected, summarize_rejected_tree_events(rejected, tree_id_cells)


def load_invisible_seconds(cursor, match_id: int, start: int, end: int, players: dict[int, dict]) -> tuple[dict[int, set[int]], dict]:
    hero_to_slot = {
        str(player["heroName"] or "").lower(): int(slot)
        for slot, player in players.items()
    }
    try:
        rows = fetch_all(
            cursor,
            """
SELECT time,log_index,type,targetname,inflictor
FROM combat_logs
WHERE CAST(match_id AS BIGINT)=%s
  AND invisibility_modifier='true'
  AND type IN ('DOTA_COMBATLOG_MODIFIER_ADD','DOTA_COMBATLOG_MODIFIER_REMOVE')
  AND time <= %s
ORDER BY time,log_index
""",
            (match_id, end),
        )
    except Exception as exc:
        return defaultdict(set), {
            "available": False,
            "reason": str(exc),
            "eventRows": 0,
            "modifiers": {},
        }

    active: dict[tuple[int, str], int] = {}
    ranges: dict[int, list[tuple[int, int]]] = defaultdict(list)
    modifiers: dict[str, int] = defaultdict(int)
    for row in rows:
        slot = hero_to_slot.get(str(row.get("targetname") or "").lower())
        if slot is None:
            continue
        inflictor = str(row.get("inflictor") or "")
        modifiers[inflictor] += 1
        key = (slot, inflictor)
        second = int(row["time"])
        if row["type"] == "DOTA_COMBATLOG_MODIFIER_ADD":
            active[key] = second
        else:
            added_at = active.pop(key, None)
            if added_at is not None:
                ranges[slot].append((added_at, second))

    for (slot, _inflictor), added_at in active.items():
        ranges[slot].append((added_at, end + 1))

    invisible: dict[int, set[int]] = defaultdict(set)
    for slot, intervals in ranges.items():
        for interval_start, interval_end in intervals:
            clamped_start = max(start, interval_start)
            clamped_end = min(end + 1, interval_end)
            if clamped_start < clamped_end:
                invisible[slot].update(range(clamped_start, clamped_end))
    return invisible, {
        "available": True,
        "eventRows": len(rows),
        "modifiers": dict(sorted(modifiers.items(), key=lambda item: (-item[1], item[0]))),
        "invisibleSeconds": sum(len(seconds) for seconds in invisible.values()),
    }


def active_wards(wards: list[dict], second: int, team: str | None = None, ward_type: str | None = None) -> list[dict]:
    return [
        ward
        for ward in wards
        if ward["start"] <= second < ward["end"]
        and (team is None or ward["team"] == team)
        and (ward_type is None or ward["wardType"] == ward_type)
    ]


def active_ward_indexes(wards: list[dict], start: int, end: int) -> tuple[dict[int, list[dict]], dict[str, dict[int, list[dict]]]]:
    obs_by_second: dict[int, list[dict]] = defaultdict(list)
    sentries_by_team_by_second: dict[str, dict[int, list[dict]]] = {
        "radiant": defaultdict(list),
        "dire": defaultdict(list),
    }
    for ward in wards:
        active_start = max(start, int(ward["start"]))
        active_end = min(end + 1, int(ward["end"]))
        if active_start >= active_end:
            continue
        target = (
            obs_by_second
            if ward["wardType"] == "obs"
            else sentries_by_team_by_second[ward["team"]]
        )
        for second in range(active_start, active_end):
            target[second].append(ward)
    return obs_by_second, sentries_by_team_by_second


def sentry_covers(sentries: list[dict], world_x: float, world_y: float) -> bool:
    radius_sq = SENTRY_RADIUS * SENTRY_RADIUS
    for sentry in sentries:
        dx = world_x - sentry["worldX"]
        dy = world_y - sentry["worldY"]
        if dx * dx + dy * dy <= radius_sq:
            return True
    return False


def new_metric_state(ward: dict, grid: VisibilityGrid) -> dict:
    origin = grid.world_to_cell(ward["worldX"], ward["worldY"])
    return {
        **ward,
        "originGrid": {"x": origin[0], "y": origin[1]},
        "lifetimeSeconds": ward["duration"],
        "enemyHeroSeenSeconds": 0,
        "uniqueHeroesSeen": 0,
        "uniqueHeroSlotsSeen": set(),
        "sightingCount": 0,
        "firstContactCount": 0,
        "invisibleHeroSeenSeconds": 0,
        "invisibleBlockedSeconds": 0,
        "lowOverlapSeenSeconds": 0,
        "overlapCellSeconds": 0,
        "visibleCellSeconds": 0,
        "activeSecondsWithCells": 0,
        "invisibleHeroTrueSightSeconds": 0,
        "uniqueInvisibleHeroesCovered": 0,
        "uniqueInvisibleHeroSlotsCovered": set(),
        "observerAssistedInvisibleSightings": 0,
        "antiInvisOpportunitySeconds": 0,
        "heroSegments": defaultdict(list),
        "lastSeenSlots": set(),
        "firstContacts": [],
        "sampleSightings": [],
        "scoreBreakdown": {},
        "valueScore": 0.0,
        "confidence": "LOW",
    }


def append_segment(segments: list[dict], second: int, invisible: bool, hero: dict) -> None:
    if segments and segments[-1]["end"] == second and segments[-1]["invisible"] == invisible:
        segments[-1]["end"] = second + 1
        segments[-1]["duration"] += 1
        return
    segments.append(
        {
            "start": second,
            "end": second + 1,
            "duration": 1,
            "invisible": invisible,
            "heroName": hero.get("heroName"),
            "persona": hero.get("persona"),
        }
    )


def apply_tree_events_before(grid: VisibilityGrid, tree_events: list[dict], start: int) -> tuple[int, int]:
    version = 0
    applied = 0
    for event in tree_events:
        if int(event["second"]) >= start:
            continue
        if grid.set_tree_alive(event["cell"][0], event["cell"][1], bool(event["alive"])):
            version += 1
        applied += 1
    return version, applied


def compute_match(cursor, match_id: int, args, base_grid: VisibilityGrid, cache: CacheFow, tree_id_cells: dict[int, tuple[int, int]]) -> dict:
    # Use a fresh mutable grid per match because dynamic tree state mutates tile bytes.
    grid = VisibilityGrid(
        base_grid.width,
        base_grid.height,
        base_grid.origin_x,
        base_grid.origin_y,
        base_grid.cell_size,
        list(base_grid.tile_bytes),
        base_grid.hard_visible,
        base_grid.metadata,
    )
    players = load_players(cursor, match_id)
    match_info = load_match_info(cursor, match_id)
    pos_start, pos_end = load_time_bounds(cursor, match_id)
    start = max(0, args.start if args.start is not None else pos_start)
    end = args.end if args.end is not None else pos_end
    wards = load_ward_intervals(cursor, match_id, players, end)
    team_side_filter = getattr(args, "team_side_filter", None)
    if team_side_filter in {"radiant", "dire"}:
        wards = [ward for ward in wards if ward["team"] == team_side_filter]
    enemy_position_filter = enemy_side(team_side_filter) if team_side_filter in {"radiant", "dire"} else None
    positions_by_second = load_positions(cursor, match_id, start, end, players, enemy_position_filter)
    invisible_seconds, invis_meta = load_invisible_seconds(cursor, match_id, start, end, players)
    tree_events, rejected_tree_events, rejected_summary = load_tree_events(cursor, match_id, grid, tree_id_cells)

    states = {int(ward["ehandle"]): new_metric_state(ward, grid) for ward in wards}
    obs_by_second, sentries_by_team_by_second = active_ward_indexes(wards, start, end)
    events_by_second: dict[int, list[dict]] = defaultdict(list)
    for event in tree_events:
        events_by_second[int(event["second"])].append(event)
    state_version, tree_events_applied_before = apply_tree_events_before(grid, tree_events, start)
    tree_events_applied_window = 0
    vision_cache: dict[tuple[int, int], tuple[set[tuple[int, int]], dict]] = {}
    team_visible_slots_previous = {"radiant": set(), "dire": set()}
    progress_callback = getattr(args, "progress_callback", None)
    progress_step = max(1, int(getattr(args, "progress_step", 30) or 30))

    for second in range(start, end + 1):
        if progress_callback and (second == start or second == end or (second - start) % progress_step == 0):
            progress_callback(match_id, second, start, end)
        for event in events_by_second.get(second, []):
            if grid.set_tree_alive(event["cell"][0], event["cell"][1], bool(event["alive"])):
                state_version += 1
            tree_events_applied_window += 1

        active_obs = obs_by_second.get(second, [])
        active_sentries_by_team = {
            "radiant": sentries_by_team_by_second["radiant"].get(second, []),
            "dire": sentries_by_team_by_second["dire"].get(second, []),
        }
        obs_cells_by_handle: dict[int, set[tuple[int, int]]] = {}
        obs_stats_by_handle: dict[int, dict] = {}
        for ward in active_obs:
            ehandle = int(ward["ehandle"])
            cache_key = (ehandle, state_version)
            if cache_key not in vision_cache:
                cells, stats = visible_cells(
                    grid,
                    cache,
                    ward["worldX"],
                    ward["worldY"],
                    OBSERVER_RADIUS,
                )
                vision_cache[cache_key] = ({tuple(cell) for cell in cells}, stats)
            obs_cells_by_handle[ehandle], obs_stats_by_handle[ehandle] = vision_cache[cache_key]

        union_by_team: dict[str, set[tuple[int, int]]] = {"radiant": set(), "dire": set()}
        for ward in active_obs:
            union_by_team[ward["team"]].update(obs_cells_by_handle.get(int(ward["ehandle"]), set()))

        team_visible_slots_now = {"radiant": set(), "dire": set()}
        hero_positions = positions_by_second.get(second, [])
        for ward in active_obs:
            state = states[int(ward["ehandle"])]
            cells = obs_cells_by_handle.get(int(ward["ehandle"]), set())
            stats = obs_stats_by_handle.get(int(ward["ehandle"]), {})
            if cells:
                state["visibleCellSeconds"] += len(cells)
                state["activeSecondsWithCells"] += 1
                allied_cells = union_by_team[ward["team"]]
                other_cells = allied_cells - cells if allied_cells else set()
                if other_cells:
                    state["overlapCellSeconds"] += len(cells & other_cells)

            enemy = enemy_side(ward["team"])
            allied_sentries = active_sentries_by_team[ward["team"]]
            other_allied_cells = union_by_team[ward["team"]] - cells
            for hero in hero_positions:
                if hero["team"] != enemy:
                    continue
                hero_cell = grid.world_to_cell(hero["worldX"], hero["worldY"])
                if hero_cell not in cells:
                    continue
                invisible = second in invisible_seconds.get(int(hero["slot"]), set())
                if invisible and not sentry_covers(allied_sentries, hero["worldX"], hero["worldY"]):
                    state["invisibleBlockedSeconds"] += 1
                    continue
                state["enemyHeroSeenSeconds"] += 1
                state["uniqueHeroSlotsSeen"].add(int(hero["slot"]))
                if invisible:
                    state["invisibleHeroSeenSeconds"] += 1
                if hero_cell not in other_allied_cells:
                    state["lowOverlapSeenSeconds"] += 1
                if int(hero["slot"]) not in team_visible_slots_previous[ward["team"]]:
                    state["firstContactCount"] += 1
                    if len(state["firstContacts"]) < 20:
                        state["firstContacts"].append(
                            {
                                "time": second,
                                "slot": int(hero["slot"]),
                                "heroName": hero["heroName"],
                                "persona": hero["persona"],
                            }
                        )
                team_visible_slots_now[ward["team"]].add(int(hero["slot"]))
                append_segment(state["heroSegments"][int(hero["slot"])], second, invisible, hero)
                if len(state["sampleSightings"]) < 20:
                    state["sampleSightings"].append(
                        {
                            "time": second,
                            "slot": int(hero["slot"]),
                            "heroName": hero["heroName"],
                            "persona": hero["persona"],
                            "invisible": invisible,
                        }
                    )

        for sentry in active_sentries_by_team["radiant"] + active_sentries_by_team["dire"]:
            state = states[int(sentry["ehandle"])]
            enemy = enemy_side(sentry["team"])
            for hero in hero_positions:
                if hero["team"] != enemy:
                    continue
                invisible = second in invisible_seconds.get(int(hero["slot"]), set())
                if not invisible:
                    continue
                if not sentry_covers([sentry], hero["worldX"], hero["worldY"]):
                    continue
                state["antiInvisOpportunitySeconds"] += 1
                state["invisibleHeroTrueSightSeconds"] += 1
                state["uniqueInvisibleHeroSlotsCovered"].add(int(hero["slot"]))
                hero_cell = grid.world_to_cell(hero["worldX"], hero["worldY"])
                if hero_cell in union_by_team[sentry["team"]]:
                    state["observerAssistedInvisibleSightings"] += 1

        team_visible_slots_previous = team_visible_slots_now

    instances = []
    for state in states.values():
        state["uniqueHeroesSeen"] = len(state.pop("uniqueHeroSlotsSeen"))
        state["uniqueInvisibleHeroesCovered"] = len(state.pop("uniqueInvisibleHeroSlotsCovered"))
        state["sightingCount"] = sum(len(segments) for segments in state["heroSegments"].values())
        state["heroSegments"] = [
            {"slot": slot, "segments": segments}
            for slot, segments in sorted(state["heroSegments"].items())
        ]
        state["avgVisibleCellCount"] = (
            state["visibleCellSeconds"] / state["activeSecondsWithCells"]
            if state["activeSecondsWithCells"]
            else 0.0
        )
        state["overlapRate"] = (
            state["overlapCellSeconds"] / state["visibleCellSeconds"]
            if state["visibleCellSeconds"]
            else 0.0
        )
        state["antiInvisEfficiency"] = (
            state["invisibleHeroTrueSightSeconds"] / state["antiInvisOpportunitySeconds"]
            if state["antiInvisOpportunitySeconds"]
            else 0.0
        )
        state["fastDewarded30"] = state["dewarded"] is True and state["duration"] <= 30
        state["fastDewarded60"] = state["dewarded"] is True and state["duration"] <= 60
        state["fastDewarded90"] = state["dewarded"] is True and state["duration"] <= 90
        state["treeDynamicApplied"] = bool(tree_events)
        instances.append(state)

    return {
        "matchId": match_id,
        "matchInfo": match_info,
        "players": [players[slot] for slot in sorted(players)],
        "timeWindow": {"start": start, "end": end},
        "invisibility": invis_meta,
        "treeEvents": {
            "rowsAccepted": len(tree_events),
            "rowsRejected": len(rejected_tree_events),
            "rejectedSummary": rejected_summary,
            "appliedBeforeStart": tree_events_applied_before,
            "appliedInWindow": tree_events_applied_window,
            "stateVersions": state_version + 1,
        },
        "wards": {
            "total": len(wards),
            "observer": sum(1 for ward in wards if ward["wardType"] == "obs"),
            "sentry": sum(1 for ward in wards if ward["wardType"] == "sen"),
        },
        "instances": instances,
    }


def p90(values: list[float]) -> float:
    values = sorted(value for value in values if value > 0)
    if not values:
        return 1.0
    index = min(len(values) - 1, math.ceil(len(values) * 0.9) - 1)
    return max(values[index], 1.0)


def norm(value: float, denominator: float) -> float:
    return min(float(value) / max(denominator, 1.0), 1.0)


def score_instances(instances: list[dict], invisibility_available: bool) -> None:
    obs = [item for item in instances if item["wardType"] == "obs"]
    sen = [item for item in instances if item["wardType"] == "sen"]
    obs_den = {
        "seen": p90([item["enemyHeroSeenSeconds"] for item in obs]),
        "low_overlap": p90([item["lowOverlapSeenSeconds"] for item in obs]),
        "first": p90([item["firstContactCount"] for item in obs]),
        "unique": p90([item["uniqueHeroesSeen"] for item in obs]),
        "life": p90([min(item["lifetimeSeconds"], 360) for item in obs]),
    }
    sen_den = {
        "true": p90([item["invisibleHeroTrueSightSeconds"] for item in sen]),
        "assist": p90([item["observerAssistedInvisibleSightings"] for item in sen]),
        "unique": p90([item["uniqueInvisibleHeroesCovered"] for item in sen]),
        "eff": p90([item["antiInvisEfficiency"] for item in sen]),
        "life": p90([min(item["lifetimeSeconds"], 360) for item in sen]),
    }
    for item in instances:
        if item["wardType"] == "obs":
            parts = {
                "seen": 35 * norm(item["enemyHeroSeenSeconds"], obs_den["seen"]),
                "lowOverlap": 20 * norm(item["lowOverlapSeenSeconds"], obs_den["low_overlap"]),
                "firstContact": 15 * norm(item["firstContactCount"], obs_den["first"]),
                "uniqueHeroes": 10 * norm(item["uniqueHeroesSeen"], obs_den["unique"]),
                "lifetime": 10 * norm(min(item["lifetimeSeconds"], 360), obs_den["life"]),
                "survival": 10 * (0.0 if item["fastDewarded60"] else 1.0),
                "penaltyHighOverlap": -10 * max(0.0, item["overlapRate"] - 0.5),
                "penaltyFastDeward": -15 if item["fastDewarded60"] else 0,
            }
        else:
            if not invisibility_available:
                parts = {
                    "trueSight": 0,
                    "observerAssist": 0,
                    "uniqueInvisible": 0,
                    "antiInvisEfficiency": 0,
                    "lifetime": 10 * norm(min(item["lifetimeSeconds"], 360), sen_den["life"]),
                    "survival": 10 * (0.0 if item["fastDewarded60"] else 1.0),
                    "penaltyNoOpportunity": -20,
                    "penaltyFastDeward": -15 if item["fastDewarded60"] else 0,
                }
            else:
                no_opportunity_penalty = -10 if item["antiInvisOpportunitySeconds"] == 0 else 0
                parts = {
                    "trueSight": 40 * norm(item["invisibleHeroTrueSightSeconds"], sen_den["true"]),
                    "observerAssist": 25 * norm(item["observerAssistedInvisibleSightings"], sen_den["assist"]),
                    "uniqueInvisible": 15 * norm(item["uniqueInvisibleHeroesCovered"], sen_den["unique"]),
                    "antiInvisEfficiency": 10 * norm(item["antiInvisEfficiency"], sen_den["eff"]),
                    "lifetime": 5 * norm(min(item["lifetimeSeconds"], 360), sen_den["life"]),
                    "survival": 5 * (0.0 if item["fastDewarded60"] else 1.0),
                    "penaltyNoOpportunity": no_opportunity_penalty,
                    "penaltyFastDeward": -15 if item["fastDewarded60"] else 0,
                }
        item["scoreBreakdown"] = {key: round(value, 3) for key, value in parts.items()}
        item["valueScore"] = round(max(0.0, min(100.0, sum(parts.values()))), 2)


def distance_sq(a: dict, b: dict) -> float:
    dx = float(a["worldX"]) - float(b["worldX"])
    dy = float(a["worldY"]) - float(b["worldY"])
    return dx * dx + dy * dy


def cluster_spots(instances: list[dict], eps: float) -> list[dict]:
    spots = []
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for item in instances:
        groups[(MAP_VERSION, item["wardType"], item["team"])].append(item)
    eps_sq = eps * eps
    spot_counter = 1
    for (map_version, ward_type, team), items in sorted(groups.items()):
        unvisited = set(range(len(items)))
        while unvisited:
            seed = unvisited.pop()
            cluster = [seed]
            queue = [seed]
            while queue:
                idx = queue.pop()
                neighbors = [
                    other
                    for other in list(unvisited)
                    if distance_sq(items[idx], items[other]) <= eps_sq
                ]
                for other in neighbors:
                    unvisited.remove(other)
                    queue.append(other)
                    cluster.append(other)
            members = [items[index] for index in cluster]
            center_x = sum(item["worldX"] for item in members) / len(members)
            center_y = sum(item["worldY"] for item in members) / len(members)
            spot_id = f"{ward_type}_{team}_{spot_counter:04d}"
            spot_counter += 1
            for item in members:
                item["spotId"] = spot_id
            deward_known = [item for item in members if item["dewarded"] is not None]
            spots.append(
                {
                    "spotId": spot_id,
                    "mapVersion": map_version,
                    "wardType": ward_type,
                    "team": team,
                    "centerWorldX": round(center_x, 3),
                    "centerWorldY": round(center_y, 3),
                    "sampleCount": len(members),
                    "matchCount": len({item["matchId"] for item in members}),
                    "instanceKeys": [
                        f"{item['matchId']}:{item['ehandle']}" for item in members
                    ],
                    "avgScore": round(sum(item["valueScore"] for item in members) / len(members), 2),
                    "avgSeenSeconds": round(sum(item["enemyHeroSeenSeconds"] for item in members) / len(members), 2),
                    "avgLifetimeSeconds": round(sum(item["lifetimeSeconds"] for item in members) / len(members), 2),
                    "dewardRate": (
                        round(sum(1 for item in deward_known if item["dewarded"]) / len(deward_known), 4)
                        if deward_known
                        else None
                    ),
                    "confidence": confidence_for_count(len(members)),
                }
            )
    return sorted(spots, key=lambda item: (-item["avgScore"], -item["sampleCount"], item["spotId"]))


def confidence_for_count(count: int) -> str:
    if count >= 20:
        return "HIGH"
    if count >= 8:
        return "MEDIUM"
    return "LOW"


def finalize_instances(instances: list[dict]) -> list[dict]:
    for item in instances:
        item["mapVersion"] = MAP_VERSION
        item["confidence"] = confidence_for_count(1)
        for key in ("lastSeenSlots",):
            item.pop(key, None)
    return sorted(instances, key=lambda item: (item["matchId"], item["start"], item["ehandle"]))


def build_leaderboards(instances: list[dict], spots: list[dict]) -> dict:
    obs = [item for item in instances if item["wardType"] == "obs"]
    sen = [item for item in instances if item["wardType"] == "sen"]
    return {
        "observer": {
            "bestOverall": sorted(obs, key=lambda item: (-item["valueScore"], -item["enemyHeroSeenSeconds"]))[:20],
            "mostSeen": sorted(obs, key=lambda item: (-item["enemyHeroSeenSeconds"], -item["valueScore"]))[:20],
            "bestLowOverlap": sorted(obs, key=lambda item: (-item["lowOverlapSeenSeconds"], -item["valueScore"]))[:20],
            "bestFirstContact": sorted(obs, key=lambda item: (-item["firstContactCount"], -item["valueScore"]))[:20],
            "worstLowValue": sorted(obs, key=lambda item: (item["valueScore"], item["enemyHeroSeenSeconds"]))[:20],
            "fastDewarded": sorted(
                [item for item in obs if item["dewarded"] is True],
                key=lambda item: (item["lifetimeSeconds"], item["valueScore"]),
            )[:20],
        },
        "sentry": {
            "bestAntiInvis": sorted(sen, key=lambda item: (-item["valueScore"], -item["invisibleHeroTrueSightSeconds"]))[:20],
            "bestObserverAssisted": sorted(sen, key=lambda item: (-item["observerAssistedInvisibleSightings"], -item["valueScore"]))[:20],
            "fastDewarded": sorted(
                [item for item in sen if item["dewarded"] is True],
                key=lambda item: (item["lifetimeSeconds"], item["valueScore"]),
            )[:20],
        },
        "spots": {
            "best": spots[:20],
            "worst": sorted(spots, key=lambda item: (item["avgScore"], -item["sampleCount"]))[:20],
        },
    }


def build_spot_details(spots: list[dict], instances: list[dict]) -> list[dict]:
    by_spot: dict[str, list[dict]] = defaultdict(list)
    for item in instances:
        by_spot[str(item.get("spotId"))].append(item)
    details = []
    for spot in spots:
        members = sorted(
            by_spot.get(spot["spotId"], []),
            key=lambda item: (-float(item.get("valueScore") or 0), item["matchId"], item["start"]),
        )
        teams = sorted({str(item.get("team")) for item in members})
        match_ids = sorted({int(item["matchId"]) for item in members})
        detail = {
            **spot,
            "teams": teams,
            "matchIds": match_ids,
            "bestInstance": compact_instance(members[0]) if members else None,
            "instances": [compact_instance(item) for item in members],
        }
        details.append(detail)
    return details


def compact_instance(item: dict) -> dict:
    keys = [
        "matchId",
        "ehandle",
        "spotId",
        "mapVersion",
        "wardType",
        "team",
        "slot",
        "placerHero",
        "start",
        "end",
        "lifetimeSeconds",
        "worldX",
        "worldY",
        "removedReason",
        "dewarded",
        "leftAttacker",
        "enemyHeroSeenSeconds",
        "uniqueHeroesSeen",
        "sightingCount",
        "firstContactCount",
        "invisibleHeroSeenSeconds",
        "invisibleBlockedSeconds",
        "lowOverlapSeenSeconds",
        "overlapRate",
        "invisibleHeroTrueSightSeconds",
        "observerAssistedInvisibleSightings",
        "antiInvisOpportunitySeconds",
        "valueScore",
        "scoreBreakdown",
        "sampleSightings",
        "firstContacts",
    ]
    return {key: item.get(key) for key in keys if key in item}


def write_html(path: Path, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False)
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>MVP0 Ward Value Report</title>
  <style>
    body {{ margin: 0; font-family: Arial, "Microsoft YaHei", sans-serif; background: #101317; color: #edf3f8; }}
    main {{ max-width: 1280px; margin: 0 auto; padding: 24px; }}
    h1, h2 {{ margin: 18px 0 10px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; }}
    .card {{ border: 1px solid #2c3744; background: #171d24; border-radius: 8px; padding: 12px; }}
    .value {{ font-size: 24px; font-weight: 700; }}
    table {{ width: 100%; border-collapse: collapse; margin: 8px 0 20px; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid #29333f; padding: 7px 8px; text-align: left; vertical-align: top; }}
    th {{ color: #9db0c4; position: sticky; top: 0; background: #151b22; }}
    .muted {{ color: #9aa8b6; }}
    .pill {{ display: inline-block; padding: 2px 7px; border-radius: 999px; background: #243244; color: #cfe2f8; font-size: 12px; }}
    .score {{ font-weight: 700; color: #94f0b8; }}
    .bad {{ color: #ff9d9d; }}
    .tabs button {{ margin-right: 8px; margin-bottom: 8px; height: 32px; border: 1px solid #344252; background: #202a35; color: #edf3f8; border-radius: 6px; cursor: pointer; }}
  </style>
</head>
<body>
<main>
  <h1>MVP0 Ward Value Report</h1>
  <p class="muted">样本：8852716636, 8852757973。Observer 使用 native FoW + 动态树；隐身英雄必须被真眼覆盖才计入可见。</p>
  <div id="summary" class="grid"></div>
  <h2>Leaderboards</h2>
  <div class="tabs" id="tabs"></div>
  <div id="table"></div>
</main>
<script>
const data = {data};
const boards = {{
  "Best Observer": data.leaderboards.observer.bestOverall,
  "Most Seen": data.leaderboards.observer.mostSeen,
  "Best Low Overlap": data.leaderboards.observer.bestLowOverlap,
  "First Contact": data.leaderboards.observer.bestFirstContact,
  "Worst Observer": data.leaderboards.observer.worstLowValue,
  "Fast Dewarded Obs": data.leaderboards.observer.fastDewarded,
  "Best Sentry": data.leaderboards.sentry.bestAntiInvis,
  "Observer-Assisted Sentry": data.leaderboards.sentry.bestObserverAssisted,
  "Best Spots": data.leaderboards.spots.best,
  "Worst Spots": data.leaderboards.spots.worst,
}};
function fmt(v) {{ return v === null || v === undefined ? "" : v; }}
function row(item, isSpot=false) {{
  if (isSpot) return `<tr><td>${{item.spotId}}</td><td>${{item.wardType}}</td><td>${{item.team}}</td><td>${{item.sampleCount}}</td><td class="score">${{item.avgScore}}</td><td>${{item.avgSeenSeconds}}</td><td>${{item.avgLifetimeSeconds}}</td><td>${{fmt(item.dewardRate)}}</td><td>${{item.confidence}}</td></tr>`;
  const bits = item.scoreBreakdown ? Object.entries(item.scoreBreakdown).map(([k,v]) => `${{k}}:${{v}}`).join("<br>") : "";
  return `<tr><td>${{item.matchId}}</td><td>${{item.ehandle}}</td><td>${{item.spotId || ""}}</td><td>${{item.wardType}}</td><td>${{item.team}}</td><td>${{item.start}}</td><td>${{item.lifetimeSeconds}}</td><td class="score">${{item.valueScore}}</td><td>${{item.enemyHeroSeenSeconds || 0}}</td><td>${{item.uniqueHeroesSeen || 0}}</td><td>${{item.firstContactCount || 0}}</td><td>${{item.invisibleHeroTrueSightSeconds || 0}}</td><td>${{item.removedReason}}</td><td>${{bits}}</td></tr>`;
}}
function render(name) {{
  const items = boards[name] || [];
  const isSpot = name.includes("Spots");
  const head = isSpot
    ? "<tr><th>spot</th><th>type</th><th>team</th><th>sample</th><th>score</th><th>seen</th><th>life</th><th>deward</th><th>confidence</th></tr>"
    : "<tr><th>match</th><th>ehandle</th><th>spot</th><th>type</th><th>team</th><th>start</th><th>life</th><th>score</th><th>seen</th><th>unique</th><th>first</th><th>trueSight</th><th>removed</th><th>breakdown</th></tr>";
  document.getElementById("table").innerHTML = `<h3>${{name}}</h3><table>${{head}}${{items.map(item => row(item, isSpot)).join("")}}</table>`;
}}
function init() {{
  const s = data.summary;
  document.getElementById("summary").innerHTML = [
    ["Matches", s.matchCount],
    ["Ward Instances", s.instanceCount],
    ["Observer", s.observerCount],
    ["Sentry", s.sentryCount],
    ["Spots", s.spotCount],
    ["Invisible Data", s.invisibilityDataAvailable ? "available" : "unavailable"],
  ].map(([k,v]) => `<div class="card"><div class="muted">${{k}}</div><div class="value">${{v}}</div></div>`).join("");
  const tabs = document.getElementById("tabs");
  Object.keys(boards).forEach((name, i) => {{
    const btn = document.createElement("button");
    btn.textContent = name;
    btn.onclick = () => render(name);
    tabs.appendChild(btn);
    if (i === 0) render(name);
  }});
}}
init();
</script>
</body>
</html>"""
    path.write_text(html, encoding="utf-8")


def write_mvp1_html(path: Path, payload: dict, map_data_url: str | None = None) -> None:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    template = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>MVP1 Ward Value Report</title>
  <style>
    body { margin: 0; font-family: Arial, "Microsoft YaHei", sans-serif; background: #101317; color: #edf3f8; }
    main { max-width: 1440px; margin: 0 auto; padding: 24px; }
    h1, h2, h3 { margin: 18px 0 10px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; }
    .layout { display: grid; grid-template-columns: minmax(520px, 1.35fr) minmax(360px, 0.85fr); gap: 16px; align-items: start; }
    .card, .panel { border: 1px solid #2c3744; background: #171d24; border-radius: 8px; padding: 12px; }
    .value { font-size: 24px; font-weight: 700; }
    table { width: 100%; border-collapse: collapse; margin: 8px 0 20px; font-size: 13px; }
    th, td { border-bottom: 1px solid #29333f; padding: 7px 8px; text-align: left; vertical-align: top; }
    th { color: #9db0c4; position: sticky; top: 0; background: #151b22; z-index: 1; }
    tr.clickable { cursor: pointer; }
    tr.clickable:hover { background: #202a35; }
    .muted { color: #9aa8b6; }
    .score { font-weight: 700; color: #94f0b8; }
    .tabs button { margin-right: 8px; margin-bottom: 8px; min-height: 32px; border: 1px solid #344252; background: #202a35; color: #edf3f8; border-radius: 6px; cursor: pointer; }
    .tabs button.active { border-color: #79a9ff; color: #d9e8ff; }
    .filters { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin: 12px 0; }
    select, input { height: 32px; border: 1px solid #344252; background: #111820; color: #edf3f8; border-radius: 6px; padding: 0 8px; }
    canvas { width: 100%; height: auto; background: #07090b; border: 1px solid #2a323c; border-radius: 8px; }
    .table-wrap { max-height: 560px; overflow: auto; border: 1px solid #29333f; border-radius: 8px; }
    .kv { display: grid; grid-template-columns: 150px 1fr; gap: 6px; font-size: 13px; }
    .mini { font-size: 12px; line-height: 1.45; }
    @media (max-width: 980px) { .layout { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
<main>
  <h1>MVP1 Ward Value Report</h1>
  <p class="muted">样本：8852716636, 8852757973。Observer 使用 native FoW + 动态树；隐身英雄必须被真眼覆盖才计入可见。本页包含眼位库、排行榜、地图点位展示和单点详情。</p>
  <div id="summary" class="grid"></div>
  <h2>Filters</h2>
  <div class="filters">
    <label>Patch<br><select id="patch"><option value="all">All</option></select></label>
    <label>Ward Type<br><select id="wardType"><option value="all">All</option><option value="obs">Observer</option><option value="sen">Sentry</option></select></label>
    <label>Side<br><select id="team"><option value="all">All</option><option value="radiant">Radiant</option><option value="dire">Dire</option></select></label>
    <label>Match<br><select id="match"><option value="all">All</option></select></label>
    <label>Min Score<br><input id="minScore" type="number" value="0" min="0" max="100" /></label>
    <label>Start From<br><input id="startFrom" type="number" value="-999" /></label>
    <label>Start To<br><input id="startTo" type="number" value="9999" /></label>
  </div>
  <div class="layout">
    <section>
      <h2>Map Spots</h2>
      <canvas id="map" width="851" height="851"></canvas>
      <h2>Leaderboards</h2>
      <div class="tabs" id="tabs"></div>
      <div id="table" class="table-wrap"></div>
    </section>
    <aside class="panel">
      <h2>Spot Detail</h2>
      <div id="detail" class="muted">Click a map point or table row.</div>
    </aside>
  </div>
</main>
<script>
const data = __DATA__;
const mapDataUrl = __MAP_DATA_URL__;
const boards = {
  "Best Observer": data.leaderboards.observer.bestOverall,
  "Most Seen": data.leaderboards.observer.mostSeen,
  "Best Low Overlap": data.leaderboards.observer.bestLowOverlap,
  "First Contact": data.leaderboards.observer.bestFirstContact,
  "Worst Observer": data.leaderboards.observer.worstLowValue,
  "Fast Dewarded Obs": data.leaderboards.observer.fastDewarded,
  "Best Sentry": data.leaderboards.sentry.bestAntiInvis,
  "Observer-Assisted Sentry": data.leaderboards.sentry.bestObserverAssisted,
  "Best Spots": data.leaderboards.spots.best,
  "Worst Spots": data.leaderboards.spots.worst,
};
let currentBoard = "Best Spots";
let selectedSpotId = null;
const spotById = new Map(data.spotDetails.map(s => [s.spotId, s]));
const instanceBySpot = new Map();
for (const instance of data.instances) {
  if (!instanceBySpot.has(instance.spotId)) instanceBySpot.set(instance.spotId, []);
  instanceBySpot.get(instance.spotId).push(instance);
}
function fmt(v) { return v === null || v === undefined ? "" : v; }
function pct(v) { return v === null || v === undefined ? "" : `${Math.round(v * 100)}%`; }
function filters() {
  return {
    patch: document.getElementById("patch").value,
    wardType: document.getElementById("wardType").value,
    team: document.getElementById("team").value,
    match: document.getElementById("match").value,
    minScore: Number(document.getElementById("minScore").value || 0),
    startFrom: Number(document.getElementById("startFrom").value || -9999),
    startTo: Number(document.getElementById("startTo").value || 99999),
  };
}
function passesInstance(item, f) {
  if (f.patch !== "all" && item.mapVersion !== f.patch) return false;
  if (f.wardType !== "all" && item.wardType !== f.wardType) return false;
  if (f.team !== "all" && item.team !== f.team) return false;
  if (f.match !== "all" && String(item.matchId) !== f.match) return false;
  if ((item.valueScore || 0) < f.minScore) return false;
  if ((item.start ?? 0) < f.startFrom || (item.start ?? 0) > f.startTo) return false;
  return true;
}
function passesSpot(spot, f) {
  if (f.patch !== "all" && spot.mapVersion !== f.patch) return false;
  if (f.wardType !== "all" && spot.wardType !== f.wardType) return false;
  if (f.team !== "all" && spot.team !== f.team) return false;
  if ((spot.avgScore || 0) < f.minScore) return false;
  const members = instanceBySpot.get(spot.spotId) || [];
  return members.some(item => passesInstance(item, f));
}
function row(item, isSpot=false) {
  if (isSpot) return `<tr class="clickable" data-spot="${item.spotId}"><td>${item.spotId}</td><td>${item.wardType}</td><td>${item.team}</td><td>${item.sampleCount}</td><td class="score">${item.avgScore}</td><td>${item.avgSeenSeconds}</td><td>${item.avgLifetimeSeconds}</td><td>${pct(item.dewardRate)}</td><td>${item.confidence}</td></tr>`;
  const bits = item.scoreBreakdown ? Object.entries(item.scoreBreakdown).map(([k,v]) => `${k}:${v}`).join("<br>") : "";
  return `<tr class="clickable" data-spot="${item.spotId || ""}"><td>${item.matchId}</td><td>${item.ehandle}</td><td>${item.spotId || ""}</td><td>${item.wardType}</td><td>${item.team}</td><td>${item.start}</td><td>${item.lifetimeSeconds}</td><td class="score">${item.valueScore}</td><td>${item.enemyHeroSeenSeconds || 0}</td><td>${item.uniqueHeroesSeen || 0}</td><td>${item.firstContactCount || 0}</td><td>${item.invisibleHeroTrueSightSeconds || 0}</td><td>${item.removedReason}</td><td>${bits}</td></tr>`;
}
function render(name) {
  currentBoard = name;
  const f = filters();
  let items = boards[name] || [];
  const isSpot = name.includes("Spots");
  items = items.filter(item => isSpot ? passesSpot(item, f) : passesInstance(item, f));
  const head = isSpot
    ? "<tr><th>spot</th><th>type</th><th>team</th><th>sample</th><th>score</th><th>seen</th><th>life</th><th>deward</th><th>confidence</th></tr>"
    : "<tr><th>match</th><th>ehandle</th><th>spot</th><th>type</th><th>team</th><th>start</th><th>life</th><th>score</th><th>seen</th><th>unique</th><th>first</th><th>trueSight</th><th>removed</th><th>breakdown</th></tr>";
  document.getElementById("table").innerHTML = `<h3>${name} <span class="muted">(${items.length})</span></h3><table>${head}${items.map(item => row(item, isSpot)).join("")}</table>`;
  for (const tr of document.querySelectorAll("tr[data-spot]")) tr.onclick = () => selectSpot(tr.dataset.spot);
  drawMap();
}
function selectSpot(spotId) {
  if (!spotId || !spotById.has(spotId)) return;
  selectedSpotId = spotId;
  const spot = spotById.get(spotId);
  const inst = spot.instances || [];
  const best = spot.bestInstance || {};
  document.getElementById("detail").innerHTML = `
    <h3>${spot.spotId}</h3>
    <div class="kv">
      <div class="muted">Type</div><div>${spot.wardType} / ${spot.team}</div>
      <div class="muted">Sample</div><div>${spot.sampleCount} instances, ${spot.matchCount} matches, ${spot.confidence}</div>
      <div class="muted">Avg Score</div><div class="score">${spot.avgScore}</div>
      <div class="muted">Avg Seen</div><div>${spot.avgSeenSeconds}</div>
      <div class="muted">Avg Lifetime</div><div>${spot.avgLifetimeSeconds}s</div>
      <div class="muted">Deward Rate</div><div>${pct(spot.dewardRate)}</div>
      <div class="muted">Center</div><div>${Math.round(spot.centerWorldX)}, ${Math.round(spot.centerWorldY)}</div>
    </div>
    <h3>Best Instance</h3>
    <div class="mini">${best.matchId || ""} e${best.ehandle || ""}, t=${best.start || ""}, score <span class="score">${best.valueScore || ""}</span>, seen=${best.enemyHeroSeenSeconds || 0}, removed=${best.removedReason || ""}</div>
    <h3>Instances</h3>
    <table><tr><th>match</th><th>ehandle</th><th>start</th><th>score</th><th>seen</th><th>trueSight</th><th>removed</th></tr>
    ${inst.map(i => `<tr><td>${i.matchId}</td><td>${i.ehandle}</td><td>${i.start}</td><td class="score">${i.valueScore}</td><td>${i.enemyHeroSeenSeconds || 0}</td><td>${i.invisibleHeroTrueSightSeconds || 0}</td><td>${i.removedReason}</td></tr>`).join("")}</table>
  `;
  drawMap();
}
function drawPoint(ctx, spot, selected=false) {
  if (!spot.pixel) return;
  const x = spot.pixel.x, y = spot.pixel.y;
  const radius = Math.max(4, Math.min(18, 4 + Math.sqrt(spot.sampleCount) * 2 + (spot.avgScore || 0) / 18));
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.fillStyle = spot.wardType === "obs"
    ? (spot.team === "radiant" ? "rgba(66,210,118,0.70)" : "rgba(238,82,82,0.70)")
    : "rgba(80,180,255,0.68)";
  ctx.fill();
  ctx.lineWidth = selected ? 4 : 1.5;
  ctx.strokeStyle = selected ? "rgba(255,255,255,0.96)" : "rgba(10,12,16,0.90)";
  ctx.stroke();
}
function drawMap() {
  const canvas = document.getElementById("map");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (window.baseMap && window.baseMap.complete) ctx.drawImage(window.baseMap, 0, 0, canvas.width, canvas.height);
  else { ctx.fillStyle = "#0b1118"; ctx.fillRect(0, 0, canvas.width, canvas.height); }
  const f = filters();
  for (const spot of data.spotDetails.filter(s => passesSpot(s, f))) drawPoint(ctx, spot, spot.spotId === selectedSpotId);
}
function mapClick(ev) {
  const rect = ev.currentTarget.getBoundingClientRect();
  const x = (ev.clientX - rect.left) * ev.currentTarget.width / rect.width;
  const y = (ev.clientY - rect.top) * ev.currentTarget.height / rect.height;
  const f = filters();
  let best = null, bestD = Infinity;
  for (const spot of data.spotDetails.filter(s => passesSpot(s, f))) {
    if (!spot.pixel) continue;
    const d = Math.hypot(spot.pixel.x - x, spot.pixel.y - y);
    if (d < bestD) { bestD = d; best = spot; }
  }
  if (best && bestD < 24) selectSpot(best.spotId);
}
function init() {
  const s = data.summary;
  document.getElementById("summary").innerHTML = [
    ["Matches", s.matchCount],
    ["Patch", data.source.mapVersion],
    ["Ward Instances", s.instanceCount],
    ["Observer", s.observerCount],
    ["Sentry", s.sentryCount],
    ["Spots", s.spotCount],
    ["Invisible Data", s.invisibilityDataAvailable ? "available" : "unavailable"],
  ].map(([k,v]) => `<div class="card"><div class="muted">${k}</div><div class="value">${v}</div></div>`).join("");
  const patchSelect = document.getElementById("patch");
  const patches = Array.from(new Set([data.source.mapVersion, ...data.instances.map(item => item.mapVersion), ...data.spotDetails.map(item => item.mapVersion)].filter(Boolean))).sort();
  for (const patch of patches) {
    const opt = document.createElement("option");
    opt.value = String(patch);
    opt.textContent = String(patch);
    patchSelect.appendChild(opt);
  }
  const matchSelect = document.getElementById("match");
  for (const mid of s.matches || []) {
    const opt = document.createElement("option");
    opt.value = String(mid);
    opt.textContent = String(mid);
    matchSelect.appendChild(opt);
  }
  const tabs = document.getElementById("tabs");
  Object.keys(boards).forEach((name, i) => {
    const btn = document.createElement("button");
    btn.textContent = name;
    btn.onclick = () => {
      for (const item of tabs.querySelectorAll("button")) item.classList.remove("active");
      btn.classList.add("active");
      render(name);
    };
    tabs.appendChild(btn);
    if (i === 0) btn.classList.add("active");
  });
  for (const id of ["patch", "wardType", "team", "match", "minScore", "startFrom", "startTo"]) {
    document.getElementById(id).addEventListener("input", () => render(currentBoard));
  }
  document.getElementById("map").addEventListener("click", mapClick);
  if (mapDataUrl) {
    window.baseMap = new Image();
    window.baseMap.onload = drawMap;
    window.baseMap.src = mapDataUrl;
  }
  render(currentBoard);
  if (data.spotDetails.length) selectSpot(data.spotDetails[0].spotId);
}
init();
</script>
</body>
</html>"""
    html = template.replace("__DATA__", data)
    html = html.replace("__MAP_DATA_URL__", json.dumps(map_data_url))
    path.write_text(html, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute MVP1 ward value metrics and report.")
    parser.add_argument("--match-id", type=int, action="append", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--database", default=os.environ.get("DOTA_DB_DATABASE", "dota2_analysis"))
    parser.add_argument("--db-host", default=os.environ.get("DOTA_DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.environ.get("DOTA_DB_PORT", "9030")))
    parser.add_argument("--db-user", default=os.environ.get("DOTA_DB_USER", ""))
    parser.add_argument("--db-password", default=os.environ.get("DOTA_DB_PASSWORD", os.environ.get("DB_PASS", "")))
    parser.add_argument("--grid", default=str(RESOURCE_ROOT / "native-fow" / "dota_static_fow_grid.json"))
    parser.add_argument("--cache", default=str(RESOURCE_ROOT / "native-fow" / "cache.fow"))
    parser.add_argument("--tree-points", default=str(RESOURCE_ROOT / "source" / "dota-map-trees.csv"))
    parser.add_argument("--map", default=str(RESOURCE_ROOT / "maps" / "7.41_map.png"))
    parser.add_argument("--projection-calibration", default=str(RESOURCE_ROOT / "calibration" / "projection_741_aerial_14pt.json"))
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    parser.add_argument("--cluster-eps", type=float, default=200.0)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    debug_dir = output_dir / "match_debug"
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    grid = VisibilityGrid.load(args.grid)
    cache = CacheFow.load(args.cache)
    tree_id_cells = load_tree_id_cells(Path(args.tree_points), grid)

    matches = []
    all_instances = []
    with connect(args) as conn:
        with conn.cursor() as cursor:
            for match_id in args.match_id:
                print(f"computing match {match_id}")
                match = compute_match(cursor, match_id, args, grid, cache, tree_id_cells)
                matches.append({key: value for key, value in match.items() if key != "instances"})
                all_instances.extend(match["instances"])
                (debug_dir / f"{match_id}.json").write_text(
                    json.dumps(json_safe(match), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

    invisibility_available = all(match["invisibility"]["available"] for match in matches)
    score_instances(all_instances, invisibility_available)
    instances = finalize_instances(all_instances)
    spots = cluster_spots(instances, args.cluster_eps)
    projection = load_projection(Path(args.projection_calibration))
    for spot in spots:
        spot["pixel"] = world_to_pixel(spot["centerWorldX"], spot["centerWorldY"], projection)
    leaderboards = build_leaderboards(
        [compact_instance(item) for item in instances],
        spots,
    )
    compact_instances = [compact_instance(item) for item in instances]
    spot_details = build_spot_details(spots, instances)
    output = {
        "source": {
            "database": args.database,
            "grid": project_path(Path(args.grid)),
            "cache": project_path(Path(args.cache)),
            "treePoints": project_path(Path(args.tree_points)),
            "map": project_path(Path(args.map)),
            "projectionCalibration": project_path(Path(args.projection_calibration)),
            "mapVersion": MAP_VERSION,
            "observerRadius": OBSERVER_RADIUS,
            "sentryRadius": SENTRY_RADIUS,
            "clusterEpsWorld": args.cluster_eps,
            "invisibilityRule": "invisibility_modifier=true requires allied sentry coverage before observer sight counts",
        },
        "summary": {
            "matchCount": len(matches),
            "matches": args.match_id,
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
    }
    (output_dir / "ward_instances.json").write_text(
        json.dumps(compact_instances, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "ward_spots.json").write_text(
        json.dumps(spots, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "leaderboards.json").write_text(
        json.dumps(leaderboards, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "spot_details.json").write_text(
        json.dumps(spot_details, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "summary.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_mvp1_html(output_dir / "index.html", output, image_data_url(Path(args.map)))
    print(json.dumps(output["summary"], ensure_ascii=False, indent=2))
    print(f"wrote {output_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
