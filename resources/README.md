# Local Resource Bundle

All runtime and source resources used by the current 7.41 ward-vision pipeline
are stored below this directory. The pipeline no longer requires files from
`steam_track` or a specific Steam installation path.

## Runtime inputs

- `maps/7.41_map.png`: 7.41 aerial background map.
- `map-data/map_data_741.rgba`: elevation, tree, grid navigation, and FOW data.
- `calibration/projection_741_aerial_14pt.json`: current world-to-pixel projection.
- `trees/tree_collision_candidates.json`: 2306 static tree collision candidates.
- `trees/tree_model_glb_bounds.json`: per-model bounds used to derive tree radii.
- `trees/models/`: the 14 exported GLB tree models used for bounds measurement.
- `occlusion/fow_blocker_nodes.json`: extracted FOW blocker line segments.
- `matches/8831926213/ward_timeline_source.json`: ward lifetimes from StarRocks.
- Dynamic tree deaths/respawns are read at build time from
  `dota2_stats.dota_tree_state_change` using the local `DOTA_DB_*` connection
  environment variables.
- `dota2_stats.player_intervals2` and `dota2_stats.players`: alive hero
  positions and hero metadata for the optional per-second theoretical hero
  vision layer.
- `native-fow/cache.fow`: Valve's angular occlusion lookup table.
- `native-fow/dota_static_fow_grid.json`: native 64-unit FoW tile-byte grid.
- `native-fow/scripts/npc/`: unit day/night vision definitions.

## Traceability inputs

- `source/8831926213.dem`: replay used to validate coordinates and events.
- `source/dota-map-trees.csv`: original tree IDs.
- `source/default_ents.vents`: entity export used for trees and FOW blockers.
- `maps/tutorial_minimap.png`: legacy minimap retained for older render checks.

Run the current end-to-end build from any working directory:

```powershell
$env:DOTA_DB_HOST='...'
$env:DOTA_DB_PORT='9030'
$env:DOTA_DB_USER='...'
$env:DOTA_DB_PASSWORD='...'
powershell -ExecutionPolicy Bypass -File tools\build_8831926213_timeline.ps1
```

The build writes the shareable single-file page to:

```text
outputs/8831926213_ward_vision_native_fow_share/8831926213_Ward_Vision_Timeline.html
```
