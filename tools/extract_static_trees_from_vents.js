const fs = require('fs');
const path = require('path');

function arg(name, fallback = null) {
  const i = process.argv.indexOf(name);
  return i >= 0 && i + 1 < process.argv.length ? process.argv[i + 1] : fallback;
}

function parseValue(raw) {
  raw = raw.trim();
  const resourceMatch = raw.match(/^resource_name:"([^"]+)"/);
  if (resourceMatch) return resourceMatch[1];
  if (raw === 'true') return true;
  if (raw === 'false') return false;
  const vector = raw.match(/^"?(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)"?$/);
  if (vector) return vector.slice(1).map(Number);
  const array = raw.match(/^\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]$/);
  if (array) return array.slice(1).map(Number);
  const quoted = raw.match(/^"([^"]*)"$/);
  if (quoted) return quoted[1];
  const num = Number(raw);
  if (Number.isFinite(num)) return num;
  return raw;
}

function readEntities(text) {
  const entities = [];
  let current = null;

  for (const line of text.split(/\r?\n/)) {
    const header = line.match(/^====(\d+)====$/);
    if (header) {
      if (current) entities.push(current);
      current = { blockIndex: Number(header[1]) };
      continue;
    }
    if (!current || !line.trim()) continue;
    const m = line.match(/^([^\s]+)\s+(.+)$/);
    if (!m) continue;
    current[m[1]] = parseValue(m[2]);
  }

  if (current) entities.push(current);
  return entities;
}

const input = arg('--input');
const output = arg('--output');

if (!input || !output) {
  console.error('Usage: node extract_static_trees_from_vents.js --input default_ents.vents --output static_trees_full.json');
  process.exit(2);
}

const text = fs.readFileSync(input, 'utf8');
const entities = readEntities(text);
const trees = entities
  .filter((e) => e.classname === 'ent_dota_tree')
  .map((e, treeId) => {
    const origin = Array.isArray(e.origin) ? e.origin : [null, null, null];
    const scales = Array.isArray(e.scales) ? e.scales : [1, 1, 1];
    const angles = Array.isArray(e.angles) ? e.angles : [0, 0, 0];
    return {
      treeId,
      blockIndex: e.blockIndex,
      hammeruniqueid: e.hammeruniqueid != null ? String(e.hammeruniqueid) : null,
      compile_source_id: e.compile_source_id != null ? String(e.compile_source_id) : null,
      model: e.model || null,
      skin: e.skin != null ? String(e.skin) : null,
      body: e.body != null ? String(e.body) : null,
      x: origin[0],
      y: origin[1],
      z: origin[2],
      angleZ: angles[1],
      scaleX: scales[0],
      scaleY: scales[1],
      scaleZ: scales[2],
      rendercolor: e.rendercolor || null,
    };
  });

const modelCounts = new Map();
for (const tree of trees) {
  modelCounts.set(tree.model, (modelCounts.get(tree.model) || 0) + 1);
}

const payload = {
  source: path.resolve(input),
  treeCount: trees.length,
  uniqueModelCount: modelCounts.size,
  modelCounts: [...modelCounts.entries()]
    .map(([model, count]) => ({ model, count }))
    .sort((a, b) => b.count - a.count || String(a.model).localeCompare(String(b.model))),
  trees,
};

fs.mkdirSync(path.dirname(output), { recursive: true });
fs.writeFileSync(output, JSON.stringify(payload, null, 2));
console.log(`wrote ${trees.length} trees, ${modelCounts.size} models -> ${output}`);
