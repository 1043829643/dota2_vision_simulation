const fs = require("fs");
const path = require("path");

const VisionSimulation = require("../package/src/vision-simulation.js");
const worlddata = require("../resources/map-data/worlddata.json");

const WIDTH = 1024;
const HEIGHT = 1024;
const WORLD_UNITS_PER_PARSER_UNIT = 128;
const WORLD_PARSER_OFFSET = 16384;
const MAP_WORLD_MIN_X = -10829.42;
const MAP_WORLD_MAX_X = 11487.75;
const MAP_WORLD_MAX_Y = 11351.48;
const MAP_WORLD_MIN_Y = -10939.96;
const DEFAULT_CALIBRATION = { x: 13.0, y: -16.0, scale: 1.0655 };

function parseArgs() {
  const args = process.argv.slice(2);
  const get = (name, fallback) => {
    const i = args.indexOf(name);
    return i >= 0 ? args[i + 1] : fallback;
  };
  return {
    anchors: get("--anchors"),
    mapData: get("--map-data"),
    output: get("--output"),
    report: get("--report"),
    searchRadius: Number(get("--search-radius", "40")),
    minElevation: Number(get("--min-elevation", "80")),
  };
}

function currentPixelToWorld(px, py) {
  const rawPx = (px - DEFAULT_CALIBRATION.x - WIDTH / 2) / DEFAULT_CALIBRATION.scale + WIDTH / 2;
  const rawPy = (py - DEFAULT_CALIBRATION.y - HEIGHT / 2) / DEFAULT_CALIBRATION.scale + HEIGHT / 2;
  const wx = (rawPx / WIDTH) * (MAP_WORLD_MAX_X - MAP_WORLD_MIN_X) + MAP_WORLD_MIN_X;
  const wy = MAP_WORLD_MAX_Y - (rawPy / HEIGHT) * (MAP_WORLD_MAX_Y - MAP_WORLD_MIN_Y);
  return { wx, wy };
}

function solveLinear(matrix, rhs) {
  const n = rhs.length;
  const a = matrix.map((row, i) => row.concat(rhs[i]));
  for (let col = 0; col < n; col += 1) {
    let pivot = col;
    for (let r = col + 1; r < n; r += 1) {
      if (Math.abs(a[r][col]) > Math.abs(a[pivot][col])) pivot = r;
    }
    if (Math.abs(a[pivot][col]) < 1e-12) throw new Error("singular matrix");
    [a[col], a[pivot]] = [a[pivot], a[col]];
    const div = a[col][col];
    for (let c = col; c <= n; c += 1) a[col][c] /= div;
    for (let r = 0; r < n; r += 1) {
      if (r === col) continue;
      const factor = a[r][col];
      for (let c = col; c <= n; c += 1) a[r][c] -= factor * a[col][c];
    }
  }
  return a.map((row) => row[n]);
}

function fitAffine(pairs) {
  const ata = Array.from({ length: 6 }, () => Array(6).fill(0));
  const atb = Array(6).fill(0);
  for (const p of pairs) {
    const rows = [
      [p.worldX, p.worldY, 1, 0, 0, 0],
      [0, 0, 0, p.worldX, p.worldY, 1],
    ];
    const bs = [p.pixelX, p.pixelY];
    for (let r = 0; r < 2; r += 1) {
      for (let i = 0; i < 6; i += 1) {
        atb[i] += rows[r][i] * bs[r];
        for (let j = 0; j < 6; j += 1) ata[i][j] += rows[r][i] * rows[r][j];
      }
    }
  }
  const [a, b, c, d, e, f] = solveLinear(ata, atb);
  return { a, b, c, d, e, f };
}

function project(affine, wx, wy) {
  return {
    x: affine.a * wx + affine.b * wy + affine.c,
    y: affine.d * wx + affine.e * wy + affine.f,
  };
}

function findTerrainAnchor(vs, anchor, searchRadius, minElevation) {
  const seed = currentPixelToWorld(anchor.pixelX, anchor.pixelY);
  const seedGrid = vs.WorldXYtoGridXY(seed.wx, seed.wy);
  const candidates = [];
  for (let dx = -searchRadius; dx <= searchRadius; dx += 1) {
    for (let dy = -searchRadius; dy <= searchRadius; dy += 1) {
      const x = seedGrid.x + dx;
      const y = seedGrid.y + dy;
      const key = `${x},${y}`;
      const elevation = vs.elevationGrid[key] && vs.elevationGrid[key].z;
      if (elevation == null || elevation < minElevation) continue;
      if (!vs.isValidXY(x, y, true, true, true)) continue;
      const distanceGrid = Math.hypot(dx, dy);
      if (distanceGrid > searchRadius) continue;
      const world = vs.GridXYtoWorldXY(x, y);
      candidates.push({ x, y, elevation, distanceGrid, worldX: world.x, worldY: world.y });
    }
  }
  if (!candidates.length) throw new Error(`No terrain candidates for ${anchor.id}`);
  candidates.sort((a, b) => b.elevation - a.elevation || a.distanceGrid - b.distanceGrid);
  const maxElevation = candidates[0].elevation;
  const minDistance = candidates.find((c) => c.elevation === maxElevation).distanceGrid;
  const cluster = candidates.filter((c) => c.elevation === maxElevation && c.distanceGrid <= minDistance + 3);
  const avg = cluster.reduce((acc, c) => ({
    x: acc.x + c.x,
    y: acc.y + c.y,
    worldX: acc.worldX + c.worldX,
    worldY: acc.worldY + c.worldY,
  }), { x: 0, y: 0, worldX: 0, worldY: 0 });
  const n = cluster.length;
  return {
    id: anchor.id,
    label: anchor.label,
    pixelX: anchor.pixelX,
    pixelY: anchor.pixelY,
    seedWorldX: seed.wx,
    seedWorldY: seed.wy,
    seedGrid,
    gridX: avg.x / n,
    gridY: avg.y / n,
    worldX: avg.worldX / n,
    worldY: avg.worldY / n,
    parserX: (avg.worldX / n + WORLD_PARSER_OFFSET) / WORLD_UNITS_PER_PARSER_UNIT,
    parserY: (avg.worldY / n + WORLD_PARSER_OFFSET) / WORLD_UNITS_PER_PARSER_UNIT,
    elevation: maxElevation,
    candidates: candidates.slice(0, 12),
  };
}

async function initialize(vs, mapData) {
  return new Promise((resolve, reject) => {
    vs.initialize(mapData, (err) => (err ? reject(err) : resolve()));
  });
}

async function main() {
  const args = parseArgs();
  if (!args.anchors || !args.mapData || !args.output) {
    console.error("Usage: node tools/build_highground_projection_calibration.js --anchors highground_anchor_pixels.json --map-data map_data_741.rgba --output highground_projection_calibration.json [--report report.md]");
    process.exit(2);
  }
  const anchorPayload = JSON.parse(fs.readFileSync(args.anchors, "utf8"));
  const vs = new VisionSimulation(worlddata);
  await initialize(vs, args.mapData);
  const pairs = anchorPayload.anchors.map((anchor) => findTerrainAnchor(vs, anchor, args.searchRadius, args.minElevation));
  const affine = fitAffine(pairs);
  for (const pair of pairs) {
    const p = project(affine, pair.worldX, pair.worldY);
    pair.fittedPixelX = p.x;
    pair.fittedPixelY = p.y;
    pair.residualPx = Math.hypot(p.x - pair.pixelX, p.y - pair.pixelY);
  }
  const output = {
    type: "world_to_pixel_affine",
    image: anchorPayload.image,
    imageWidth: anchorPayload.imageWidth,
    imageHeight: anchorPayload.imageHeight,
    source: {
      anchors: path.resolve(args.anchors),
      mapData: path.resolve(args.mapData),
      worlddata,
      note: "Pixel anchors were clicked by the user. World anchors were snapped to the nearest valid high-elevation wardable terrain plateau using the old projection only as a search seed.",
    },
    affine,
    pairs,
    residual: {
      maxPx: Math.max(...pairs.map((p) => p.residualPx)),
      meanPx: pairs.reduce((sum, p) => sum + p.residualPx, 0) / pairs.length,
    },
  };
  fs.writeFileSync(args.output, JSON.stringify(output, null, 2));
  if (args.report) {
    const lines = [
      "# Highground Projection Calibration",
      "",
      `Output: ${path.resolve(args.output)}`,
      `Mean residual: ${output.residual.meanPx.toFixed(2)} px`,
      `Max residual: ${output.residual.maxPx.toFixed(2)} px`,
      "",
      "| anchor | clicked pixel | snapped world | parser | elevation | residual |",
      "| --- | ---: | ---: | ---: | ---: | ---: |",
    ];
    for (const p of pairs) {
      lines.push(`| ${p.label} | (${p.pixelX.toFixed(2)}, ${p.pixelY.toFixed(2)}) | (${p.worldX.toFixed(0)}, ${p.worldY.toFixed(0)}) | (${p.parserX.toFixed(2)}, ${p.parserY.toFixed(2)}) | ${p.elevation} | ${p.residualPx.toFixed(2)} px |`);
    }
    fs.writeFileSync(args.report, lines.join("\n"), "utf8");
  }
  console.log(JSON.stringify({ output: args.output, report: args.report, residual: output.residual }, null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
