import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WORLD_UNITS_PER_PARSER_UNIT = 128.0
WORLD_PARSER_OFFSET = 16384.0


def parser_to_world(x, y):
    return (
        float(x) * WORLD_UNITS_PER_PARSER_UNIT - WORLD_PARSER_OFFSET,
        float(y) * WORLD_UNITS_PER_PARSER_UNIT - WORLD_PARSER_OFFSET,
    )


def active_at(wards, t):
    return [w for w in wards if w["start"] <= t and (w.get("end") is None or t < w["end"])]


def world_to_grid(wx, wy, world, cell_size):
    return (
        int(round((wx - world["worldMinX"]) / cell_size)),
        int(round((wy - world["worldMinY"]) / cell_size)),
    )


def grid_to_layer_px(gx, gy, grid_h):
    return gx, grid_h - gy - 1


def draw_marker(draw, x, y, ward, label, scale=1):
    is_obs = ward.get("type") == "obs"
    is_radiant = ward.get("team") == "radiant"
    fill = (70, 220, 120) if is_radiant else (240, 80, 80)
    outline = (255, 255, 255) if is_obs else (80, 190, 255)
    r = 5 * scale if is_obs else 4 * scale
    draw.ellipse((x - r, y - r, x + r, y + r), fill=fill, outline=outline, width=max(1, int(2 * scale)))
    if not is_obs:
        draw.ellipse((x - r * 2.2, y - r * 2.2, x + r * 2.2, y + r * 2.2), outline=(80, 190, 255), width=max(1, int(scale)))
    draw.text((x + 7 * scale, y - 6 * scale), label, fill=(255, 255, 255), stroke_width=max(1, int(2 * scale)), stroke_fill=(0, 0, 0))


def load_tree_points(mapdata_path):
    if not mapdata_path:
        return []
    data = json.loads(Path(mapdata_path).read_text(encoding="utf-8"))
    if isinstance(data.get("trees"), list):
        return data["trees"]
    return data.get("data", {}).get("ent_dota_tree", [])


def load_vision_by_handle(occlusion_path):
    if not occlusion_path:
        return {}, None
    data = json.loads(Path(occlusion_path).read_text(encoding="utf-8"))
    by_handle = {
        int(item["ehandle"]): item.get("cells", [])
        for item in data.get("results", [])
    }
    return by_handle, data.get("source", {})


def draw_grid_square(draw, gx, gy, grid_h, scale, fill):
    px, py = grid_to_layer_px(gx, gy, grid_h)
    x = px * scale
    y = py * scale
    r = max(1, int(scale * 0.46))
    draw.rectangle((x - r, y - r, x + r, y + r), fill=fill)


def make_tree_vision_overlay(map_data, wards, world, cell_size, out_path, t, mapdata_path=None, occlusion_path=None):
    img = Image.open(map_data).convert("RGB")
    grid_w = img.width // 5
    grid_h = img.height
    scale = 3
    layer = img.crop((0, 0, grid_w, grid_h)).resize((grid_w * scale, grid_h * scale), Image.Resampling.NEAREST).convert("RGBA")
    overlay = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    trees = load_tree_points(mapdata_path)
    for tree in trees:
        gx, gy = world_to_grid(tree["x"], tree["y"], world, cell_size)
        draw_grid_square(draw, gx, gy, grid_h, scale, (52, 168, 70, 155))

    vision_by_handle, vision_source = load_vision_by_handle(occlusion_path)
    for ward in wards:
        if ward.get("type") != "obs":
            continue
        cells = vision_by_handle.get(int(ward["ehandle"]), [])
        if not cells:
            continue
        fill = (70, 220, 120, 112) if ward.get("team") == "radiant" else (240, 80, 80, 112)
        for gx, gy in cells:
            draw_grid_square(draw, gx, gy, grid_h, scale, fill)

    result = Image.alpha_composite(layer, overlay).convert("RGB")
    draw2 = ImageDraw.Draw(result)
    for ward in wards:
        wx, wy = parser_to_world(ward["x"], ward["y"])
        gx, gy = world_to_grid(wx, wy, world, cell_size)
        px, py = grid_to_layer_px(gx, gy, grid_h)
        draw_marker(draw2, px * scale, py * scale, ward, f"{ward['type']} {ward['team'][0]} {ward['ehandle']}", scale=1)

    draw2.text((8, 8), f"7.41 devilesk trees + vision t={t}s cell={cell_size}", fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
    draw2.text((8, 22), "green dots=trees, green/red cells=observer vision", fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
    result.save(out_path)
    return {
        "treeCount": len(trees),
        "occlusionSource": vision_source,
        "visionCellsByHandle": {str(k): len(v) for k, v in vision_by_handle.items()},
    }


def make_focus_crop(source_path, report_wards, ehandle, out_path, crop_radius_px=150, upscale=3):
    if not ehandle:
        return None
    target = next((w for w in report_wards if int(w["ehandle"]) == int(ehandle)), None)
    if not target:
        return None
    img = Image.open(source_path).convert("RGB")
    scale = img.width / 327
    cx = int(target["image"]["x"] * scale)
    cy = int(target["image"]["y"] * scale)
    left = max(0, cx - crop_radius_px)
    top = max(0, cy - crop_radius_px)
    right = min(img.width, cx + crop_radius_px)
    bottom = min(img.height, cy + crop_radius_px)
    crop = img.crop((left, top, right, bottom)).resize(((right - left) * upscale, (bottom - top) * upscale), Image.Resampling.NEAREST)
    draw = ImageDraw.Draw(crop)
    draw.text((8, 8), f"focus ehandle={ehandle}", fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
    crop.save(out_path)
    return str(out_path)


def make_layer_contact_sheet(map_data, wards, world, cell_size, out_path, t):
    img = Image.open(map_data).convert("RGB")
    layer_count = 5
    grid_w = img.width // layer_count
    grid_h = img.height
    labels = ["elevation", "tree_elevation", "gridnav", "ent_fow_blocker_node", "no_wards"]
    scale = 3
    gap = 24
    header_h = 36
    sheet = Image.new("RGB", (grid_w * scale * layer_count + gap * (layer_count - 1), grid_h * scale + header_h), (18, 18, 18))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    rows = []

    for i, name in enumerate(labels):
        crop = img.crop((i * grid_w, 0, (i + 1) * grid_w, grid_h)).resize((grid_w * scale, grid_h * scale), Image.Resampling.NEAREST)
        ox = i * (grid_w * scale + gap)
        sheet.paste(crop, (ox, header_h))
        draw.text((ox + 4, 8), name, fill=(255, 255, 255), font=font)

        for ward in wards:
            wx, wy = parser_to_world(ward["x"], ward["y"])
            gx, gy = world_to_grid(wx, wy, world, cell_size)
            px, py = grid_to_layer_px(gx, gy, grid_h)
            sx = ox + px * scale
            sy = header_h + py * scale
            label = f"{ward['type']} {ward['team'][0]} {ward['ehandle']}"
            draw_marker(draw, sx, sy, ward, label, scale=1)
            if i == 0:
                rows.append({
                    "ehandle": ward["ehandle"],
                    "type": ward["type"],
                    "team": ward["team"],
                    "parser": {"x": ward["x"], "y": ward["y"]},
                    "world": {"x": wx, "y": wy},
                    "grid": {"x": gx, "y": gy},
                    "image": {"x": px, "y": py},
                })

    sheet.save(out_path)
    return {"gridWidth": grid_w, "gridHeight": grid_h, "wards": rows}


def make_single_layer_overlay(map_data, wards, world, cell_size, out_path, t, layer_index=0):
    img = Image.open(map_data).convert("RGB")
    grid_w = img.width // 5
    grid_h = img.height
    layer = img.crop((layer_index * grid_w, 0, (layer_index + 1) * grid_w, grid_h)).resize((grid_w * 3, grid_h * 3), Image.Resampling.NEAREST)
    draw = ImageDraw.Draw(layer)
    for ward in wards:
        wx, wy = parser_to_world(ward["x"], ward["y"])
        gx, gy = world_to_grid(wx, wy, world, cell_size)
        px, py = grid_to_layer_px(gx, gy, grid_h)
        draw_marker(draw, px * 3, py * 3, ward, f"{ward['type']} {ward['team'][0]} {ward['ehandle']}", scale=1)
    draw.text((8, 8), f"7.41 devilesk grid layer={layer_index} t={t}s cell={cell_size}", fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
    layer.save(out_path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--map-data", required=True)
    parser.add_argument("--ward-json", required=True)
    parser.add_argument(
        "--worlddata",
        default=str(Path(__file__).resolve().parents[1] / "resources" / "map-data" / "worlddata.json"),
    )
    parser.add_argument("--time", type=int, default=1362)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--cell-size", type=float, default=64)
    parser.add_argument("--mapdata-json")
    parser.add_argument("--occlusion-cells")
    parser.add_argument("--focus-ehandle", type=int)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.loads(Path(args.ward_json).read_text(encoding="utf-8"))
    world = json.loads(Path(args.worlddata).read_text(encoding="utf-8"))
    wards = active_at(payload["wards"], args.time)

    contact = out_dir / f"devilesk_741_map_data_layers_t{args.time}_wards.png"
    meta = make_layer_contact_sheet(Path(args.map_data), wards, world, args.cell_size, contact, args.time)

    elevation_overlay = out_dir / f"devilesk_741_elevation_layer_t{args.time}_wards.png"
    make_single_layer_overlay(Path(args.map_data), wards, world, args.cell_size, elevation_overlay, args.time, layer_index=0)

    tree_overlay = out_dir / f"devilesk_741_tree_layer_t{args.time}_wards.png"
    make_single_layer_overlay(Path(args.map_data), wards, world, args.cell_size, tree_overlay, args.time, layer_index=1)

    tree_vision_overlay = out_dir / f"devilesk_741_trees_vision_t{args.time}.png"
    tree_vision_meta = make_tree_vision_overlay(
        Path(args.map_data),
        wards,
        world,
        args.cell_size,
        tree_vision_overlay,
        args.time,
        mapdata_path=args.mapdata_json,
        occlusion_path=args.occlusion_cells,
    )

    focus_overlay = out_dir / f"devilesk_741_trees_vision_t{args.time}_focus_e{args.focus_ehandle}.png" if args.focus_ehandle else None
    focus_path = make_focus_crop(tree_vision_overlay, meta["wards"], args.focus_ehandle, focus_overlay) if focus_overlay else None

    report = {
        "time": args.time,
        "mapData": str(Path(args.map_data).resolve()),
        "worlddata": world,
        "cellSize": args.cell_size,
        "contactSheet": str(contact),
        "elevationOverlay": str(elevation_overlay),
        "treeOverlay": str(tree_overlay),
        "treeVisionOverlay": str(tree_vision_overlay),
        "focusOverlay": focus_path,
        "treeVisionMeta": tree_vision_meta,
        **meta,
    }
    report_path = out_dir / f"devilesk_741_ward_mapping_t{args.time}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "contactSheet": str(contact),
        "elevationOverlay": str(elevation_overlay),
        "treeOverlay": str(tree_overlay),
        "treeVisionOverlay": str(tree_vision_overlay),
        "focusOverlay": focus_path,
        "json": str(report_path),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
