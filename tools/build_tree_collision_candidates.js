const fs = require('fs');
const path = require('path');

function arg(name, fallback = null) {
  const i = process.argv.indexOf(name);
  return i >= 0 && i + 1 < process.argv.length ? process.argv[i + 1] : fallback;
}

function basenameNoExt(modelPath) {
  return String(modelPath || '').split(/[\\/]/).pop().replace(/\.vmdl$/i, '').replace(/\.glb$/i, '');
}

function selectBounds(meshes) {
  const defaultMesh = meshes.find((m) => /model$/i.test(m.meshName) && !/stump|inspector/i.test(m.meshName)) || meshes[0];
  const stumpMesh = meshes.find((m) => /stump/i.test(m.meshName));
  const inspectorMesh = meshes.find((m) => /inspector/i.test(m.meshName));
  return {
    defaultRadiusAxis: defaultMesh ? defaultMesh.xyRadiusMaxAxis : null,
    defaultRadiusCorner: defaultMesh ? defaultMesh.xyRadiusCorner : null,
    stumpRadiusAxis: stumpMesh ? stumpMesh.xyRadiusMaxAxis : null,
    inspectorRadiusAxis: inspectorMesh ? inspectorMesh.xyRadiusMaxAxis : null,
    sourceMeshName: defaultMesh ? defaultMesh.meshName : null,
  };
}

const treesPath = arg('--trees');
const boundsPath = arg('--bounds');
const output = arg('--output');

if (!treesPath || !boundsPath || !output) {
  console.error('Usage: node build_tree_collision_candidates.js --trees static_trees_full.json --bounds tree_model_glb_bounds.json --output tree_collision_candidates.json');
  process.exit(2);
}

const treesPayload = JSON.parse(fs.readFileSync(treesPath, 'utf8'));
const boundsPayload = JSON.parse(fs.readFileSync(boundsPath, 'utf8'));
const boundsByModel = new Map();

for (const item of boundsPayload) {
  const model = basenameNoExt(item.file);
  if (model.endsWith('_physics')) continue;
  boundsByModel.set(model, selectBounds(item.meshes));
}

const trees = treesPayload.trees.map((tree) => {
  const modelKey = basenameNoExt(tree.model);
  const bounds = boundsByModel.get(modelKey) || {};
  const scale = Math.max(tree.scaleX || 1, tree.scaleY || 1);
  return {
    ...tree,
    modelKey,
    radiusAxis: bounds.defaultRadiusAxis != null ? bounds.defaultRadiusAxis * scale : null,
    radiusCorner: bounds.defaultRadiusCorner != null ? bounds.defaultRadiusCorner * scale : null,
    stumpRadiusAxis: bounds.stumpRadiusAxis != null ? bounds.stumpRadiusAxis * scale : null,
    inspectorRadiusAxis: bounds.inspectorRadiusAxis != null ? bounds.inspectorRadiusAxis * scale : null,
    radiusSourceMeshName: bounds.sourceMeshName || null,
  };
});

const missingModels = [...new Set(trees.filter((tree) => tree.radiusAxis == null).map((tree) => tree.model))];
const radii = trees.map((tree) => tree.radiusAxis).filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
const percentile = (p) => radii.length ? radii[Math.min(radii.length - 1, Math.floor((radii.length - 1) * p))] : null;

const payload = {
  sourceTrees: path.resolve(treesPath),
  sourceBounds: path.resolve(boundsPath),
  treeCount: trees.length,
  missingModels,
  radiusStats: {
    min: radii[0] ?? null,
    p25: percentile(0.25),
    median: percentile(0.5),
    p75: percentile(0.75),
    max: radii[radii.length - 1] ?? null,
  },
  trees,
};

fs.mkdirSync(path.dirname(output), { recursive: true });
fs.writeFileSync(output, JSON.stringify(payload, null, 2));
console.log(`wrote ${trees.length} collision candidates -> ${output}`);
