import argparse
import json
import math
import os
import shutil
from pathlib import Path

import pymysql
from PIL import Image, ImageDraw, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = PROJECT_ROOT / "resources"

H741 = [
    [0.0076578273910818, 0.0002417645481266, -0.4912248797627541],
    [-0.0000195675289368, 0.0078239058911833, -0.4804050188013505],
    [-0.0000504343416886, 0.0004340781970321, 1.0],
]

WORLD_UNITS_PER_PARSER_UNIT = 128.0
WORLD_PARSER_OFFSET = 16384.0
VISION_WORLD_MIN_X = -10464.0
VISION_WORLD_MIN_Y = -10464.0
VISION_CELL_SIZE = 64.0
VISION_CELL_CENTER_OFFSET = 0.0
MAP_WORLD_MIN_X = -10829.42
MAP_WORLD_MAX_X = 11487.75
MAP_WORLD_MAX_Y = 11351.48
MAP_WORLD_MIN_Y = -10939.96
DEFAULT_CALIBRATION = {
    "x": 13.0,
    "y": -16.0,
    "scale": 1.0655,
}
PROJECTION_CALIBRATION = None
TREE_POINTS = []
HERO_VISION = None
occlusion_payload = None

RADIANT = {"name": "Radiant", "color": [66, 210, 118], "fill": [66, 210, 118, 120]}
DIRE = {"name": "Dire", "color": [238, 82, 82], "fill": [238, 82, 82, 120]}
RADIANT_HERO_FILL = (72, 160, 255, 58)
DIRE_HERO_FILL = (255, 172, 64, 58)


def connect():
    return pymysql.connect(
        host=os.environ.get("DOTA_DB_HOST", os.environ.get("DB_HOST", "127.0.0.1")),
        port=int(os.environ.get("DOTA_DB_PORT", os.environ.get("DB_PORT", "9030"))),
        user=os.environ.get("DOTA_DB_USER", os.environ.get("DB_USER", "")),
        password=os.environ.get("DOTA_DB_PASSWORD", os.environ.get("DB_PASS", "")),
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=90,
    )


def hmap(x, y):
    w = H741[2][0] * x + H741[2][1] * y + H741[2][2]
    return (
        (H741[0][0] * x + H741[0][1] * y + H741[0][2]) / w,
        (H741[1][0] * x + H741[1][1] * y + H741[1][2]) / w,
    )


def project_path(path):
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def to_px(x, y, width, height):
    nx, ny = hmap(float(x), float(y))
    return nx * width, (1.0 - ny) * height


def apply_calibration(px, py, width, height):
    scale = DEFAULT_CALIBRATION["scale"]
    return (
        (px - width / 2) * scale + width / 2 + DEFAULT_CALIBRATION["x"],
        (py - height / 2) * scale + height / 2 + DEFAULT_CALIBRATION["y"],
    )


def world_to_px(wx, wy, width, height, calibrated=True):
    if PROJECTION_CALIBRATION:
        affine = PROJECTION_CALIBRATION["affine"]
        sx = width / float(PROJECTION_CALIBRATION.get("imageWidth", width))
        sy = height / float(PROJECTION_CALIBRATION.get("imageHeight", height))
        return (
            (affine["a"] * wx + affine["b"] * wy + affine["c"]) * sx,
            (affine["d"] * wx + affine["e"] * wy + affine["f"]) * sy,
        )
    px = (wx - MAP_WORLD_MIN_X) / (MAP_WORLD_MAX_X - MAP_WORLD_MIN_X) * width
    py = (MAP_WORLD_MAX_Y - wy) / (MAP_WORLD_MAX_Y - MAP_WORLD_MIN_Y) * height
    return apply_calibration(px, py, width, height) if calibrated else (px, py)


def parser_to_world(x, y):
    return (
        float(x) * WORLD_UNITS_PER_PARSER_UNIT - WORLD_PARSER_OFFSET,
        float(y) * WORLD_UNITS_PER_PARSER_UNIT - WORLD_PARSER_OFFSET,
    )


def ward_to_px(x, y, width, height):
    wx, wy = parser_to_world(x, y)
    return world_to_px(wx, wy, width, height)


def radius_px(x, y, width, height, dota_units):
    wx, wy = parser_to_world(x, y)
    cx, cy = world_to_px(wx, wy, width, height)
    x2, y2 = world_to_px(wx + dota_units, wy, width, height)
    x3, y3 = world_to_px(wx, wy + dota_units, width, height)
    return (math.dist((cx, cy), (x2, y2)) + math.dist((cx, cy), (x3, y3))) / 2.0


def grid_to_px(gx, gy, width, height):
    wx = (gx + VISION_CELL_CENTER_OFFSET) * VISION_CELL_SIZE + VISION_WORLD_MIN_X
    wy = (gy + VISION_CELL_CENTER_OFFSET) * VISION_CELL_SIZE + VISION_WORLD_MIN_Y
    return world_to_px(wx, wy, width, height)


def grid_cell_radius_px(gx, gy, width, height):
    cx, cy = grid_to_px(gx, gy, width, height)
    wx = (gx + VISION_CELL_CENTER_OFFSET) * VISION_CELL_SIZE + VISION_WORLD_MIN_X
    wy = (gy + VISION_CELL_CENTER_OFFSET) * VISION_CELL_SIZE + VISION_WORLD_MIN_Y
    x2, y2 = world_to_px(wx + VISION_CELL_SIZE, wy, width, height)
    x3, y3 = world_to_px(wx, wy + VISION_CELL_SIZE, width, height)
    return max(1.0, (math.dist((cx, cy), (x2, y2)) + math.dist((cx, cy), (x3, y3))) / 2.0)


def query_wards(match_id):
    q = """
SELECT time, slot, type, attackername, x, y, z, entityleft, ehandle, log_index
FROM dota2_stats.ward_placed_left_fact
WHERE CAST(match_id AS BIGINT)=%s
ORDER BY time, log_index
"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(q, (match_id,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def build_intervals(rows):
    by_handle = {}
    for row in rows:
        by_handle.setdefault(int(row["ehandle"]), []).append(row)

    intervals = []
    unmatched = []
    for ehandle, events in by_handle.items():
        placements = [r for r in events if str(r["entityleft"]).lower() == "false"]
        lefts = [r for r in events if str(r["entityleft"]).lower() == "true"]
        if not placements:
            unmatched.append({"ehandle": ehandle, "events": events})
            continue
        place = placements[0]
        ward_type = str(place["type"]).replace("_left", "")
        left = next((r for r in lefts if str(r["type"]).startswith(ward_type)), None)
        slot = place["slot"]
        team = "radiant" if slot is not None and int(slot) < 5 else "dire"
        intervals.append(
            {
                "ehandle": ehandle,
                "type": ward_type,
                "team": team,
                "slot": None if slot is None else int(slot),
                "start": int(place["time"]),
                "end": int(left["time"]) if left else None,
                "x": float(place["x"]),
                "y": float(place["y"]),
                "z": None if place["z"] is None else float(place["z"]),
                "left_attacker": "" if not left else str(left["attackername"] or ""),
            }
        )
    intervals.sort(key=lambda r: (r["start"], r["team"], r["type"], r["ehandle"]))
    return intervals, unmatched


def active_at(intervals, t):
    return [w for w in intervals if w["start"] <= t and (w["end"] is None or t < w["end"])]


def vision_cells_at(ward, t):
    for segment in ward.get("visionTimeline", []):
        if segment["start"] <= t < segment["end"]:
            return segment.get("cells", [])
    return ward.get("visionCells", [])


def hero_vision_at(t):
    if not HERO_VISION:
        return None
    offset = int(t) - int(HERO_VISION.get("start", 0))
    seconds = HERO_VISION.get("seconds", [])
    if 0 <= offset < len(seconds) and int(seconds[offset].get("time")) == int(t):
        return seconds[offset]
    return next((item for item in seconds if int(item.get("time")) == int(t)), None)


def ward_count_label(wards):
    obs_r = sum(1 for w in wards if w["type"] == "obs" and w["team"] == "radiant")
    obs_d = sum(1 for w in wards if w["type"] == "obs" and w["team"] == "dire")
    sen_r = sum(1 for w in wards if w["type"] == "sen" and w["team"] == "radiant")
    sen_d = sum(1 for w in wards if w["type"] == "sen" and w["team"] == "dire")
    return f"obs R/D={obs_r}/{obs_d} sentry R/D={sen_r}/{sen_d}"


def draw_snapshot(base_path, out_path, intervals, t, title):
    base = Image.open(base_path).convert("RGBA")
    width, height = base.size
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    active_wards = active_at(intervals, t)
    hero_second = hero_vision_at(t)

    if hero_second:
        for fill, runs in (
            (RADIANT_HERO_FILL, hero_second.get("radiant", [])),
            (DIRE_HERO_FILL, hero_second.get("dire", [])),
        ):
            for gy, x0, x1 in runs:
                px0, py0 = grid_to_px(x0, gy, width, height)
                px1, py1 = grid_to_px(x1, gy, width, height)
                r = grid_cell_radius_px(x0, gy, width, height) * 0.62
                draw.rectangle(
                    (
                        min(px0, px1) - r,
                        min(py0, py1) - r,
                        max(px0, px1) + r,
                        max(py0, py1) + r,
                    ),
                    fill=fill,
                )

    for tree in TREE_POINTS:
        tx, ty = world_to_px(float(tree["x"]), float(tree["y"]), width, height)
        draw.rectangle((tx - 1.5, ty - 1.5, tx + 1.5, ty + 1.5), fill=(35, 235, 90, 220))

    for ward in active_wards:
        if ward["type"] != "obs":
            continue
        cx, cy = ward_to_px(ward["x"], ward["y"], width, height)
        style = RADIANT if ward["team"] == "radiant" else DIRE
        vision_cells = vision_cells_at(ward, t)
        if vision_cells:
            fill = tuple(style["fill"])
            for gx, gy in vision_cells:
                px, py = grid_to_px(gx, gy, width, height)
                r = grid_cell_radius_px(gx, gy, width, height) * 0.62
                draw.rectangle((px - r, py - r, px + r, py + r), fill=fill)
        else:
            r = radius_px(ward["x"], ward["y"], width, height, 1600)
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=tuple(style["fill"]), outline=tuple(style["color"] + [220]), width=2)

    for ward in active_wards:
        if ward["type"] != "sen":
            continue
        cx, cy = ward_to_px(ward["x"], ward["y"], width, height)
        r = radius_px(ward["x"], ward["y"], width, height, 1000)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(80, 180, 255, 245), width=3)

    for ward in active_wards:
        cx, cy = ward_to_px(ward["x"], ward["y"], width, height)
        style = RADIANT if ward["team"] == "radiant" else DIRE
        if ward["type"] not in ("obs", "sen"):
            r = radius_px(ward["x"], ward["y"], width, height, 1000)
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(80, 180, 255, 200), width=2)
        draw.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), fill=tuple(style["color"] + [255]), outline=(255, 255, 255, 240))

    tree_events = (((occlusion_payload or {}).get("treeDebug") or {}).get("eventsByTime") or {}).get(str(t), [])
    for event in tree_events:
        px, py = grid_to_px(event["cell"][0], event["cell"][1], width, height)
        color = (48, 235, 105, 245) if event.get("alive") else (255, 90, 70, 245)
        draw.ellipse((px - 5, py - 5, px + 5, py + 5), outline=color, width=3)

    result = Image.alpha_composite(base, overlay)
    draw = ImageDraw.Draw(result)
    font = ImageFont.load_default()
    hero_label = ""
    if hero_second:
        hero_label = f" hero={len(hero_second.get('heroes', []))}"
    label = f"{title}  t={t}s  {ward_count_label(active_wards)}{hero_label}"
    draw.rectangle((8, 8, 8 + len(label) * 7 + 12, 32), fill=(0, 0, 0, 180))
    draw.text((14, 14), label, fill=(255, 255, 255, 255), font=font)
    result.convert("RGB").save(out_path, quality=92)


def write_html(out_dir, image_name, payload):
    html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Dota Ward Vision Timeline</title>
  <style>
    :root { color-scheme: dark; font-family: Arial, "Microsoft YaHei", sans-serif; }
    body { margin: 0; background: #101317; color: #eef3f7; }
    main { max-width: 1120px; margin: 0 auto; padding: 20px; }
    .bar { display: grid; grid-template-columns: auto 1fr auto auto; gap: 12px; align-items: center; margin: 12px 0; }
    .toggles { display: flex; gap: 14px; flex-wrap: wrap; margin: 10px 0 12px; color: #c9d4df; font-size: 13px; }
    .toggles label { display: inline-flex; align-items: center; gap: 6px; }
    .meta { margin: 8px 0 12px; color: #98a7b6; font-size: 12px; }
    button { height: 34px; padding: 0 14px; border: 1px solid #3d4652; border-radius: 6px; background: #202833; color: #eef3f7; cursor: pointer; }
    input[type=range] { width: 100%; }
    canvas { width: 100%; height: auto; background: #07090b; border: 1px solid #2a323c; }
    .legend { display: flex; gap: 16px; flex-wrap: wrap; color: #bdc8d4; font-size: 13px; }
    .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
  </style>
</head>
<body>
  <main>
    <h2>8831926213 Ward Vision Timeline</h2>
    <div class="legend">
      <span><i class="dot" style="background:#42d276"></i>Radiant observer vision</span>
      <span><i class="dot" style="background:#ee5252"></i>Dire observer vision</span>
      <span><i class="dot" style="background:#48a0ff"></i>Radiant hero theoretical vision</span>
      <span><i class="dot" style="background:#ffac40"></i>Dire hero theoretical vision</span>
      <span><i class="dot" style="background:#50b4ff"></i>Sentry true sight range, no occlusion</span>
    </div>
    <div class="toggles">
      <label><input id="showWards" type="checkbox" checked /> Wards</label>
      <label><input id="showHeroes" type="checkbox" /> Hero vision</label>
      <label><input id="showTrees" type="checkbox" /> Tree events</label>
    </div>
    <div id="meta" class="meta"></div>
    <div class="bar">
      <button id="play">Play</button>
      <input id="time" type="range" />
      <strong id="clock"></strong>
      <span id="count"></span>
    </div>
    <canvas id="map"></canvas>
  </main>
  <script>
    const data = __PAYLOAD__;
    const img = new Image();
    img.src = "__IMAGE__";
    const canvas = document.getElementById("map");
    const ctx = canvas.getContext("2d");
    const slider = document.getElementById("time");
    const clock = document.getElementById("clock");
    const count = document.getElementById("count");
    const play = document.getElementById("play");
    const meta = document.getElementById("meta");
    const showWards = document.getElementById("showWards");
    const showHeroes = document.getElementById("showHeroes");
    const showTrees = document.getElementById("showTrees");
    let timer = null;

    slider.min = data.start;
    slider.max = data.end;
    slider.step = 1;
    slider.value = data.start;

    function hmap(x, y) {
      const h = data.homography;
      const w = h[2][0] * x + h[2][1] * y + h[2][2];
      return [
        (h[0][0] * x + h[0][1] * y + h[0][2]) / w,
        (h[1][0] * x + h[1][1] * y + h[1][2]) / w,
      ];
    }
    function px(x, y) {
      const [nx, ny] = hmap(x, y);
      return [nx * canvas.width, (1 - ny) * canvas.height];
    }
    function applyCalibration(px, py) {
      const c = data.defaultCalibration;
      return [
        (px - canvas.width / 2) * c.scale + canvas.width / 2 + c.x,
        (py - canvas.height / 2) * c.scale + canvas.height / 2 + c.y,
      ];
    }
    function worldPx(wx, wy) {
      if (data.projectionCalibration) {
        const a = data.projectionCalibration.affine;
        const sx = canvas.width / (data.projectionCalibration.imageWidth || canvas.width);
        const sy = canvas.height / (data.projectionCalibration.imageHeight || canvas.height);
        return [
          (a.a * wx + a.b * wy + a.c) * sx,
          (a.d * wx + a.e * wy + a.f) * sy,
        ];
      }
      const px = (wx - data.mapWorldMinX) / (data.mapWorldMaxX - data.mapWorldMinX) * canvas.width;
      const py = (data.mapWorldMaxY - wy) / (data.mapWorldMaxY - data.mapWorldMinY) * canvas.height;
      return applyCalibration(px, py);
    }
    function parserWorld(x, y) {
      return [
        x * data.worldUnitsPerParserUnit - data.worldParserOffset,
        y * data.worldUnitsPerParserUnit - data.worldParserOffset,
      ];
    }
    function wardPx(x, y) {
      const [wx, wy] = parserWorld(x, y);
      return worldPx(wx, wy);
    }
    function radius(x, y, dotaUnits) {
      const [wx, wy] = parserWorld(x, y);
      const [cx, cy] = worldPx(wx, wy);
      const [x2, y2] = worldPx(wx + dotaUnits, wy);
      const [x3, y3] = worldPx(wx, wy + dotaUnits);
      return (Math.hypot(cx - x2, cy - y2) + Math.hypot(cx - x3, cy - y3)) / 2;
    }
    function gridPx(gx, gy) {
      const cellSize = data.visionCellSize || 64;
      const centerOffset = data.visionCellCenterOffset || 0;
      const wx = (gx + centerOffset) * cellSize + data.visionWorldMinX;
      const wy = (gy + centerOffset) * cellSize + data.visionWorldMinY;
      return worldPx(wx, wy);
    }
    function gridCellRadius(gx, gy) {
      const cellSize = data.visionCellSize || 64;
      const centerOffset = data.visionCellCenterOffset || 0;
      const [cx, cy] = gridPx(gx, gy);
      const wx = (gx + centerOffset) * cellSize + data.visionWorldMinX;
      const wy = (gy + centerOffset) * cellSize + data.visionWorldMinY;
      const [x2, y2] = worldPx(wx + cellSize, wy);
      const [x3, y3] = worldPx(wx, wy + cellSize);
      return Math.max(1, (Math.hypot(cx - x2, cy - y2) + Math.hypot(cx - x3, cy - y3)) / 2);
    }
    function active(t) {
      return data.wards.filter(w => w.start <= t && (w.end === null || t < w.end));
    }
    function visionCellsAt(w, t) {
      if (w.visionTimeline) {
        const segment = w.visionTimeline.find(s => s.start <= t && t < s.end);
        if (segment) return segment.cells || [];
      }
      return w.visionCells || [];
    }
    function heroVisionAt(t) {
      if (!data.heroVision || !data.heroVision.seconds) return null;
      const offset = t - data.heroVision.start;
      const item = data.heroVision.seconds[offset];
      if (item && item.time === t) return item;
      return data.heroVision.seconds.find(s => s.time === t) || null;
    }
    function drawRle(runs, color) {
      ctx.fillStyle = color;
      for (const run of runs || []) {
        const gy = run[0], x0 = run[1], x1 = run[2];
        const [aX, aY] = gridPx(x0, gy);
        const [bX, bY] = gridPx(x1, gy);
        const rr = gridCellRadius(x0, gy) * 0.62;
        ctx.fillRect(
          Math.min(aX, bX) - rr,
          Math.min(aY, bY) - rr,
          Math.abs(bX - aX) + rr * 2,
          Math.abs(bY - aY) + rr * 2
        );
      }
    }
    function treeEventsAt(t) {
      const groups = data.treeDebug && data.treeDebug.eventsByTime;
      return groups ? (groups[String(t)] || []) : [];
    }
    function renderMeta(t, wards, heroSecond) {
      const dyn = data.occlusionSource && data.occlusionSource.dynamicState;
      const rejected = data.treeDebug && data.treeDebug.rejectedSummary;
      const treeNow = treeEventsAt(t).length;
      const heroCount = heroSecond ? heroSecond.heroes.length : 0;
      const rejectedText = rejected ? ` rejected ${rejected.total || 0}` : "";
      meta.textContent = `tree events now ${treeNow}; heroes ${heroCount}; accepted ${dyn ? dyn.treeEventsAccepted : 0};${rejectedText}`;
    }
    function countLabel(wards) {
      const obsR = wards.filter(w => w.type === "obs" && w.team === "radiant").length;
      const obsD = wards.filter(w => w.type === "obs" && w.team === "dire").length;
      const senR = wards.filter(w => w.type === "sen" && w.team === "radiant").length;
      const senD = wards.filter(w => w.type === "sen" && w.team === "dire").length;
      return `Obs R/D ${obsR}/${obsD} · Sentry R/D ${senR}/${senD}`;
    }
    function fmt(t) {
      const sign = t < 0 ? "-" : "";
      t = Math.abs(t);
      return `${sign}${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
    }
    function draw() {
      const t = Number(slider.value);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      const heroSecond = heroVisionAt(t);
      if (showHeroes.checked && heroSecond) {
        drawRle(heroSecond.radiant, "rgba(72,160,255,0.23)");
        drawRle(heroSecond.dire, "rgba(255,172,64,0.23)");
        for (const hero of heroSecond.heroes || []) {
          const [hx, hy] = worldPx(hero.worldX, hero.worldY);
          ctx.beginPath();
          ctx.arc(hx, hy, 3.4, 0, Math.PI * 2);
          ctx.fillStyle = hero.team === "radiant" ? "rgb(72,160,255)" : "rgb(255,172,64)";
          ctx.fill();
          ctx.strokeStyle = "rgba(255,255,255,0.8)";
          ctx.stroke();
        }
      }
      ctx.fillStyle = "rgba(35,235,90,0.86)";
      for (const tree of data.treePoints || []) {
        const [tx, ty] = worldPx(tree.x, tree.y);
        ctx.fillRect(tx - 1.5, ty - 1.5, 3, 3);
      }
      const wards = active(t);
      if (showWards.checked) for (const w of wards) {
        if (w.type !== "obs") continue;
        const [cx, cy] = wardPx(w.x, w.y);
        const radiant = w.team === "radiant";
        const color = radiant ? "66,210,118" : "238,82,82";
        const visionCells = visionCellsAt(w, t);
        if (visionCells.length) {
          ctx.fillStyle = `rgba(${color},0.47)`;
          for (const cell of visionCells) {
            const [pxc, pyc] = gridPx(cell[0], cell[1]);
            const rr = gridCellRadius(cell[0], cell[1]) * 0.62;
            ctx.fillRect(pxc - rr, pyc - rr, rr * 2, rr * 2);
          }
        } else if (w.type === "obs") {
          const r = radius(w.x, w.y, 1600);
          ctx.beginPath();
          ctx.arc(cx, cy, r, 0, Math.PI * 2);
          ctx.fillStyle = `rgba(${color},0.47)`;
          ctx.fill();
          ctx.lineWidth = 2;
          ctx.strokeStyle = `rgba(${color},0.9)`;
          ctx.stroke();
        }
      }
      if (showWards.checked) for (const w of wards) {
        if (w.type !== "sen") continue;
        const [cx, cy] = wardPx(w.x, w.y);
        const r = radius(w.x, w.y, 1000);
        ctx.beginPath();
        ctx.arc(cx, cy, r, 0, Math.PI * 2);
        ctx.setLineDash([8, 6]);
        ctx.lineWidth = 3;
        ctx.strokeStyle = "rgba(80,180,255,0.98)";
        ctx.stroke();
        ctx.setLineDash([]);
      }
      if (showWards.checked) for (const w of wards) {
        const [cx, cy] = wardPx(w.x, w.y);
        const radiant = w.team === "radiant";
        const color = radiant ? "66,210,118" : "238,82,82";
        ctx.beginPath();
        ctx.arc(cx, cy, 4.5, 0, Math.PI * 2);
        ctx.fillStyle = `rgb(${color})`;
        ctx.fill();
        ctx.strokeStyle = "white";
        ctx.stroke();
      }
      if (showTrees.checked) {
        for (const event of treeEventsAt(t)) {
          const [tx, ty] = gridPx(event.cell[0], event.cell[1]);
          ctx.beginPath();
          ctx.arc(tx, ty, 6, 0, Math.PI * 2);
          ctx.lineWidth = 3;
          ctx.strokeStyle = event.alive ? "rgba(48,235,105,0.98)" : "rgba(255,90,70,0.98)";
          ctx.stroke();
        }
      }
      clock.textContent = fmt(t);
      count.textContent = countLabel(wards);
      renderMeta(t, wards, heroSecond);
    }
    img.onload = () => {
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      draw();
    };
    slider.addEventListener("input", draw);
    showWards.addEventListener("change", draw);
    showHeroes.addEventListener("change", draw);
    showTrees.addEventListener("change", draw);
    play.addEventListener("click", () => {
      if (timer) {
        clearInterval(timer);
        timer = null;
        play.textContent = "Play";
        return;
      }
      play.textContent = "Pause";
      timer = setInterval(() => {
        let t = Number(slider.value) + 1;
        if (t > Number(slider.max)) t = Number(slider.min);
        slider.value = t;
        draw();
      }, 120);
    });
  </script>
</body>
</html>
"""
    html = html.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    html = html.replace("__IMAGE__", image_name)
    (out_dir / "index.html").write_text(html, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--match-id", type=int, default=8831926213)
    parser.add_argument("--map", default=str(RESOURCE_ROOT / "maps" / "7.41_map.png"))
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "outputs" / "8831926213_ward_vision"))
    parser.add_argument("--occlusion-cells")
    parser.add_argument("--input-json", help="Use an existing ward_timeline.json instead of querying ward_placed_left_fact.")
    parser.add_argument("--projection-calibration", help="JSON file containing a world_to_pixel_affine calibration.")
    parser.add_argument("--preview-times", help="Comma-separated seconds to render in addition to standard previews.")
    parser.add_argument("--tree-points", help="JSON containing a trees array with world x/y coordinates.")
    parser.add_argument("--hero-vision", help="JSON generated by compute_hero_vision_native.py.")
    args = parser.parse_args()

    global PROJECTION_CALIBRATION, TREE_POINTS, HERO_VISION
    if args.projection_calibration:
        PROJECTION_CALIBRATION = json.loads(Path(args.projection_calibration).read_text(encoding="utf-8"))
    if args.tree_points:
        tree_payload = json.loads(Path(args.tree_points).read_text(encoding="utf-8"))
        TREE_POINTS = tree_payload if isinstance(tree_payload, list) else tree_payload.get("trees", [])
    if args.hero_vision:
        HERO_VISION = json.loads(Path(args.hero_vision).read_text(encoding="utf-8"))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base_path = Path(args.map)
    image_name = base_path.name
    target_base_path = out_dir / image_name
    if base_path.resolve() != target_base_path.resolve():
        shutil.copyfile(base_path, target_base_path)

    if args.input_json:
        existing_payload = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
        rows = []
        intervals = existing_payload["wards"]
        unmatched = existing_payload.get("unmatched", [])
    else:
        rows = query_wards(args.match_id)
        intervals, unmatched = build_intervals(rows)
    occlusion = None
    global occlusion_payload
    occlusion_payload = None
    if args.occlusion_cells:
        occlusion = json.loads(Path(args.occlusion_cells).read_text(encoding="utf-8"))
        occlusion_payload = occlusion
        global VISION_CELL_SIZE, VISION_WORLD_MIN_X, VISION_WORLD_MIN_Y, VISION_CELL_CENTER_OFFSET
        occlusion_grid = (occlusion.get("source") or {}).get("grid") or {}
        VISION_CELL_SIZE = float((occlusion.get("source") or {}).get("cellSize") or VISION_CELL_SIZE)
        VISION_CELL_CENTER_OFFSET = float(
            (occlusion.get("source") or {}).get("cellCenterOffset")
            if (occlusion.get("source") or {}).get("cellCenterOffset") is not None
            else VISION_CELL_CENTER_OFFSET
        )
        VISION_WORLD_MIN_X = float(occlusion_grid.get("worldMinX") or VISION_WORLD_MIN_X)
        VISION_WORLD_MIN_Y = float(occlusion_grid.get("worldMinY") or VISION_WORLD_MIN_Y)
        cells_by_handle = {int(r["ehandle"]): r for r in occlusion.get("results", [])}
        for ward in intervals:
            result = cells_by_handle.get(int(ward["ehandle"]))
            if result and result.get("cells"):
                ward["visionCells"] = result["cells"]
                ward["visionTimeline"] = result.get("visionTimeline", [])
                ward["lightArea"] = result.get("lightArea")
                ward["originGrid"] = result.get("originGrid") or result.get("grid")
                ward["snappedOrigin"] = bool(result.get("snapped"))
    end_values = [w["end"] for w in intervals if w["end"] is not None]
    start = min(w["start"] for w in intervals)
    end = max(end_values) if end_values else max(w["start"] for w in intervals)
    tree_debug_events_by_time = {}
    if occlusion:
        for event in occlusion.get("treeEvents", []):
            second = str(int(event["second"]))
            tree_debug_events_by_time.setdefault(second, []).append(
                {
                    "cell": event["cell"],
                    "alive": event["alive"],
                    "sourceIndex": event.get("sourceIndex"),
                }
            )
    payload = {
        "match_id": args.match_id,
        "map_image": image_name,
        "source_map_path": project_path(base_path),
        "start": start,
        "end": end,
        "worldUnitsPerParserUnit": WORLD_UNITS_PER_PARSER_UNIT,
        "worldParserOffset": WORLD_PARSER_OFFSET,
        "visionWorldMinX": VISION_WORLD_MIN_X,
        "visionWorldMinY": VISION_WORLD_MIN_Y,
        "visionCellSize": VISION_CELL_SIZE,
        "visionCellCenterOffset": VISION_CELL_CENTER_OFFSET,
        "mapWorldMinX": MAP_WORLD_MIN_X,
        "mapWorldMaxX": MAP_WORLD_MAX_X,
        "mapWorldMinY": MAP_WORLD_MIN_Y,
        "mapWorldMaxY": MAP_WORLD_MAX_Y,
        "defaultCalibration": DEFAULT_CALIBRATION,
        "projectionCalibration": PROJECTION_CALIBRATION,
        "homography": H741,
        "notes": [
            "Observer wards are drawn from precomputed visibility cells when occlusion data is present.",
            "Sentry wards are drawn as unobstructed true sight rings; only observer wards use occlusion.",
            "The current observer engine uses Valve cache.fow angular intervals and native FoW tile-byte height, tree, and explicit-blocker rules.",
            "When projectionCalibration is present, rendering uses its world_to_pixel_affine matrix; otherwise it uses map bounds plus x=13 y=-16 scale=1.0655.",
            "Optional heroVision is theoretical native FoW from alive hero positions in player_intervals2; it does not include abilities, invisibility, smoke, or shared unit vision.",
        ],
        "wards": intervals,
        "heroVision": HERO_VISION,
        "treeDebug": None if not occlusion else {
            "eventsByTime": tree_debug_events_by_time,
            "rejectedSummary": ((occlusion.get("source") or {}).get("dynamicState") or {}).get("rejectedTreeEventSummary"),
        },
        "treePoints": [{"x": float(tree["x"]), "y": float(tree["y"])} for tree in TREE_POINTS],
        "unmatched": unmatched,
        "occlusionSource": None if not occlusion else occlusion.get("source"),
    }
    (out_dir / "ward_timeline.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_html(out_dir, image_name, payload)

    preview_times = [start, 0, 300, 600, 900, 1200, 1419, 1500, end]
    if args.preview_times:
        preview_times.extend(int(value.strip()) for value in args.preview_times.split(",") if value.strip())
    for t in sorted(set(t for t in preview_times if start <= t <= end)):
        draw_snapshot(base_path, out_dir / f"preview_t{t:+05d}.jpg", intervals, t, f"match {args.match_id}")

    print(json.dumps({
        "out_dir": str(out_dir),
        "rows": len(rows),
        "wards": len(intervals),
        "unmatched": len(unmatched),
        "start": start,
        "end": end,
        "html": str(out_dir / "index.html"),
        "json": str(out_dir / "ward_timeline.json"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
