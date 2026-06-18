const fs = require("fs");
const path = require("path");

const [inputHtml, outputHtml] = process.argv.slice(2);
if (!inputHtml || !outputHtml) {
  console.error("Usage: node build_standalone_html.js input.html output.html");
  process.exit(2);
}

const inputDir = path.dirname(path.resolve(inputHtml));
let html = fs.readFileSync(inputHtml, "utf8");
const imagePattern = /img\.src = "([^"]+)";/;
const match = html.match(imagePattern);
if (!match) {
  throw new Error("Could not find the map image assignment in the HTML");
}

const imagePath = path.resolve(inputDir, match[1]);
const extension = path.extname(imagePath).toLowerCase();
const mimeType = extension === ".jpg" || extension === ".jpeg" ? "image/jpeg" : "image/png";
const dataUrl = `data:${mimeType};base64,${fs.readFileSync(imagePath).toString("base64")}`;
html = html.replace(imagePattern, `img.src = "${dataUrl}";`);

fs.writeFileSync(outputHtml, html);
console.log(JSON.stringify({
  inputHtml: path.resolve(inputHtml),
  imagePath,
  outputHtml: path.resolve(outputHtml),
  bytes: fs.statSync(outputHtml).size,
}, null, 2));
