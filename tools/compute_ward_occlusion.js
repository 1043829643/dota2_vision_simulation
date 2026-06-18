const fs = require("fs");
const path = require("path");

const VisionSimulation = require("../package/src/vision-simulation.js");
const worlddata = require("../resources/map-data/worlddata.json");
const PROJECT_ROOT = path.resolve(__dirname, "..");

const WORLD_UNITS_PER_PARSER_UNIT = 128;
const PARSER_ORIGIN = 128;
const WORLD_PARSER_OFFSET = 16384;
const DEFAULT_CELL_SIZE = 64;

function projectPath(filePath) {
  const absolute = path.resolve(filePath);
  const relative = path.relative(PROJECT_ROOT, absolute);
  return relative.startsWith("..") ? absolute : relative.replaceAll("\\", "/");
}

function parserToWorld(x, y) {
  return {
    x: x * WORLD_UNITS_PER_PARSER_UNIT - WORLD_PARSER_OFFSET,
    y: y * WORLD_UNITS_PER_PARSER_UNIT - WORLD_PARSER_OFFSET,
  };
}

function nearestValidGrid(vs, grid, maxDistance = 8) {
  if (vs.isValidXY(grid.x, grid.y, true, true, true)) return grid;
  for (let d = 1; d <= maxDistance; d++) {
    let best = null;
    for (let dx = -d; dx <= d; dx++) {
      for (let dy = -d; dy <= d; dy++) {
        if (Math.max(Math.abs(dx), Math.abs(dy)) !== d) continue;
        const candidate = { x: grid.x + dx, y: grid.y + dy };
        if (!vs.isValidXY(candidate.x, candidate.y, true, true, true)) continue;
        const dist2 = dx * dx + dy * dy;
        if (!best || dist2 < best.dist2) {
          best = { ...candidate, key: `${candidate.x},${candidate.y}`, dist2 };
        }
      }
    }
    if (best) return best;
  }
  return null;
}

function addGridDisk(map, x, y, radiusCells) {
  const radius = Math.max(0, Math.round(radiusCells));
  for (let dx = -radius; dx <= radius; dx++) {
    for (let dy = -radius; dy <= radius; dy++) {
      if (dx * dx + dy * dy > radius * radius) continue;
      const gx = x + dx;
      const gy = y + dy;
      map[`${gx},${gy}`] = { x: gx, y: gy, key: `${gx},${gy}` };
    }
  }
}

function applyFowBlockerLines(vs, blockerLinePath, cellSize, maxSegmentLength, lineRadiusWorld) {
  if (!blockerLinePath) return { segmentCount: 0, cellCountBefore: Object.keys(vs.ent_fow_blocker_node).length, cellCountAfter: Object.keys(vs.ent_fow_blocker_node).length };
  const payload = JSON.parse(fs.readFileSync(blockerLinePath, "utf8"));
  let segmentCount = 0;
  const before = Object.keys(vs.ent_fow_blocker_node).length;
  const radiusCells = lineRadiusWorld / cellSize;
  for (const segment of payload.segments || []) {
    if (segment.length > maxSegmentLength) continue;
    const a = segment.a;
    const b = segment.b;
    const steps = Math.max(1, Math.ceil(segment.length / (cellSize / 2)));
    for (let i = 0; i <= steps; i++) {
      const t = i / steps;
      const wx = a.x + (b.x - a.x) * t;
      const wy = a.y + (b.y - a.y) * t;
      const grid = vs.WorldXYtoGridXY(wx, wy);
      addGridDisk(vs.ent_fow_blocker_node, grid.x, grid.y, radiusCells);
    }
    segmentCount++;
  }
  // Invalidate blocker-dependent caches created by updateVisibility.
  vs.paddedFowBlockerNode = null;
  vs.fowBlockerObstacles = null;
  return { segmentCount, cellCountBefore: before, cellCountAfter: Object.keys(vs.ent_fow_blocker_node).length };
}

function getArg(args, name) {
  const index = args.indexOf(name);
  if (index === -1) return undefined;
  return args[index + 1];
}

function loadExternalTrees(treePath, radiusField, fixedRadius, radiusBuckets, radiusAdd, radiusMax) {
  if (!treePath) return null;
  const payload = JSON.parse(fs.readFileSync(treePath, "utf8"));
  const sourceTrees = Array.isArray(payload) ? payload : payload.trees;
  if (!Array.isArray(sourceTrees)) {
    throw new Error(`External tree file has no trees array: ${treePath}`);
  }
  return sourceTrees.map((tree, index) => {
    const requestedRadius = Number(tree[radiusField]);
    const sourceRadius = Number.isFinite(requestedRadius) && requestedRadius > 0
      ? requestedRadius
      : Number(tree.radiusAxis);
    let radiusWorld = fixedRadius !== undefined
      ? fixedRadius
      : radiusBuckets
        ? (sourceRadius < radiusBuckets.threshold ? radiusBuckets.low : radiusBuckets.high)
        : sourceRadius;
    if (fixedRadius === undefined && !radiusBuckets) {
      radiusWorld += radiusAdd;
      if (radiusMax !== undefined) radiusWorld = Math.min(radiusWorld, radiusMax);
    }
    if (!Number.isFinite(radiusWorld) || radiusWorld <= 0) {
      throw new Error(`Invalid tree radius at index ${index}: ${tree[radiusField]}`);
    }
    return {
      x: Number(tree.x),
      y: Number(tree.y),
      z: Number(tree.z),
      radiusWorld,
      treeId: tree.treeId,
      model: tree.model,
    };
  });
}

function computeWard(vs, ward, cellSize) {
  if (ward.type !== "obs") {
    return { ehandle: ward.ehandle, type: ward.type, cells: [] };
  }
  const world = parserToWorld(ward.x, ward.y);
  const grid = vs.WorldXYtoGridXY(world.x, world.y);
  const continuousGrid = vs.WorldXYtoGridXY(world.x, world.y, true);
  if (!vs.elevationGrid[grid.key]) {
    return {
      ehandle: ward.ehandle,
      type: ward.type,
      invalid: true,
      grid,
      world,
      cells: [],
    };
  }
  // Replay coordinates are authoritative. Terrain shadowcasting uses the nearest
  // grid cell, while tree raycasts retain the exact continuous origin.
  vs.updateVisibility(grid.x, grid.y, Math.floor(1600 / cellSize), continuousGrid);
  const cells = Object.keys(vs.lights).map((key) => {
    const [x, y] = key.split(",").map(Number);
    return [x, y];
  });
  return {
    ehandle: ward.ehandle,
    type: ward.type,
    grid,
    originGrid: grid,
    continuousOriginGrid: continuousGrid,
    snapped: false,
    world,
    lightArea: vs.lightArea,
    area: vs.area,
    cells,
  };
}

function main() {
  const args = process.argv.slice(2);
  const input = getArg(args, "--input");
  const mapData = getArg(args, "--map-data");
  const output = getArg(args, "--output");
  const cellSizeArg = getArg(args, "--cell-size");
  const cellSize = cellSizeArg ? Number(cellSizeArg) : DEFAULT_CELL_SIZE;
  const blockerPaddingArg = getArg(args, "--blocker-padding-cells");
  const blockerPaddingCells = blockerPaddingArg ? Number(blockerPaddingArg) : 0;
  const treeShadowRadiusArg = getArg(args, "--tree-shadow-radius-cells");
  const treeShadowRadiusCells = treeShadowRadiusArg ? Number(treeShadowRadiusArg) : undefined;
  const treeClusterPaddingArg = getArg(args, "--tree-cluster-padding-cells");
  const treeClusterPaddingCells = treeClusterPaddingArg ? Number(treeClusterPaddingArg) : 0;
  const treeClusterMinArg = getArg(args, "--tree-cluster-min-cells");
  const treeClusterMinCells = treeClusterMinArg ? Number(treeClusterMinArg) : 18;
  const fowBlockerLines = getArg(args, "--fow-blocker-lines");
  const maxFowSegmentLength = Number(getArg(args, "--max-fow-segment-length") || 256);
  const fowLineRadiusWorld = Number(getArg(args, "--fow-line-radius-world") || 96);
  const externalTreesPath = getArg(args, "--external-trees");
  const externalTreeShape = getArg(args, "--external-tree-shape") || "circle";
  const treeBodyVisibleArg = getArg(args, "--tree-body-visible");
  const treeBodyVisible = treeBodyVisibleArg === undefined
    ? true
    : !["false", "0", "no"].includes(String(treeBodyVisibleArg).toLowerCase());
  const treeRadiusField = getArg(args, "--tree-radius-field") || "radiusAxis";
  const fixedTreeRadiusArg = getArg(args, "--fixed-tree-radius-world");
  const fixedTreeRadiusWorld = fixedTreeRadiusArg === undefined ? undefined : Number(fixedTreeRadiusArg);
  const treeRadiusAdd = Number(getArg(args, "--tree-radius-add-world") || 0);
  const treeRadiusMaxArg = getArg(args, "--tree-radius-max-world");
  const treeRadiusMax = treeRadiusMaxArg === undefined ? undefined : Number(treeRadiusMaxArg);
  const treeRadiusThresholdArg = getArg(args, "--tree-radius-threshold");
  const treeRadiusLowArg = getArg(args, "--tree-radius-low");
  const treeRadiusHighArg = getArg(args, "--tree-radius-high");
  const radiusBucketArgs = [treeRadiusThresholdArg, treeRadiusLowArg, treeRadiusHighArg];
  const hasAnyRadiusBucketArg = radiusBucketArgs.some((value) => value !== undefined);
  const hasAllRadiusBucketArgs = radiusBucketArgs.every((value) => value !== undefined);
  const treeRadiusBuckets = hasAllRadiusBucketArgs ? {
    threshold: Number(treeRadiusThresholdArg),
    low: Number(treeRadiusLowArg),
    high: Number(treeRadiusHighArg),
  } : null;
  if (!input || !mapData || !output) {
    console.error("Usage: node tools/compute_ward_occlusion.js --input ward_timeline.json --map-data map_data_741.rgba --output ward_occlusion_cells.json [--cell-size 32]");
    process.exit(2);
  }
  if (!Number.isFinite(cellSize) || cellSize <= 0) {
    console.error(`Invalid --cell-size ${cellSizeArg}`);
    process.exit(2);
  }
  if (!Number.isFinite(blockerPaddingCells) || blockerPaddingCells < 0) {
    console.error(`Invalid --blocker-padding-cells ${blockerPaddingArg}`);
    process.exit(2);
  }
  if (treeShadowRadiusCells !== undefined && (!Number.isFinite(treeShadowRadiusCells) || treeShadowRadiusCells <= 0)) {
    console.error(`Invalid --tree-shadow-radius-cells ${treeShadowRadiusArg}`);
    process.exit(2);
  }
  if (!Number.isFinite(treeClusterPaddingCells) || treeClusterPaddingCells < 0) {
    console.error(`Invalid --tree-cluster-padding-cells ${treeClusterPaddingArg}`);
    process.exit(2);
  }
  if (!Number.isFinite(treeClusterMinCells) || treeClusterMinCells < 1) {
    console.error(`Invalid --tree-cluster-min-cells ${treeClusterMinArg}`);
    process.exit(2);
  }
  if (fixedTreeRadiusWorld !== undefined && (!Number.isFinite(fixedTreeRadiusWorld) || fixedTreeRadiusWorld <= 0)) {
    console.error(`Invalid --fixed-tree-radius-world ${fixedTreeRadiusArg}`);
    process.exit(2);
  }
  if (!Number.isFinite(treeRadiusAdd) || treeRadiusAdd < 0) {
    console.error(`Invalid --tree-radius-add-world ${treeRadiusAdd}`);
    process.exit(2);
  }
  if (treeRadiusMax !== undefined && (!Number.isFinite(treeRadiusMax) || treeRadiusMax <= 0)) {
    console.error(`Invalid --tree-radius-max-world ${treeRadiusMaxArg}`);
    process.exit(2);
  }
  if (hasAnyRadiusBucketArg && !hasAllRadiusBucketArgs) {
    console.error("--tree-radius-threshold, --tree-radius-low and --tree-radius-high must be provided together");
    process.exit(2);
  }
  if (treeRadiusBuckets && Object.values(treeRadiusBuckets).some((value) => !Number.isFinite(value) || value <= 0)) {
    console.error("Invalid tree radius bucket parameters");
    process.exit(2);
  }
  if (!["circle", "square"].includes(externalTreeShape)) {
    console.error(`Invalid --external-tree-shape ${externalTreeShape}`);
    process.exit(2);
  }

  const payload = JSON.parse(fs.readFileSync(input, "utf8"));
  const observerWards = payload.wards.filter((ward) => ward.type === "obs");
  const externalTrees = loadExternalTrees(
    externalTreesPath,
    treeRadiusField,
    fixedTreeRadiusWorld,
    treeRadiusBuckets,
    treeRadiusAdd,
    treeRadiusMax
  );
  const vs = new VisionSimulation(worlddata, {
    cellSize,
    blockerPaddingCells,
    treeShadowRadiusCells,
    treeClusterPaddingCells,
    treeClusterMinCells,
    externalTreeRaycast: !!externalTrees,
    externalTreeShape,
    externalTreeBodyVisible: treeBodyVisible,
  });
  vs.initialize(mapData, (err) => {
    if (err) {
      console.error(err);
      process.exit(1);
    }
    if (externalTrees) {
      vs.setExternalTrees(externalTrees);
    }
    const fowLineSource = applyFowBlockerLines(vs, fowBlockerLines, cellSize, maxFowSegmentLength, fowLineRadiusWorld);
    const results = observerWards.map((ward, index) => {
      const result = computeWard(vs, ward, cellSize);
      if ((index + 1) % 10 === 0 || index === observerWards.length - 1) {
        console.error(`computed ${index + 1}/${observerWards.length}`);
      }
      return result;
    });
    const out = {
      match_id: payload.match_id,
      source: {
        input: projectPath(input),
        mapData: projectPath(mapData),
        worlddata,
        cellSize,
        blockerPaddingCells,
        treeShadowRadiusCells: treeShadowRadiusCells || Math.SQRT2,
        treeClusterPaddingCells,
        treeClusterMinCells,
        externalTrees: externalTrees ? {
          path: projectPath(externalTreesPath),
          count: externalTrees.length,
          radiusField: fixedTreeRadiusWorld === undefined ? treeRadiusField : null,
          fixedRadiusWorld: fixedTreeRadiusWorld,
          radiusBuckets: treeRadiusBuckets,
          radiusAddWorld: treeRadiusAdd,
          radiusMaxWorld: treeRadiusMax,
          occlusionMethod: `per-cell-segment-${externalTreeShape}-raycast`,
          shape: externalTreeShape,
          treeBodyVisible,
        } : null,
        grid: {
          width: vs.gridWidth,
          height: vs.gridHeight,
          worldMinX: vs.worldMinX,
          worldMinY: vs.worldMinY,
          worldMaxX: vs.worldMaxX,
          worldMaxY: vs.worldMaxY,
        },
        parserToWorld: {
          x: "parser_x * 128 - 16384",
          y: "parser_y * 128 - 16384",
        },
        fowBlockerLines: fowBlockerLines ? {
          path: projectPath(fowBlockerLines),
          maxSegmentLength: maxFowSegmentLength,
          lineRadiusWorld: fowLineRadiusWorld,
          ...fowLineSource,
        } : null,
      },
      observerWardCount: observerWards.length,
      results,
    };
    fs.writeFileSync(output, JSON.stringify(out));
  });
}

main();
