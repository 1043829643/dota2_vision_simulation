const fs = require('fs');
const path = require('path');

function arg(name, fallback = null) {
  const i = process.argv.indexOf(name);
  return i >= 0 && i + 1 < process.argv.length ? process.argv[i + 1] : fallback;
}

function parseGlb(file) {
  const buf = fs.readFileSync(file);
  const u32 = (offset) => buf.readUInt32LE(offset);
  let offset = 12;
  let json = null;
  let bin = null;

  while (offset < buf.length) {
    const len = u32(offset);
    const type = buf.toString('ascii', offset + 4, offset + 8);
    const start = offset + 8;
    if (type === 'JSON') {
      json = JSON.parse(buf.toString('utf8', start, start + len).replace(/\0+$/, ''));
    } else if (type === 'BIN\0') {
      bin = buf.subarray(start, start + len);
    }
    offset = start + len;
  }

  if (!json || !bin) throw new Error(`Invalid glb: ${file}`);
  return { json, bin };
}

function reader(json, bin, accessorIndex) {
  const accessor = json.accessors[accessorIndex];
  const view = json.bufferViews[accessor.bufferView];
  const componentSize = { 5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4 }[accessor.componentType];
  const componentCount = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4, MAT4: 16 }[accessor.type];
  const stride = view.byteStride || componentSize * componentCount;
  const base = (view.byteOffset || 0) + (accessor.byteOffset || 0);

  function readComponent(offset) {
    switch (accessor.componentType) {
      case 5126: return bin.readFloatLE(offset);
      case 5125: return bin.readUInt32LE(offset);
      case 5123: return bin.readUInt16LE(offset);
      case 5122: return bin.readInt16LE(offset);
      case 5121: return bin.readUInt8(offset);
      case 5120: return bin.readInt8(offset);
      default: throw new Error(`Unsupported component type ${accessor.componentType}`);
    }
  }

  return {
    count: accessor.count,
    read: (i, c) => readComponent(base + i * stride + c * componentSize),
  };
}

function measureFile(file) {
  const { json, bin } = parseGlb(file);
  return json.meshes.map((mesh, meshIndex) => {
    const min = [Infinity, Infinity, Infinity];
    const max = [-Infinity, -Infinity, -Infinity];
    let vertexCount = 0;

    for (const primitive of mesh.primitives || []) {
      const positionAccessor = primitive.attributes && primitive.attributes.POSITION;
      if (positionAccessor == null) continue;
      const r = reader(json, bin, positionAccessor);
      for (let i = 0; i < r.count; i += 1) {
        const x = r.read(i, 0);
        const y = r.read(i, 1);
        const z = r.read(i, 2);
        min[0] = Math.min(min[0], x);
        min[1] = Math.min(min[1], y);
        min[2] = Math.min(min[2], z);
        max[0] = Math.max(max[0], x);
        max[1] = Math.max(max[1], y);
        max[2] = Math.max(max[2], z);
        vertexCount += 1;
      }
    }

    const corners = [
      [min[0], min[1]],
      [min[0], max[1]],
      [max[0], min[1]],
      [max[0], max[1]],
    ];
    return {
      meshIndex,
      meshName: mesh.name || '',
      vertexCount,
      min,
      max,
      xyRadiusMaxAxis: Math.max(Math.abs(min[0]), Math.abs(max[0]), Math.abs(min[1]), Math.abs(max[1])),
      xyRadiusCorner: Math.max(...corners.map(([x, y]) => Math.hypot(x, y))),
    };
  });
}

function walk(dir) {
  const files = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) files.push(...walk(full));
    else if (entry.isFile() && entry.name.endsWith('.glb')) files.push(full);
  }
  return files;
}

const input = arg('--input');
const output = arg('--output');

if (!input || !output) {
  console.error('Usage: node measure_glb_bounds.js --input glb-dir-or-file --output bounds.json');
  process.exit(2);
}

const stat = fs.statSync(input);
const files = stat.isDirectory() ? walk(input) : [input];
const results = files.map((file) => ({
  file: path.resolve(file),
  meshes: measureFile(file),
}));

fs.mkdirSync(path.dirname(output), { recursive: true });
fs.writeFileSync(output, JSON.stringify(results, null, 2));
console.log(`measured ${results.length} glb files -> ${output}`);
