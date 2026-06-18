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
    factor: Number(get("--factor", "2")),
  };
}

function copyPixel(src, srcWidth, srcX, srcY, dst, dstWidth, dstX, dstY) {
  const srcIdx = (srcY * srcWidth + srcX) * 4;
  const dstIdx = (dstY * dstWidth + dstX) * 4;
  dst[dstIdx + 0] = src[srcIdx + 0];
  dst[dstIdx + 1] = src[srcIdx + 1];
  dst[dstIdx + 2] = src[srcIdx + 2];
  dst[dstIdx + 3] = src[srcIdx + 3];
}

function main() {
  const args = parseArgs();
  if (!args.input || !args.output || !Number.isInteger(args.factor) || args.factor < 2) {
    console.error("Usage: node tools/upscale_map_data_rgba.js --input map_data_741.rgba --output map_data_741_x2.rgba [--factor 2]");
    process.exit(2);
  }

  const meta = JSON.parse(fs.readFileSync(`${args.input}.json`, "utf8"));
  const src = fs.readFileSync(args.input);
  const layerCount = 5;
  const oldGridWidth = meta.width / layerCount;
  const oldGridHeight = meta.height;
  if (!Number.isInteger(oldGridWidth)) throw new Error(`Unexpected width ${meta.width}`);

  const newGridWidth = (oldGridWidth - 1) * args.factor + 1;
  const newGridHeight = (oldGridHeight - 1) * args.factor + 1;
  const newWidth = newGridWidth * layerCount;
  const newHeight = newGridHeight;
  const dst = Buffer.alloc(newWidth * newHeight * 4, 255);

  for (let layer = 0; layer < layerCount; layer += 1) {
    for (let y = 0; y < newGridHeight; y += 1) {
      const srcY = Math.min(oldGridHeight - 1, Math.round(y / args.factor));
      for (let x = 0; x < newGridWidth; x += 1) {
        const srcX = Math.min(oldGridWidth - 1, Math.round(x / args.factor));
        copyPixel(
          src,
          meta.width,
          layer * oldGridWidth + srcX,
          srcY,
          dst,
          newWidth,
          layer * newGridWidth + x,
          y,
        );
      }
    }
  }

  // Tree layer is sparse marker data, not a raster area. Rebuild it by placing one
  // marker per original tree at the scaled coordinate, preventing duplicate trees.
  const treeLayer = 1;
  for (let y = 0; y < newGridHeight; y += 1) {
    for (let x = 0; x < newGridWidth; x += 1) {
      const idx = (y * newWidth + treeLayer * newGridWidth + x) * 4;
      dst[idx + 0] = 255;
      dst[idx + 1] = 255;
      dst[idx + 2] = 255;
      dst[idx + 3] = 255;
    }
  }
  for (let y = 0; y < oldGridHeight; y += 1) {
    for (let x = 0; x < oldGridWidth; x += 1) {
      const srcIdx = (y * meta.width + treeLayer * oldGridWidth + x) * 4;
      const r = src[srcIdx + 0];
      const g = src[srcIdx + 1];
      const b = src[srcIdx + 2];
      if (g === 0 && b === 0) {
        const nx = x * args.factor;
        const ny = y * args.factor;
        const dstIdx = (ny * newWidth + treeLayer * newGridWidth + nx) * 4;
        dst[dstIdx + 0] = r;
        dst[dstIdx + 1] = g;
        dst[dstIdx + 2] = b;
        dst[dstIdx + 3] = 255;
      }
    }
  }

  fs.mkdirSync(path.dirname(args.output), { recursive: true });
  fs.writeFileSync(args.output, dst);
  fs.writeFileSync(`${args.output}.json`, JSON.stringify({ width: newWidth, height: newHeight }));
  console.log(JSON.stringify({
    input: path.resolve(args.input),
    output: path.resolve(args.output),
    factor: args.factor,
    oldGridWidth,
    oldGridHeight,
    newGridWidth,
    newGridHeight,
    oldCellSize: 64,
    newCellSize: 64 / args.factor,
  }, null, 2));
}

main();
