$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Resources = Join-Path $ProjectRoot "resources"
$OutputDir = Join-Path $ProjectRoot "outputs\8831926213_ward_vision_native_fow"
$OcclusionPath = Join-Path $OutputDir "ward_occlusion_cells.json"
$TreeEventsSql = @"
SELECT time, log_index, state, treeId
FROM dota2_stats.dota_tree_state_change
WHERE match_id=%s
ORDER BY time, log_index
"@

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

python (Join-Path $PSScriptRoot "compute_ward_occlusion_native.py") `
  --input (Join-Path $Resources "matches\8831926213\ward_timeline_source.json") `
  --grid (Join-Path $Resources "native-fow\dota_static_fow_grid.json") `
  --cache (Join-Path $Resources "native-fow\cache.fow") `
  --tree-points (Join-Path $Resources "source\dota-map-trees.csv") `
  --tree-events-sql $TreeEventsSql `
  --output $OcclusionPath `
  --radius 1600

if ($LASTEXITCODE -ne 0) {
  throw "Ward occlusion generation failed with exit code $LASTEXITCODE."
}

python (Join-Path $PSScriptRoot "render_ward_vision.py") `
  --match-id 8831926213 `
  --map (Join-Path $Resources "maps\7.41_map.png") `
  --out-dir $OutputDir `
  --input-json (Join-Path $Resources "matches\8831926213\ward_timeline_source.json") `
  --occlusion-cells $OcclusionPath `
  --projection-calibration (Join-Path $Resources "calibration\projection_741_aerial_14pt.json") `
  --preview-times 1358,1800

if ($LASTEXITCODE -ne 0) {
  throw "Timeline rendering failed with exit code $LASTEXITCODE."
}

Write-Host "Timeline ready: $(Join-Path $OutputDir 'index.html')"
