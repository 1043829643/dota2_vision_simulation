import argparse
import importlib.util
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def load_render_module(project_root):
    path = project_root / "tools" / "render_ward_vision.py"
    spec = importlib.util.spec_from_file_location("render_ward_vision", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_rgba_grid(path):
    meta = json.loads((Path(str(path) + ".json")).read_text(encoding="utf-8"))
    data = Path(path).read_bytes()
    return meta, data


def pixel(data, width, x, y):
    idx = ((y * width) + x) * 4
    return data[idx], data[idx + 1], data[idx + 2], data[idx + 3]


def image_to_grid_y(grid_h, image_y):
    return grid_h - image_y - 1


def grid_to_image_y(grid_h, grid_y):
    return grid_h - grid_y - 1


def add_disk(target, gx, gy, radius):
    r = int(math.ceil(radius))
    for dx in range(-r, r + 1):
        for dy in range(-r, r + 1):
            if dx * dx + dy * dy > radius * radius:
                continue
            target.add((gx + dx, gy + dy))


def cluster_pad(points, min_cells, radius):
    if not radius or not min_cells:
        return set(points)
    points = set(points)
    visited = set()
    out = set(points)
    for start in list(points):
        if start in visited:
            continue
        stack = [start]
        visited.add(start)
        comp = []
        while stack:
            gx, gy = stack.pop()
            comp.append((gx, gy))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nxt = (gx + dx, gy + dy)
                    if nxt in points and nxt not in visited:
                        visited.add(nxt)
                        stack.append(nxt)
        if len(comp) < min_cells:
            continue
        for gx, gy in comp:
            add_disk(out, gx, gy, radius)
    return out


def rasterize_fow_lines(blockers, fow_payload, mod, cell_size, max_segment_length, line_radius_world):
    if not fow_payload:
        return
    radius = line_radius_world / cell_size
    for seg in fow_payload.get("segments", []):
        if seg.get("length", 0) > max_segment_length:
            continue
        a = seg["a"]
        b = seg["b"]
        steps = max(1, math.ceil(seg["length"] / (cell_size / 2)))
        for i in range(steps + 1):
            t = i / steps
            wx = a["x"] + (b["x"] - a["x"]) * t
            wy = a["y"] + (b["y"] - a["y"]) * t
            gx = round((wx - mod.VISION_WORLD_MIN_X) / cell_size)
            gy = round((wy - mod.VISION_WORLD_MIN_Y) / cell_size)
            add_disk(blockers, gx, gy, radius)


def build_blockers(map_data, occlusion_source, ward_grid, ward_z, mod):
    meta, raw = load_rgba_grid(map_data)
    raw_w = int(meta["width"])
    raw_h = int(meta["height"])
    grid_w = int((occlusion_source.get("grid") or {}).get("width") or (raw_w // 5))
    grid_h = int((occlusion_source.get("grid") or {}).get("height") or raw_h)
    cell_size = float(occlusion_source.get("cellSize") or 64)
    blocker_pad = int(occlusion_source.get("blockerPaddingCells") or 0)
    tree_block_cells = max(2, round(128 / cell_size))
    tree_shadow_radius = float(occlusion_source.get("treeShadowRadiusCells") or math.sqrt(2))
    tree_cluster_padding = int(occlusion_source.get("treeClusterPaddingCells") or 0)
    tree_cluster_min = int(occlusion_source.get("treeClusterMinCells") or 18)

    elevation = {}
    elevation_values = set()
    tree_centers = []
    fow = set()

    for iy in range(grid_h):
        gy = image_to_grid_y(grid_h, iy)
        for gx in range(grid_w):
            ez = pixel(raw, raw_w, gx, iy)[0]
            elevation[(gx, gy)] = ez
            elevation_values.add(ez)

            tr = pixel(raw, raw_w, grid_w + gx, iy)
            if tr[1] == 0 and tr[2] == 0:
                tree_centers.append((gx - 0.5, gy - 0.5, tr[0] + 40))

            fp = pixel(raw, raw_w, grid_w * 3 + gx, iy)
            if fp[0] == 0:
                fow.add((gx, gy))

    origin_elev = elevation.get((ward_grid["x"], ward_grid["y"]), ward_z)
    elevation_walls = set()
    for (gx, gy), z in elevation.items():
        if z <= origin_elev:
            continue
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                if elevation.get((gx + dx, gy + dy), 255) <= origin_elev:
                    elevation_walls.add((gx, gy))
                    dx = dy = 99
                    break
            if dx == 99:
                break

    tree_blocks = set()
    tree_centers_active = []
    for cx, cy, tree_elev in tree_centers:
        if origin_elev >= tree_elev:
            continue
        tree_centers_active.append((cx, cy, tree_elev))
        start_x = math.floor(cx - (tree_block_cells - 2) / 2)
        start_y = math.floor(cy - (tree_block_cells - 2) / 2)
        for dx in range(tree_block_cells):
            for dy in range(tree_block_cells):
                tree_blocks.add((start_x + dx, start_y + dy))

    def pad(points, radius):
        if radius <= 0:
            return set(points)
        out = set()
        for gx, gy in points:
            add_disk(out, gx, gy, radius)
        return out

    fow_lines = occlusion_source.get("fowBlockerLines")
    if fow_lines and fow_lines.get("path"):
        fow_payload = json.loads(Path(fow_lines["path"]).read_text(encoding="utf-8"))
        rasterize_fow_lines(
            fow,
            fow_payload,
            mod,
            cell_size,
            float(fow_lines.get("maxSegmentLength") or 512),
            float(fow_lines.get("lineRadiusWorld") or 256),
        )

    tree_blocks = cluster_pad(tree_blocks, tree_cluster_min, tree_cluster_padding)

    return {
        "gridWidth": grid_w,
        "gridHeight": grid_h,
        "cellSize": cell_size,
        "originElevation": origin_elev,
        "elevation": pad(elevation_walls, blocker_pad),
        "tree": pad(tree_blocks, blocker_pad),
        "fow": pad(fow, blocker_pad),
        "treeCenters": tree_centers_active,
        "treeShadowRadiusCells": tree_shadow_radius,
        "treeClusterPaddingCells": tree_cluster_padding,
        "treeClusterMinCells": tree_cluster_min,
    }


def draw_grid_cell(draw, mod, gx, gy, width, height, cell_size, fill):
    wx = gx * cell_size + mod.VISION_WORLD_MIN_X
    wy = gy * cell_size + mod.VISION_WORLD_MIN_Y
    px, py = mod.world_to_px(wx, wy, width, height)
    wx2 = wx + cell_size
    wy2 = wy + cell_size
    px2, _ = mod.world_to_px(wx2, wy, width, height)
    _, py2 = mod.world_to_px(wx, wy2, width, height)
    r = max(1, (abs(px2 - px) + abs(py2 - py)) * 0.24)
    draw.rectangle((px - r, py - r, px + r, py + r), fill=fill)


def first_blocker_on_ray(origin, angle, radius_cells, blockers):
    ox, oy = origin
    last = (ox, oy)
    steps = int(math.ceil(radius_cells * 2))
    for i in range(1, steps + 1):
        dist = i / 2
        gx = round(ox + math.cos(angle) * dist)
        gy = round(oy + math.sin(angle) * dist)
        last = (gx, gy)
        if (gx, gy) in blockers["tree"]:
            return "tree", (gx, gy)
        if (gx, gy) in blockers["elevation"]:
            return "elevation", (gx, gy)
        if (gx, gy) in blockers["fow"]:
            return "fow", (gx, gy)
    return "open", last


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--ward-json", required=True)
    parser.add_argument("--occlusion-cells", required=True)
    parser.add_argument("--map-data", required=True)
    parser.add_argument("--map-image", required=True)
    parser.add_argument("--projection-calibration", required=True)
    parser.add_argument("--ehandle", type=int, required=True)
    parser.add_argument("--time", type=int, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--ray-count", type=int, default=144)
    args = parser.parse_args()

    project_root = Path(args.project_root)
    mod = load_render_module(project_root)
    mod.PROJECTION_CALIBRATION = json.loads(Path(args.projection_calibration).read_text(encoding="utf-8"))

    payload = json.loads(Path(args.ward_json).read_text(encoding="utf-8"))
    occlusion = json.loads(Path(args.occlusion_cells).read_text(encoding="utf-8"))
    source = occlusion.get("source") or {}
    mod.VISION_CELL_SIZE = float(source.get("cellSize") or mod.VISION_CELL_SIZE)
    grid_src = source.get("grid") or {}
    mod.VISION_WORLD_MIN_X = float(grid_src.get("worldMinX") or mod.VISION_WORLD_MIN_X)
    mod.VISION_WORLD_MIN_Y = float(grid_src.get("worldMinY") or mod.VISION_WORLD_MIN_Y)

    ward = next(w for w in payload["wards"] if int(w["ehandle"]) == args.ehandle)
    result = next(r for r in occlusion["results"] if int(r["ehandle"]) == args.ehandle)
    ward_world = mod.parser_to_world(ward["x"], ward["y"])
    origin = result.get("originGrid") or result.get("grid")
    visible = {tuple(c) for c in result.get("cells", [])}

    blockers = build_blockers(Path(args.map_data), source, origin, ward.get("z"), mod)
    radius_cells = 1600 / blockers["cellSize"]
    ox, oy = origin["x"], origin["y"]

    base = Image.open(args.map_image).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    width, height = base.size

    # Draw visible area first.
    for gx, gy in visible:
        draw_grid_cell(draw, mod, gx, gy, width, height, blockers["cellSize"], (65, 220, 120, 88))

    # Draw blocker cells near this ward.
    near_sets = [
        ("fow", blockers["fow"], (255, 64, 64, 130)),
        ("elevation", blockers["elevation"], (255, 178, 48, 130)),
        ("tree", blockers["tree"], (156, 82, 255, 118)),
    ]
    for _, pts, color in near_sets:
        for gx, gy in pts:
            if (gx - ox) * (gx - ox) + (gy - oy) * (gy - oy) <= (radius_cells + 4) ** 2:
                draw_grid_cell(draw, mod, gx, gy, width, height, blockers["cellSize"], color)

    # Ray diagnostics.
    ray_colors = {
        "open": (120, 210, 255, 145),
        "tree": (180, 80, 255, 210),
        "elevation": (255, 176, 40, 220),
        "fow": (255, 60, 60, 220),
    }
    ray_counts = {"open": 0, "tree": 0, "elevation": 0, "fow": 0}
    cpx, cpy = mod.world_to_px(ward_world[0], ward_world[1], width, height)
    for i in range(args.ray_count):
        angle = math.tau * i / args.ray_count
        reason, end = first_blocker_on_ray((ox, oy), angle, radius_cells, blockers)
        ray_counts[reason] += 1
        wx = end[0] * blockers["cellSize"] + mod.VISION_WORLD_MIN_X
        wy = end[1] * blockers["cellSize"] + mod.VISION_WORLD_MIN_Y
        epx, epy = mod.world_to_px(wx, wy, width, height)
        draw.line((cpx, cpy, epx, epy), fill=ray_colors[reason], width=1)

    draw.ellipse((cpx - 6, cpy - 6, cpx + 6, cpy + 6), fill=(255, 255, 255, 255), outline=(0, 0, 0, 255), width=2)

    result_img = Image.alpha_composite(base, overlay).convert("RGB")
    draw2 = ImageDraw.Draw(result_img)
    font = ImageFont.load_default()
    lines = [
        f"match {payload.get('match_id')} t={args.time}s ehandle={args.ehandle}",
        f"obs {ward.get('team')} world=({ward_world[0]:.0f},{ward_world[1]:.0f}) grid=({ox},{oy}) elev={blockers['originElevation']}",
        f"visible cells={len(visible)} treeR={blockers['treeShadowRadiusCells']} cell={blockers['cellSize']}",
        f"rays open={ray_counts['open']} tree={ray_counts['tree']} elevation={ray_counts['elevation']} fow={ray_counts['fow']}",
        "green=visible purple=tree orange=elevation red=fow blue=open-ray",
    ]
    y = 10
    for line in lines:
        draw2.text((10, y), line, fill=(255, 255, 255), font=font, stroke_width=2, stroke_fill=(0, 0, 0))
        y += 13

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    result_img.save(out)
    sidecar = out.with_suffix(".json")
    sidecar.write_text(json.dumps({
        "out": str(out),
        "ehandle": args.ehandle,
        "time": args.time,
        "ward": ward,
        "wardWorld": {"x": ward_world[0], "y": ward_world[1]},
        "originGrid": origin,
        "visibleCells": len(visible),
        "rayCounts": ray_counts,
        "blockerCountsInRadius": {
            name: sum(1 for gx, gy in pts if (gx - ox) * (gx - ox) + (gy - oy) * (gy - oy) <= (radius_cells + 4) ** 2)
            for name, pts, _ in near_sets
        },
        "occlusionSource": source,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(out), "json": str(sidecar), "rayCounts": ray_counts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
