const fs = require("fs");
const path = require("path");

function parseArgs() {
  const args = process.argv.slice(2);
  const get = (name, fallback) => {
    const i = args.indexOf(name);
    return i >= 0 ? args[i + 1] : fallback;
  };
  return {
    input: get("--input"),
    output: get("--output"),
    report: get("--report"),
    nearX: Number(get("--near-x", "3131")),
    nearY: Number(get("--near-y", "-4144")),
    nearRadius: Number(get("--near-radius", "2600")),
  };
}

function parseVentBlocks(text) {
  const chunks = text.split(/\r?\n====\d+====\r?\n/g);
  return chunks.map((chunk, index) => {
    const entity = { order: index };
    for (const line of chunk.split(/\r?\n/)) {
      const m = line.match(/^\s*([A-Za-z0-9_#]+)\s+(.*)$/);
      if (!m) continue;
      const key = m[1];
      let value = m[2].trim();
      if (value.startsWith('"') && value.endsWith('"')) value = value.slice(1, -1);
      entity[key] = value;
    }
    return entity;
  });
}

function parseOrigin(origin) {
  if (!origin) return null;
  const parts = origin.trim().split(/\s+/).map(Number);
  if (parts.length < 2 || parts.some((v) => !Number.isFinite(v))) return null;
  return { x: parts[0], y: parts[1], z: parts[2] || 0 };
}

function distPointSegment(px, py, ax, ay, bx, by) {
  const dx = bx - ax;
  const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  if (!len2) return Math.hypot(px - ax, py - ay);
  const t = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

function main() {
  const args = parseArgs();
  if (!args.input || !args.output) {
    console.error("Usage: node tools/extract_fow_blocker_nodes.js --input default_ents.vents --output fow_blocker_nodes.json [--report report.md]");
    process.exit(2);
  }
  const entities = parseVentBlocks(fs.readFileSync(args.input, "utf8"));
  const nodes = entities
    .filter((entity) => entity.classname === "ent_fow_blocker_node")
    .map((entity) => ({ ...entity, originParsed: parseOrigin(entity.origin) }))
    .filter((entity) => entity.originParsed);
  const groups = new Map();
  for (const node of nodes) {
    const name = node.targetname || "";
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(node);
  }

  const groupPayloads = [];
  const segments = [];
  for (const [targetname, groupNodes] of groups) {
    groupNodes.sort((a, b) => a.order - b.order);
    const points = groupNodes.map((node) => ({
      order: node.order,
      compileSourceId: node.compile_source_id,
      hammerUniqueId: node.hammeruniqueid,
      x: node.originParsed.x,
      y: node.originParsed.y,
      z: node.originParsed.z,
    }));
    groupPayloads.push({ targetname, count: points.length, points });
    for (let i = 1; i < points.length; i += 1) {
      const a = points[i - 1];
      const b = points[i];
      segments.push({
        targetname,
        a,
        b,
        length: Math.hypot(b.x - a.x, b.y - a.y),
        distanceToProbe: distPointSegment(args.nearX, args.nearY, a.x, a.y, b.x, b.y),
      });
    }
  }
  segments.sort((a, b) => a.distanceToProbe - b.distanceToProbe);

  const output = {
    source: path.resolve(args.input),
    nodeCount: nodes.length,
    groupCount: groupPayloads.length,
    note: "Segments are inferred by connecting ent_fow_blocker_node origins with the same targetname in compiled entity order.",
    groups: groupPayloads,
    segments,
  };
  fs.mkdirSync(path.dirname(args.output), { recursive: true });
  fs.writeFileSync(args.output, JSON.stringify(output, null, 2));

  if (args.report) {
    const lines = [
      "# FOW Blocker Nodes",
      "",
      `Source: ${path.resolve(args.input)}`,
      `Nodes: ${nodes.length}`,
      `Groups: ${groupPayloads.length}`,
      `Probe: (${args.nearX}, ${args.nearY}), radius ${args.nearRadius}`,
      "",
      "## Nearest Segments",
      "",
      "| distance | targetname | a | b | length |",
      "| ---: | --- | ---: | ---: | ---: |",
    ];
    for (const s of segments.filter((s) => s.distanceToProbe <= args.nearRadius).slice(0, 40)) {
      lines.push(`| ${s.distanceToProbe.toFixed(1)} | ${s.targetname} | (${s.a.x.toFixed(0)}, ${s.a.y.toFixed(0)}) | (${s.b.x.toFixed(0)}, ${s.b.y.toFixed(0)}) | ${s.length.toFixed(1)} |`);
    }
    fs.writeFileSync(args.report, lines.join("\n"), "utf8");
  }
  console.log(JSON.stringify({
    output: path.resolve(args.output),
    report: args.report && path.resolve(args.report),
    nodeCount: nodes.length,
    groupCount: groupPayloads.length,
    nearestDistance: segments[0] && segments[0].distanceToProbe,
  }, null, 2));
}

main();
