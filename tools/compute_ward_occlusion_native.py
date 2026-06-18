from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path

import pymysql

from native_fow import CacheFow, VisibilityGrid, visible_cells


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORLD_UNITS_PER_PARSER_UNIT = 128.0
WORLD_PARSER_OFFSET = 16384.0


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


def connect_database():
    return pymysql.connect(
        host=os.environ.get("DOTA_DB_HOST", os.environ.get("DB_HOST", "127.0.0.1")),
        port=int(os.environ.get("DOTA_DB_PORT", os.environ.get("DB_PORT", "9030"))),
        user=os.environ.get("DOTA_DB_USER", os.environ.get("DB_USER", "")),
        password=os.environ.get("DOTA_DB_PASSWORD", os.environ.get("DB_PASS", "")),
        database=os.environ.get("DOTA_DB_DATABASE", os.environ.get("DB_DATABASE")) or None,
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=90,
        cursorclass=pymysql.cursors.DictCursor,
    )


def load_tree_event_rows(args, match_id: int) -> tuple[list[dict], dict]:
    if args.tree_events:
        path = Path(args.tree_events)
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("events", [])
        return rows, {"kind": "json", "path": project_path(path)}
    if args.tree_events_sql:
        with connect_database() as conn:
            with conn.cursor() as cursor:
                cursor.execute(args.tree_events_sql, (match_id,))
                rows = list(cursor.fetchall())
        return rows, {"kind": "database", "query": args.tree_events_sql}
    return [], {"kind": "none"}


def first_value(row: dict, *names):
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None:
            return value
    return None


def load_tree_id_cells(path: Path | None, grid: VisibilityGrid) -> dict[int, tuple[int, int]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    trees = payload if isinstance(payload, list) else payload.get("trees", [])
    result = {}
    for tree in trees:
        tree_id = first_value(tree, "treeId", "tree_id", "id")
        x = first_value(tree, "x", "world_x")
        y = first_value(tree, "y", "world_y")
        if tree_id is None or x is None or y is None:
            continue
        cell = grid.world_to_cell(float(x), float(y))
        if grid.in_bounds(*cell):
            result[int(tree_id)] = cell
    return result


def event_alive(row: dict) -> bool:
    alive = first_value(row, "alive", "is_alive", "tree_alive")
    if alive is not None:
        if isinstance(alive, str):
            return alive.strip().lower() in {"1", "true", "alive", "respawn", "spawn"}
        return bool(alive)
    action = str(first_value(row, "event", "event_type", "action", "type") or "").lower()
    if any(token in action for token in ("respawn", "spawn", "alive", "grow")):
        return True
    if any(token in action for token in ("death", "destroy", "cut", "kill", "dead")):
        return False
    raise ValueError(f"cannot determine tree event action from row: {row}")


def normalize_tree_events(
    rows: list[dict],
    grid: VisibilityGrid,
    tree_id_cells: dict[int, tuple[int, int]],
) -> tuple[list[dict], list[dict]]:
    events = []
    rejected = []
    for source_index, row in enumerate(rows):
        try:
            raw_time = first_value(row, "time", "game_time", "timestamp", "event_time")
            if raw_time is None:
                raise ValueError("missing time")
            effective_second = math.ceil(float(raw_time))

            grid_x = first_value(row, "grid_x", "cell_x", "fow_x")
            grid_y = first_value(row, "grid_y", "cell_y", "fow_y")
            if grid_x is not None and grid_y is not None:
                cell = (int(grid_x), int(grid_y))
            else:
                tree_id = first_value(row, "tree_id", "treeid", "id")
                if tree_id is not None and int(tree_id) in tree_id_cells:
                    cell = tree_id_cells[int(tree_id)]
                else:
                    world_x = first_value(row, "world_x", "x")
                    world_y = first_value(row, "world_y", "y")
                    parser_x = first_value(row, "parser_x")
                    parser_y = first_value(row, "parser_y")
                    if world_x is None or world_y is None:
                        if parser_x is None or parser_y is None:
                            raise ValueError("missing tree cell, known tree_id, or coordinates")
                        world_x, world_y = parser_to_world(float(parser_x), float(parser_y))
                    cell = grid.world_to_cell(float(world_x), float(world_y))

            if not grid.in_bounds(*cell):
                raise ValueError(f"cell outside grid: {cell}")
            if not (grid.tile_byte_at(*cell) & 0x80):
                raise ValueError(f"cell is not an initial static tree cell: {cell}")
            events.append(
                {
                    "time": float(raw_time),
                    "second": effective_second,
                    "cell": [cell[0], cell[1]],
                    "alive": event_alive(row),
                    "sourceIndex": source_index,
                }
            )
        except (TypeError, ValueError) as exc:
            rejected.append(
                {"sourceIndex": source_index, "reason": str(exc), "row": row}
            )
    events.sort(key=lambda event: (event["second"], event["sourceIndex"]))
    return events, rejected


def append_vision_segment(segments: list[dict], second: int, cells: list[list[int]], stats: dict):
    if segments and segments[-1]["cells"] == cells:
        segments[-1]["end"] = second + 1
        return
    segments.append(
        {
            "start": second,
            "end": second + 1,
            "cells": cells,
            "lightArea": stats.get("visibleCellCount", 0),
            "viewerHeight": stats.get("viewerHeight"),
            "angularBlockerCount": stats.get("angularBlockerCount"),
            "blockedByKind": stats.get("blockedByKind"),
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute observer ward vision with Valve cache.fow rules."
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--grid", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--radius", type=float, default=1600.0)
    parser.add_argument(
        "--tree-events",
        help="JSON list/object containing tree death and respawn events.",
    )
    parser.add_argument(
        "--tree-events-sql",
        default=os.environ.get("DOTA_TREE_EVENTS_SQL"),
        help=(
            "Parameterized SQL returning tree events. It must contain one %s "
            "placeholder for match_id; DB credentials come from DOTA_DB_*."
        ),
    )
    parser.add_argument(
        "--tree-points",
        help="Optional trees JSON used to resolve tree_id to world coordinates.",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    grid_path = Path(args.grid)
    cache_path = Path(args.cache)
    output_path = Path(args.output)

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    grid = VisibilityGrid.load(grid_path)
    cache = CacheFow.load(cache_path)
    observer_wards = [ward for ward in payload["wards"] if ward["type"] == "obs"]
    event_rows, event_source = load_tree_event_rows(args, int(payload["match_id"]))
    tree_id_cells = load_tree_id_cells(
        Path(args.tree_points) if args.tree_points else None, grid
    )
    tree_events, rejected_tree_events = normalize_tree_events(
        event_rows, grid, tree_id_cells
    )

    results = []
    total_visible = 0
    total_candidates = 0
    ward_results = {}
    for ward in observer_wards:
        world_x, world_y = parser_to_world(float(ward["x"]), float(ward["y"]))
        ward_results[int(ward["ehandle"])] = {
            "ward": ward,
            "world": (world_x, world_y),
            "segments": [],
        }

    start = min(int(ward["start"]) for ward in observer_wards)
    ward_ends = [ward.get("end") for ward in observer_wards if ward.get("end") is not None]
    end = max(int(value) for value in ward_ends)
    events_by_second = {}
    for event in tree_events:
        events_by_second.setdefault(event["second"], []).append(event)
    state_version = 0
    applied_event_count = 0
    visibility_cache = {}

    for event in (event for event in tree_events if event["second"] < start):
        if grid.set_tree_alive(event["cell"][0], event["cell"][1], event["alive"]):
            state_version += 1
        applied_event_count += 1

    for second in range(start, end):
        for event in events_by_second.get(second, []):
            if grid.set_tree_alive(event["cell"][0], event["cell"][1], event["alive"]):
                state_version += 1
            applied_event_count += 1

        active_wards = [
            item
            for item in ward_results.values()
            if int(item["ward"]["start"]) <= second
            and (
                item["ward"].get("end") is None
                or second < int(item["ward"]["end"])
            )
        ]
        for item in active_wards:
            ehandle = int(item["ward"]["ehandle"])
            cache_key = (ehandle, state_version)
            if cache_key not in visibility_cache:
                visibility_cache[cache_key] = visible_cells(
                    grid,
                    cache,
                    item["world"][0],
                    item["world"][1],
                    float(args.radius),
                )
            cells, stats = visibility_cache[cache_key]
            append_vision_segment(item["segments"], second, cells, stats)

        if (second - start + 1) % 300 == 0 or second == end - 1:
            print(f"computed timeline {second - start + 1}/{end - start} seconds")

    for item in ward_results.values():
        ward = item["ward"]
        segments = item["segments"]
        if not segments:
            continue
        first = segments[0]
        origin = grid.world_to_cell(*item["world"])
        total_visible += first["lightArea"]
        candidate_count = sum(
            1
            for dy in range(-math.ceil(args.radius / grid.cell_size), math.ceil(args.radius / grid.cell_size) + 1)
            for dx in range(-math.ceil(args.radius / grid.cell_size), math.ceil(args.radius / grid.cell_size) + 1)
            if dx * dx + dy * dy <= (args.radius / grid.cell_size) ** 2
        )
        total_candidates += candidate_count
        results.append(
            {
                "ehandle": int(ward["ehandle"]),
                "type": "obs",
                "world": {"x": item["world"][0], "y": item["world"][1]},
                "grid": {"x": origin[0], "y": origin[1], "key": f"{origin[0]},{origin[1]}"},
                "originGrid": {"x": origin[0], "y": origin[1]},
                "snapped": False,
                "viewerHeight": first.get("viewerHeight"),
                "candidateCellCount": candidate_count,
                "lightArea": first["lightArea"],
                "angularBlockerCount": first.get("angularBlockerCount"),
                "blockedByKind": first.get("blockedByKind"),
                "invalid": False,
                "cells": first["cells"],
                "visionTimeline": segments,
            }
        )

    grid_meta = grid.metadata
    output = {
        "match_id": payload.get("match_id"),
        "source": {
            "engine": "valve-cache-fow",
            "input": project_path(input_path),
            "gridData": project_path(grid_path),
            "cacheFow": project_path(cache_path),
            "cellSize": grid.cell_size,
            "cellCenterOffset": 0.5,
            "visionRadiusWorld": float(args.radius),
            "grid": {
                "width": grid.width,
                "height": grid.height,
                "worldMinX": grid.origin_x,
                "worldMinY": grid.origin_y,
                "worldMaxX": grid.origin_x + grid.width * grid.cell_size,
                "worldMaxY": grid.origin_y + grid.height * grid.cell_size,
            },
            "tileByteLayout": {
                "height": "bits 0..4",
                "heightEdge": "0x20",
                "explicitFowBlocker": "0x40",
                "tree": "0x80",
            },
            "heightThresholds": grid_meta.get("height_thresholds"),
            "flagCounts": grid_meta.get("tile_byte_flag_counts"),
            "treeCount": grid_meta.get("tree_count"),
            "fowBlockerNodeCount": grid_meta.get("fow_blocker_node_count"),
            "parserToWorld": {
                "x": "parser_x * 128 - 16384",
                "y": "parser_y * 128 - 16384",
            },
            "algorithm": (
                "Valve cache.fow angular intervals with native FoW tile-byte "
                "height/tree/explicit-blocker selection rules"
            ),
            "dynamicState": {
                "treeDeathsAndRespawnsApplied": bool(tree_events),
                "treeEventSource": event_source,
                "treeEventRows": len(event_rows),
                "treeEventsAccepted": len(tree_events),
                "treeEventsRejected": len(rejected_tree_events),
                "treeEventsAppliedInTimeline": applied_event_count,
                "treeStateVersions": state_version + 1,
                "dynamicBlockersApplied": False,
            },
        },
        "observerWardCount": len(observer_wards),
        "summary": {
            "candidateCells": total_candidates,
            "visibleCells": total_visible,
            "visibleRatio": (
                total_visible / total_candidates if total_candidates else 0.0
            ),
        },
        "treeEvents": tree_events,
        "rejectedTreeEvents": rejected_tree_events,
        "results": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
