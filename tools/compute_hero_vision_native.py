from __future__ import annotations

import argparse
import json
import math
import os
import re
from collections import defaultdict
from pathlib import Path

import pymysql

from native_fow import CacheFow, VisibilityGrid, visible_cells


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = PROJECT_ROOT / "resources"
WORLD_UNITS_PER_PARSER_UNIT = 128.0
WORLD_PARSER_OFFSET = 16384.0
DEFAULT_DAY_VISION = 1800.0
DEFAULT_NIGHT_VISION = 800.0


def project_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def parser_to_world(x: float, y: float) -> tuple[float, float]:
    return (
        x * WORLD_UNITS_PER_PARSER_UNIT - WORLD_PARSER_OFFSET,
        y * WORLD_UNITS_PER_PARSER_UNIT - WORLD_PARSER_OFFSET,
    )


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


def side_from_team_num(team) -> str:
    return "radiant" if int(team) == 2 else "dire"


def parse_hero_vision(path: Path) -> dict[str, dict[str, float]]:
    text = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    heroes: dict[str, dict[str, float]] = {}
    current = None
    pending_name = None
    depth = 0
    name_re = re.compile(r'^\s*"([^"]+)"\s*$')
    kv_re = re.compile(r'^\s*"([^"]+)"\s+"([^"]*)"')

    for line in text:
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        name_match = name_re.match(line)
        if name_match and depth == 1:
            pending_name = name_match.group(1)
        if "{" in stripped:
            depth += stripped.count("{")
            if pending_name and depth == 2:
                current = pending_name
                heroes.setdefault(current, {})
                pending_name = None
            continue
        if "}" in stripped:
            if current and depth == 2:
                current = None
            depth -= stripped.count("}")
            continue
        if current:
            kv = kv_re.match(line)
            if kv and kv.group(1) in ("VisionDaytimeRange", "VisionNighttimeRange"):
                try:
                    heroes[current][kv.group(1)] = float(kv.group(2))
                except ValueError:
                    pass

    base = heroes.get("npc_dota_hero_base", {})
    base_day = base.get("VisionDaytimeRange", DEFAULT_DAY_VISION)
    base_night = base.get("VisionNighttimeRange", DEFAULT_NIGHT_VISION)
    result = {}
    for name, values in heroes.items():
        if not name.startswith("npc_dota_hero_") or name == "npc_dota_hero_base":
            continue
        result[name] = {
            "day": values.get("VisionDaytimeRange", base_day),
            "night": values.get("VisionNighttimeRange", base_night),
        }
    return result


def is_daytime(second: int) -> bool:
    if second < 0:
        return True
    return second % 600 < 300


def load_players(cursor, match_id: int, hero_vision: dict[str, dict[str, float]]) -> dict[int, dict]:
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
        hero_name = str(row.get("hero_name") or "")
        vision = hero_vision.get(
            hero_name,
            {"day": DEFAULT_DAY_VISION, "night": DEFAULT_NIGHT_VISION},
        )
        players[slot] = {
            "slot": slot,
            "steamid": None if row.get("steamid") is None else int(row["steamid"]),
            "heroName": hero_name,
            "heroId": None if row.get("hero_id") is None else int(row["hero_id"]),
            "persona": row.get("persona"),
            "team": side_from_team_num(row["team"]),
            "visionDay": vision["day"],
            "visionNight": vision["night"],
        }
    if len(players) != 10:
        raise ValueError(f"expected 10 players for match {match_id}, got {len(players)}")
    return players


def load_positions(cursor, match_id: int, start: int, end: int) -> dict[int, list[dict]]:
    rows = fetch_all(
        cursor,
        """
SELECT time,slot,unit,life_state,x,y
FROM player_intervals2
WHERE CAST(match_id AS BIGINT)=%s
  AND time BETWEEN %s AND %s
  AND x <> ''
  AND y <> ''
  AND life_state='0'
ORDER BY time,slot,log_index
""",
        (match_id, start, end),
    )
    by_second: dict[int, list[dict]] = defaultdict(list)
    seen = set()
    for row in rows:
        key = (int(row["time"]), int(row["slot"]))
        if key in seen:
            continue
        seen.add(key)
        by_second[key[0]].append(
            {
                "slot": key[1],
                "unit": row.get("unit"),
                "x": float(row["x"]),
                "y": float(row["y"]),
            }
        )
    return by_second


def position_time_bounds(cursor, match_id: int) -> tuple[int, int]:
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


def cells_to_rle(cells: set[tuple[int, int]]) -> list[list[int]]:
    by_row: dict[int, list[int]] = defaultdict(list)
    for x, y in cells:
        by_row[y].append(x)
    runs = []
    for y in sorted(by_row):
        xs = sorted(set(by_row[y]))
        start = prev = xs[0]
        for x in xs[1:]:
            if x == prev + 1:
                prev = x
                continue
            runs.append([y, start, prev])
            start = prev = x
        runs.append([y, start, prev])
    return runs


def apply_tree_events_before(grid: VisibilityGrid, events: list[dict], start: int) -> tuple[int, int]:
    version = 0
    applied = 0
    for event in events:
        if int(event["second"]) >= start:
            continue
        if grid.set_tree_alive(event["cell"][0], event["cell"][1], bool(event["alive"])):
            version += 1
        applied += 1
    return version, applied


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute per-second native FoW hero vision.")
    parser.add_argument("--match-id", type=int, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--grid", default=str(RESOURCE_ROOT / "native-fow" / "dota_static_fow_grid.json"))
    parser.add_argument("--cache", default=str(RESOURCE_ROOT / "native-fow" / "cache.fow"))
    parser.add_argument("--npc-heroes", default=str(RESOURCE_ROOT / "native-fow" / "scripts" / "npc" / "npc_heroes.txt"))
    parser.add_argument("--occlusion-cells", help="ward_occlusion_cells.json containing accepted tree events.")
    parser.add_argument("--database", default=os.environ.get("DOTA_DB_DATABASE", "dota2_analysis"))
    parser.add_argument("--db-host", default=os.environ.get("DOTA_DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.environ.get("DOTA_DB_PORT", "9030")))
    parser.add_argument("--db-user", default=os.environ.get("DOTA_DB_USER", ""))
    parser.add_argument("--db-password", default=os.environ.get("DOTA_DB_PASSWORD", os.environ.get("DB_PASS", "")))
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    args = parser.parse_args()

    grid = VisibilityGrid.load(args.grid)
    cache = CacheFow.load(args.cache)
    hero_vision = parse_hero_vision(Path(args.npc_heroes))
    tree_events = []
    tree_event_source = {"kind": "none"}
    if args.occlusion_cells:
        occlusion = json.loads(Path(args.occlusion_cells).read_text(encoding="utf-8"))
        tree_events = occlusion.get("treeEvents", [])
        tree_event_source = {
            "kind": "ward_occlusion_cells",
            "path": project_path(Path(args.occlusion_cells)),
        }

    with connect(args) as conn:
        with conn.cursor() as cursor:
            min_time, max_time = position_time_bounds(cursor, args.match_id)
            start = max(0, args.start if args.start is not None else min_time)
            end = args.end if args.end is not None else max_time
            players = load_players(cursor, args.match_id, hero_vision)
            positions_by_second = load_positions(cursor, args.match_id, start, end)

    events_by_second: dict[int, list[dict]] = defaultdict(list)
    for event in tree_events:
        events_by_second[int(event["second"])].append(event)
    state_version, applied_before = apply_tree_events_before(grid, tree_events, start)
    applied_in_window = 0
    visibility_cache = {}
    seconds = []
    total_hero_positions = 0
    total_runs = {"radiant": 0, "dire": 0}

    for second in range(start, end + 1):
        for event in events_by_second.get(second, []):
            if grid.set_tree_alive(event["cell"][0], event["cell"][1], bool(event["alive"])):
                state_version += 1
            applied_in_window += 1

        team_cells = {"radiant": set(), "dire": set()}
        heroes = []
        daytime = is_daytime(second)
        for position in positions_by_second.get(second, []):
            player = players.get(int(position["slot"]))
            if not player:
                continue
            radius = player["visionDay"] if daytime else player["visionNight"]
            world_x, world_y = parser_to_world(position["x"], position["y"])
            origin_cell = grid.world_to_cell(world_x, world_y)
            cache_key = (
                int(position["slot"]),
                origin_cell[0],
                origin_cell[1],
                int(radius),
                state_version,
            )
            if cache_key not in visibility_cache:
                visibility_cache[cache_key] = visible_cells(
                    grid, cache, world_x, world_y, float(radius)
                )[0]
            cells = {tuple(cell) for cell in visibility_cache[cache_key]}
            team_cells[player["team"]].update(cells)
            total_hero_positions += 1
            heroes.append(
                {
                    "slot": int(position["slot"]),
                    "team": player["team"],
                    "heroName": player["heroName"],
                    "persona": player["persona"],
                    "x": position["x"],
                    "y": position["y"],
                    "worldX": world_x,
                    "worldY": world_y,
                    "grid": [origin_cell[0], origin_cell[1]],
                    "radius": radius,
                }
            )

        radiant_runs = cells_to_rle(team_cells["radiant"])
        dire_runs = cells_to_rle(team_cells["dire"])
        total_runs["radiant"] += len(radiant_runs)
        total_runs["dire"] += len(dire_runs)
        seconds.append(
            {
                "time": second,
                "daytime": daytime,
                "radiant": radiant_runs,
                "dire": dire_runs,
                "heroes": heroes,
            }
        )
        if (second - start + 1) % 300 == 0 or second == end:
            print(f"computed hero vision {second - start + 1}/{end - start + 1} seconds")

    output = {
        "match_id": args.match_id,
        "source": {
            "engine": "valve-cache-fow-hero-vision",
            "database": args.database,
            "gridData": project_path(Path(args.grid)),
            "cacheFow": project_path(Path(args.cache)),
            "npcHeroes": project_path(Path(args.npc_heroes)),
            "treeEventSource": tree_event_source,
            "treeEventsAppliedBeforeStart": applied_before,
            "treeEventsAppliedInTimeline": applied_in_window,
            "treeStateVersions": state_version + 1,
            "aliveFilter": "player_intervals2.life_state='0'",
            "dayNightRule": "day when second % 600 < 300, otherwise night",
            "cellEncoding": "row run-length encoding [y, xStart, xEnd]",
        },
        "start": start,
        "end": end,
        "players": [players[slot] for slot in sorted(players)],
        "summary": {
            "seconds": len(seconds),
            "heroPositions": total_hero_positions,
            "radiantRuns": total_runs["radiant"],
            "direRuns": total_runs["dire"],
        },
        "seconds": seconds,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
