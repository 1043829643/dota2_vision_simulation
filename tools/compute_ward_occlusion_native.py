from __future__ import annotations

import argparse
import json
from pathlib import Path

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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute observer ward vision with Valve cache.fow rules."
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--grid", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--radius", type=float, default=1600.0)
    args = parser.parse_args()

    input_path = Path(args.input)
    grid_path = Path(args.grid)
    cache_path = Path(args.cache)
    output_path = Path(args.output)

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    grid = VisibilityGrid.load(grid_path)
    cache = CacheFow.load(cache_path)
    observer_wards = [ward for ward in payload["wards"] if ward["type"] == "obs"]

    results = []
    total_visible = 0
    total_candidates = 0
    for index, ward in enumerate(observer_wards, start=1):
        world_x, world_y = parser_to_world(float(ward["x"]), float(ward["y"]))
        cells, stats = visible_cells(
            grid, cache, world_x, world_y, float(args.radius)
        )
        total_visible += stats.get("visibleCellCount", 0)
        total_candidates += stats.get("candidateCellCount", 0)
        results.append(
            {
                "ehandle": int(ward["ehandle"]),
                "type": "obs",
                "world": {"x": world_x, "y": world_y},
                "grid": {
                    "x": stats["originCell"][0],
                    "y": stats["originCell"][1],
                    "key": f"{stats['originCell'][0]},{stats['originCell'][1]}",
                },
                "originGrid": {
                    "x": stats["originCell"][0],
                    "y": stats["originCell"][1],
                },
                "snapped": False,
                "viewerHeight": stats.get("viewerHeight"),
                "candidateCellCount": stats.get("candidateCellCount"),
                "lightArea": stats.get("visibleCellCount"),
                "angularBlockerCount": stats.get("angularBlockerCount"),
                "blockedByKind": stats.get("blockedByKind"),
                "invalid": stats.get("invalid", False),
                "cells": cells,
            }
        )
        if index % 10 == 0 or index == len(observer_wards):
            print(f"computed {index}/{len(observer_wards)}")

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
                "treeDeathsAndRespawnsApplied": False,
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
