from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


TAU = math.tau
CACHE_TILE_STRUCT = struct.Struct("<III f II f")


@dataclass(frozen=True)
class CacheTileInfo:
    sorted_tile_index: int
    occluder_start: int
    occluder_end: int
    occluder_distance_sq: float
    tree_start: int
    tree_end: int
    tree_distance_sq: float


@dataclass(frozen=True)
class AngularBlocker:
    cell: tuple[int, int]
    relative_cell: tuple[int, int]
    tile_byte: int
    kind: str
    start_angle: float
    end_angle: float
    distance_sq_cells: float


class CacheFow:
    """Reader for Valve's scripts/fow/cache.fow angular lookup table."""

    def __init__(
        self,
        path: Path,
        diameter: int,
        radius_cells: int,
        angles: tuple[float, ...],
        tile_offset: int,
        tile_count: int,
        data: bytes,
    ) -> None:
        self.path = path
        self.diameter = diameter
        self.radius_cells = radius_cells
        self.angles = angles
        self.tile_offset = tile_offset
        self.tile_count = tile_count
        self._data = data

    @classmethod
    def load(cls, path: Path | str) -> "CacheFow":
        path = Path(path)
        data = path.read_bytes()
        if len(data) < 28:
            raise ValueError(f"{path} is too small to be cache.fow")

        diameter, radius, radius_sq_bits, angle_header_count, unk0, unk1 = struct.unpack_from(
            "<6I", data, 0
        )
        radius_sq = struct.unpack("<f", struct.pack("<I", radius_sq_bits))[0]
        if diameter != radius * 2 + 1 or int(radius_sq) != radius * radius:
            raise ValueError("unexpected cache.fow radius header")
        if unk0 != 0 or unk1 != 0:
            raise ValueError("unexpected cache.fow header tail")

        angle_count = angle_header_count - 1
        angles_offset = 24
        tile_count_offset = angles_offset + angle_count * 8
        angles = struct.unpack_from(f"<{angle_count}d", data, angles_offset)
        tile_count = struct.unpack_from("<I", data, tile_count_offset)[0]
        if tile_count != diameter * diameter:
            raise ValueError("unexpected cache.fow tile count")

        tile_offset = tile_count_offset + 4
        expected_size = tile_offset + tile_count * CACHE_TILE_STRUCT.size
        if expected_size != len(data):
            raise ValueError("unexpected cache.fow file size")
        return cls(path, diameter, radius, tuple(angles), tile_offset, tile_count, data)

    def angle(self, index: int) -> float:
        if 0 <= index < len(self.angles):
            return self.angles[index]
        if index == len(self.angles):
            return TAU
        raise IndexError(f"angle index out of range: {index}")

    def tile(self, dx: int, dy: int) -> Optional[CacheTileInfo]:
        if abs(dx) > self.radius_cells or abs(dy) > self.radius_cells:
            return None
        index = (dy + self.radius_cells) * self.diameter + (dx + self.radius_cells)
        if not 0 <= index < self.tile_count:
            return None
        values = CACHE_TILE_STRUCT.unpack_from(
            self._data, self.tile_offset + index * CACHE_TILE_STRUCT.size
        )
        return CacheTileInfo(*values)

    def interval(self, dx: int, dy: int, kind: str) -> Optional[tuple[float, float, float]]:
        info = self.tile(dx, dy)
        if info is None:
            return None
        if kind == "tree":
            if not math.isfinite(info.tree_distance_sq):
                return None
            return (
                self.angle(info.tree_start),
                self.angle(info.tree_end),
                info.tree_distance_sq,
            )
        if not math.isfinite(info.occluder_distance_sq):
            return None
        return (
            self.angle(info.occluder_start),
            self.angle(info.occluder_end),
            info.occluder_distance_sq,
        )


class VisibilityGrid:
    """Native FoW tile-byte grid reconstructed from dota.vhcg and map entities."""

    def __init__(
        self,
        width: int,
        height: int,
        origin_x: float,
        origin_y: float,
        cell_size: float,
        tile_bytes: list[int],
        hard_visible: Optional[list[bool]],
        metadata: dict,
    ) -> None:
        if len(tile_bytes) != width * height:
            raise ValueError("tile byte count does not match grid dimensions")
        self.width = width
        self.height = height
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.cell_size = cell_size
        self.tile_bytes = bytearray(value & 0xFF for value in tile_bytes)
        self.hard_visible = hard_visible
        self.metadata = metadata

    @classmethod
    def load(cls, path: Path | str) -> "VisibilityGrid":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        origin = payload.get("origin", [0.0, 0.0])
        return cls(
            int(payload["width"]),
            int(payload["height"]),
            float(origin[0]),
            float(origin[1]),
            float(payload.get("cell_size", 64.0)),
            [int(value) for value in payload["tile_bytes"]],
            payload.get("hard_visible"),
            payload.get("metadata", {}),
        )

    def world_to_cell(self, x: float, y: float) -> tuple[int, int]:
        return (
            math.floor((x - self.origin_x) / self.cell_size),
            math.floor((y - self.origin_y) / self.cell_size),
        )

    def cell_center(self, x: int, y: int) -> tuple[float, float]:
        return (
            self.origin_x + (x + 0.5) * self.cell_size,
            self.origin_y + (y + 0.5) * self.cell_size,
        )

    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def tile_byte_at(self, x: int, y: int) -> Optional[int]:
        if not self.in_bounds(x, y):
            return None
        return self.tile_bytes[y * self.width + x]

    def hard_visible_at(self, x: int, y: int) -> bool:
        if not self.in_bounds(x, y):
            return False
        if self.hard_visible is None:
            return True
        return bool(self.hard_visible[y * self.width + x])


def normalize_angle(angle: float) -> float:
    angle = math.fmod(angle, TAU)
    return angle + TAU if angle < 0 else angle


def angle_in_interval(angle: float, start: float, end: float) -> bool:
    angle = normalize_angle(angle)
    start = normalize_angle(start)
    end = normalize_angle(end)
    if start <= end:
        return start <= angle <= end
    return angle >= start or angle <= end


def select_blocker_kind(
    tile_byte: int, viewer_height: int, block_mask: int = 0xE0
) -> Optional[str]:
    masked = tile_byte & block_mask
    if masked == 0:
        return None

    tile_height = tile_byte & 0x1F
    if masked & 0x80:
        if tile_height + 1 < viewer_height:
            return "occluder" if masked & 0x40 else None
        return "tree"
    if tile_height <= viewer_height:
        return "occluder" if masked & 0x40 else None
    return "occluder" if masked & 0x20 else None


def build_angular_blockers(
    grid: VisibilityGrid,
    cache: CacheFow,
    origin: tuple[int, int],
    radius_cells: float,
    viewer_height: int,
    block_mask: int = 0xE0,
) -> list[AngularBlocker]:
    scan_radius = min(cache.radius_cells, math.ceil(radius_cells) + 2)
    blockers: list[AngularBlocker] = []
    ox, oy = origin
    for dy in range(-scan_radius, scan_radius + 1):
        y = oy + dy
        if not 0 <= y < grid.height:
            continue
        for dx in range(-scan_radius, scan_radius + 1):
            if dx == 0 and dy == 0:
                continue
            x = ox + dx
            if not 0 <= x < grid.width:
                continue
            tile_byte = grid.tile_bytes[y * grid.width + x]
            kind = select_blocker_kind(tile_byte, viewer_height, block_mask)
            if kind is None:
                continue
            interval = cache.interval(dx, dy, kind)
            if interval is None:
                continue
            start, end, distance_sq = interval
            blockers.append(
                AngularBlocker(
                    cell=(x, y),
                    relative_cell=(dx, dy),
                    tile_byte=tile_byte,
                    kind=kind,
                    start_angle=start,
                    end_angle=end,
                    distance_sq_cells=distance_sq,
                )
            )
    return blockers


def visible_cells(
    grid: VisibilityGrid,
    cache: CacheFow,
    world_x: float,
    world_y: float,
    radius_world: float,
) -> tuple[list[list[int]], dict]:
    origin = grid.world_to_cell(world_x, world_y)
    if not grid.in_bounds(*origin):
        return [], {"invalid": True, "originCell": list(origin)}

    origin_tile = grid.tile_byte_at(*origin) or 0
    viewer_height = origin_tile & 0x1F
    radius_cells = radius_world / grid.cell_size
    blockers = build_angular_blockers(
        grid, cache, origin, radius_cells, viewer_height
    )

    cells: list[list[int]] = []
    blocked_by_kind = {"tree": 0, "occluder": 0}
    search_radius = math.ceil(radius_cells)
    ox, oy = origin
    for dy in range(-search_radius, search_radius + 1):
        for dx in range(-search_radius, search_radius + 1):
            distance_sq = dx * dx + dy * dy
            if distance_sq > radius_cells * radius_cells:
                continue
            x, y = ox + dx, oy + dy
            if not grid.hard_visible_at(x, y):
                continue
            if distance_sq == 0:
                cells.append([x, y])
                continue

            target_angle = normalize_angle(math.atan2(dx, dy))
            blocker_hit = None
            for blocker in blockers:
                if distance_sq <= blocker.distance_sq_cells:
                    continue
                if angle_in_interval(
                    target_angle, blocker.start_angle, blocker.end_angle
                ):
                    blocker_hit = blocker
                    break
            if blocker_hit is None:
                cells.append([x, y])
            else:
                blocked_by_kind[blocker_hit.kind] += 1

    return cells, {
        "invalid": False,
        "originCell": list(origin),
        "viewerHeight": viewer_height,
        "candidateCellCount": sum(
            1
            for dy in range(-search_radius, search_radius + 1)
            for dx in range(-search_radius, search_radius + 1)
            if dx * dx + dy * dy <= radius_cells * radius_cells
        ),
        "visibleCellCount": len(cells),
        "angularBlockerCount": len(blockers),
        "blockedByKind": blocked_by_kind,
    }
