# Dota 2 Map Data 7.41

Source repository: https://github.com/leamare/dota-interactive-map

Source commit: `bc73d0e3ea6a421d43780a017aa92e0288c939b5`

Copied on: 2026-06-16

## Contents

- `data/741/mapdata.json`: map entities and unit stats.
- `data/741/elevation.json`: elevation polygons generated from map data.
- `data/741/ent_fow_blocker_node.json`: vision blocker polygons.
- `data/741/no_wards.json`: invalid ward placement polygons.
- `data/741/map_zones.json`: named/zone overlay data.
- `data/741/riverflow.json`: river path/flow overlay data.
- `data/741/npc_dota_spawner.json`: lane spawn overlay data.
- `data/741/path_corner.json`: lane path overlay data.
- `data/741/tree_ids.csv`: tree IDs extracted from local Steam map files.
- `data/741/tree_ids.json`: JSON version of `tree_ids.csv`.
- `img/map_data_741.png`: encoded vision-simulation image used by `dota-vision-simulation`.

## Notes

Public 7.41d patch notes do not mention map geometry, tree, terrain, ward placement, or vision blocker changes. This 7.41 data is therefore the best available public map-data match for 7.41d unless Valve made undocumented map-file changes.

The tree ID data came from `resources/source/dota-map-trees.csv`. It contains 2475 `ent_dota_tree` rows and matches `data/741/mapdata.json` by `(x, y)` coordinate with no missing rows on either side.
