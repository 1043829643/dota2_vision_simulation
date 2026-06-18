import json
from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


H741 = [
    [0.0076578273910818, 0.0002417645481266, -0.4912248797627541],
    [-0.0000195675289368, 0.0078239058911833, -0.4804050188013505],
    [-0.0000504343416886, 0.0004340781970321, 1.0],
]

WORLD_UNITS_PER_PARSER_UNIT = 128.0
WORLD_PARSER_OFFSET = 16384.0
VISION_WORLD_MIN_X = -10464.0
VISION_WORLD_MIN_Y = -10464.0
MAP_WORLD_MIN_X = -10829.42
MAP_WORLD_MAX_X = 11487.75
MAP_WORLD_MAX_Y = 11351.48
MAP_WORLD_MIN_Y = -10939.96

PALETTE = {
    0: (35, 72, 170, 105),
    20: (48, 130, 210, 105),
    40: (64, 190, 110, 105),
    60: (150, 210, 80, 105),
    80: (225, 220, 90, 115),
    100: (245, 170, 65, 120),
    120: (235, 115, 70, 125),
    140: (220, 70, 85, 130),
    160: (185, 65, 150, 135),
    180: (145, 70, 190, 140),
    200: (105, 80, 220, 145),
    220: (80, 100, 240, 150),
    240: (215, 215, 235, 155),
    255: (255, 255, 255, 160),
}

DEFAULT_CALIBRATION = {
    "x": 13.0,
    "y": -16.0,
    "scale": 1.0655,
    "opacity": 1.0,
    "rotate": 0,
    "flipX": False,
    "flipY": False,
}


def hmap(x, y):
    w = H741[2][0] * x + H741[2][1] * y + H741[2][2]
    return (
        (H741[0][0] * x + H741[0][1] * y + H741[0][2]) / w,
        (H741[1][0] * x + H741[1][1] * y + H741[1][2]) / w,
    )


def to_px(parser_x, parser_y, width, height):
    nx, ny = hmap(parser_x, parser_y)
    return nx * width, (1.0 - ny) * height


def grid_to_px(gx, gy, width, height):
    wx = gx * 64.0 + VISION_WORLD_MIN_X
    wy = gy * 64.0 + VISION_WORLD_MIN_Y
    return (
        (wx - MAP_WORLD_MIN_X) / (MAP_WORLD_MAX_X - MAP_WORLD_MIN_X) * width,
        (MAP_WORLD_MAX_Y - wy) / (MAP_WORLD_MAX_Y - MAP_WORLD_MIN_Y) * height,
    )


def render():
    root = Path.cwd()
    base_path = root / "outputs/dota_current_map_assets/materials/overviews/dota_psd_8b4a9409.png"
    map_data_path = root / "map-data-741/img/map_data_741.png"
    out_dir = root / "outputs/741_terrain_elevation_overlay"
    out_dir.mkdir(parents=True, exist_ok=True)

    base = Image.open(base_path).convert("RGBA")
    map_data = Image.open(map_data_path).convert("RGBA")
    width, height = base.size
    grid_w = map_data.size[1]
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    counts = Counter()
    for image_y in range(grid_w):
        gy = grid_w - image_y - 1
        for gx in range(grid_w):
            elevation = map_data.getpixel((gx, image_y))[0]
            counts[elevation] += 1
            color = PALETTE.get(elevation)
            if not color:
                continue
            px, py = grid_to_px(gx, gy, width, height)
            overlay_draw.rectangle((px - 2, py - 2, px + 2, py + 2), fill=color)

    calibrated_overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    scaled_size = (
        round(width * DEFAULT_CALIBRATION["scale"]),
        round(height * DEFAULT_CALIBRATION["scale"]),
    )
    scaled_overlay = overlay.resize(scaled_size, Image.Resampling.NEAREST)
    paste_x = round((width - scaled_size[0]) / 2 + DEFAULT_CALIBRATION["x"])
    paste_y = round((height - scaled_size[1]) / 2 + DEFAULT_CALIBRATION["y"])
    calibrated_overlay.alpha_composite(scaled_overlay, (paste_x, paste_y))

    result = Image.alpha_composite(base, calibrated_overlay)
    draw = ImageDraw.Draw(result)
    font = ImageFont.load_default()
    title = "Dota 7.41 elevation overlay from map_data_741.png"
    draw.rectangle((10, 10, 430, 32), fill=(0, 0, 0, 185))
    draw.text((16, 16), title, fill=(255, 255, 255, 255), font=font)

    legend_x, legend_y = 16, 48
    row_h = 19
    legend_w = 185
    legend_h = row_h * len(sorted(counts)) + 12
    draw.rectangle((legend_x - 6, legend_y - 6, legend_x + legend_w, legend_y + legend_h), fill=(0, 0, 0, 165))
    for idx, elevation in enumerate(sorted(counts)):
        y = legend_y + idx * row_h
        color = PALETTE[elevation]
        draw.rectangle((legend_x, y, legend_x + 14, y + 12), fill=color)
        draw.text((legend_x + 20, y), f"elevation {elevation:3d}  cells {counts[elevation]}", fill=(255, 255, 255, 255), font=font)

    out_png = out_dir / "terrain_elevation_overlay_741.png"
    overlay_png = out_dir / "terrain_elevation_layer_741.png"
    base_copy = out_dir / base_path.name
    base.save(base_copy)
    overlay.save(overlay_png)
    result.convert("RGB").save(out_png, quality=94)

    payload = {
        "sourceMap": str(base_path),
        "sourceMapData": str(map_data_path),
        "gridWidth": grid_w,
        "homography": H741,
        "projection": {
            "type": "direct_world_bounds",
            "worldMinX": MAP_WORLD_MIN_X,
            "worldMaxX": MAP_WORLD_MAX_X,
            "worldMinY": MAP_WORLD_MIN_Y,
            "worldMaxY": MAP_WORLD_MAX_Y,
            "note": "Terrain overlay uses map_data grid -> world coordinates -> overview image bounds. It does not use the DB homography.",
        },
        "elevationCounts": dict(sorted(counts.items())),
        "palette": {str(k): list(v) for k, v in PALETTE.items()},
        "defaultCalibration": DEFAULT_CALIBRATION,
        "output": str(out_png),
        "overlay": str(overlay_png),
        "base": str(base_copy),
    }
    (out_dir / "terrain_elevation_overlay_741.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    legend_rows = "\n".join(
        f'<div class="legend-row"><span style="background:rgba({PALETTE[e][0]},{PALETTE[e][1]},{PALETTE[e][2]},{PALETTE[e][3] / 255:.3f})"></span>elevation {e} · {counts[e]} cells</div>'
        for e in sorted(counts)
    )
    html = f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>Dota 7.41 Terrain Elevation Overlay</title>
<style>
body{{margin:0;background:#111;color:#eee;font-family:Arial,"Microsoft YaHei",sans-serif}}
main{{max-width:1180px;margin:0 auto;padding:18px}}
.toolbar{{display:grid;grid-template-columns:repeat(8,minmax(100px,1fr));gap:12px;align-items:end;margin:12px 0 14px}}
label{{display:grid;gap:4px;font-size:12px;color:#cbd5df}}
label.check{{display:flex;gap:7px;align-items:center;height:32px}}
input[type=range]{{width:100%}}
input[type=number]{{width:100%;box-sizing:border-box;background:#0f1720;color:#eef3f7;border:1px solid #3d4652;border-radius:5px;padding:5px 6px}}
button{{height:32px;border:1px solid #3d4652;border-radius:6px;background:#202833;color:#eef3f7;cursor:pointer}}
.stage{{position:relative;width:100%;aspect-ratio:1/1;border:1px solid #333;background:#000;overflow:hidden}}
.stage img{{position:absolute;inset:0;width:100%;height:100%;user-select:none;pointer-events:none}}
#terrain{{transform-origin:50% 50%;mix-blend-mode:normal}}
.legend{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:4px 12px;margin-top:12px;font-size:12px;color:#d9e2ec}}
.legend-row span{{display:inline-block;width:13px;height:13px;margin-right:6px;vertical-align:-2px;border:1px solid rgba(255,255,255,.25)}}
.readout{{font-family:Consolas,monospace;color:#a7f3d0;font-size:12px;margin-top:8px}}
</style>
<main>
<h2>Dota 7.41 Terrain Elevation Overlay</h2>
<div class="toolbar">
  <label>X offset <input id="x" type="range" min="-300" max="300" step="0.1" value="{DEFAULT_CALIBRATION['x']}"><input id="xNum" type="number" step="0.1" value="{DEFAULT_CALIBRATION['x']}"></label>
  <label>Y offset <input id="y" type="range" min="-300" max="300" step="0.1" value="{DEFAULT_CALIBRATION['y']}"><input id="yNum" type="number" step="0.1" value="{DEFAULT_CALIBRATION['y']}"></label>
  <label>Scale <input id="scale" type="range" min="0.70" max="1.30" step="0.0005" value="{DEFAULT_CALIBRATION['scale']}"><input id="scaleNum" type="number" step="0.0005" value="{DEFAULT_CALIBRATION['scale']}"></label>
  <label>Opacity <input id="opacity" type="range" min="0" max="1" step="0.01" value="{DEFAULT_CALIBRATION['opacity']}"><input id="opacityNum" type="number" step="0.01" value="{DEFAULT_CALIBRATION['opacity']}"></label>
  <label class="check"><input id="rotate180" type="checkbox">Rotate 180</label>
  <label class="check"><input id="flipX" type="checkbox">Flip X</label>
  <label class="check"><input id="flipY" type="checkbox">Flip Y</label>
  <button id="reset">Reset</button>
</div>
<div class="stage">
  <img id="base" src="{base_copy.name}" alt="Dota 7.41 map">
  <img id="terrain" src="{overlay_png.name}" alt="Elevation overlay">
</div>
<div class="readout" id="readout"></div>
<div class="legend">{legend_rows}</div>
</main>
<script>
const controls = {{
  x: document.getElementById("x"),
  y: document.getElementById("y"),
  scale: document.getElementById("scale"),
  opacity: document.getElementById("opacity"),
}};
const numberControls = {{
  x: document.getElementById("xNum"),
  y: document.getElementById("yNum"),
  scale: document.getElementById("scaleNum"),
  opacity: document.getElementById("opacityNum"),
}};
const terrain = document.getElementById("terrain");
const readout = document.getElementById("readout");
const rotate180 = document.getElementById("rotate180");
const flipX = document.getElementById("flipX");
const flipY = document.getElementById("flipY");
function apply() {{
  const x = Number(controls.x.value);
  const y = Number(controls.y.value);
  const scale = Number(controls.scale.value);
  const opacity = Number(controls.opacity.value);
  const rotate = rotate180.checked ? 180 : 0;
  const sx = scale * (flipX.checked ? -1 : 1);
  const sy = scale * (flipY.checked ? -1 : 1);
  terrain.style.transform = `translate(${{x}}px, ${{y}}px) rotate(${{rotate}}deg) scale(${{sx}}, ${{sy}})`;
  terrain.style.opacity = opacity;
  numberControls.x.value = x.toFixed(1);
  numberControls.y.value = y.toFixed(1);
  numberControls.scale.value = scale.toFixed(4);
  numberControls.opacity.value = opacity.toFixed(2);
  readout.textContent = `x=${{x}}px y=${{y}}px scale=${{scale.toFixed(4)}} opacity=${{opacity.toFixed(2)}} rotate=${{rotate}} flipX=${{flipX.checked}} flipY=${{flipY.checked}}`;
}}
Object.values(controls).forEach(input => input.addEventListener("input", apply));
rotate180.addEventListener("change", apply);
flipX.addEventListener("change", apply);
flipY.addEventListener("change", apply);
for (const key of Object.keys(numberControls)) {{
  numberControls[key].addEventListener("input", () => {{
    controls[key].value = numberControls[key].value;
    apply();
  }});
}}
document.getElementById("reset").addEventListener("click", () => {{
  controls.x.value = {DEFAULT_CALIBRATION['x']};
  controls.y.value = {DEFAULT_CALIBRATION['y']};
  controls.scale.value = {DEFAULT_CALIBRATION['scale']};
  controls.opacity.value = {DEFAULT_CALIBRATION['opacity']};
  rotate180.checked = false;
  flipX.checked = false;
  flipY.checked = false;
  apply();
}});
window.addEventListener("keydown", (event) => {{
  const step = event.shiftKey ? 0.1 : 1;
  if (event.key === "ArrowLeft") controls.x.value = (Number(controls.x.value) - step).toFixed(1);
  else if (event.key === "ArrowRight") controls.x.value = (Number(controls.x.value) + step).toFixed(1);
  else if (event.key === "ArrowUp") controls.y.value = (Number(controls.y.value) - step).toFixed(1);
  else if (event.key === "ArrowDown") controls.y.value = (Number(controls.y.value) + step).toFixed(1);
  else return;
  event.preventDefault();
  apply();
}});
apply();
</script>
</html>
"""
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    render()
