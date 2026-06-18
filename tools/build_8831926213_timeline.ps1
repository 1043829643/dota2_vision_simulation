$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Resources = Join-Path $ProjectRoot "resources"
$OutputDir = Join-Path $ProjectRoot "outputs\8831926213_full_match_aerial741_tree_invisible"
$OcclusionPath = Join-Path $OutputDir "ward_occlusion_cells.json"

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

node (Join-Path $PSScriptRoot "compute_ward_occlusion.js") `
  --input (Join-Path $Resources "matches\8831926213\ward_timeline_source.json") `
  --map-data (Join-Path $Resources "map-data\map_data_741.rgba") `
  --output $OcclusionPath `
  --cell-size 64 `
  --blocker-padding-cells 1 `
  --fow-blocker-lines (Join-Path $Resources "occlusion\fow_blocker_nodes.json") `
  --max-fow-segment-length 512 `
  --fow-line-radius-world 256 `
  --external-trees (Join-Path $Resources "trees\tree_collision_candidates.json") `
  --external-tree-shape circle `
  --tree-radius-field stumpRadiusAxis `
  --tree-radius-add-world 30 `
  --tree-radius-max-world 128 `
  --tree-body-visible false

if ($LASTEXITCODE -ne 0) {
  throw "Ward occlusion generation failed with exit code $LASTEXITCODE."
}

python (Join-Path $PSScriptRoot "render_ward_vision.py") `
  --match-id 8831926213 `
  --map (Join-Path $Resources "maps\7.41_map.png") `
  --out-dir $OutputDir `
  --input-json (Join-Path $Resources "matches\8831926213\ward_timeline_source.json") `
  --occlusion-cells $OcclusionPath `
  --projection-calibration (Join-Path $Resources "calibration\projection_741_aerial_14pt.json")

if ($LASTEXITCODE -ne 0) {
  throw "Timeline rendering failed with exit code $LASTEXITCODE."
}

Write-Host "Timeline ready: $(Join-Path $OutputDir 'index.html')"
