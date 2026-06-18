# Native FoW Integration

The current observer-ward engine replaces the legacy approximate shadowcasting
pipeline with rules reconstructed from Dota 2's server FoW implementation.

## Inputs

- `resources/native-fow/cache.fow`
  - Valve's 701 x 701 relative-cell angular lookup table.
- `resources/native-fow/dota_static_fow_grid.json`
  - 296 x 296 grid, 64 world units per cell.
  - World bounds: `[-9472, -9472]` to `[9472, 9472]`.
- `resources/native-fow/scripts/npc/`
  - Unit day/night vision definitions retained for future hero integration.

The static grid contains:

- 2306 tree cells.
- 393 FoW blocker nodes, represented by 376 unique cells.
- 5326 height-edge cells.
- Height thresholds `[96, 224, 352, 480]`.

Its tile-byte layout is:

```text
bits 0..4  height level
bit 5      0x20 height/cliff edge
bit 6      0x40 explicit FoW blocker
bit 7      0x80 tree
```

## Observer Calculation

`tools/compute_ward_occlusion_native.py` converts every observer ward from
parser coordinates to world coordinates:

```text
world_x = parser_x * 128 - 16384
world_y = parser_y * 128 - 16384
```

For each ward it:

1. Locates the native FoW viewer cell.
2. Reads the viewer height from the tile byte.
3. Builds the relevant tree, cliff, and explicit-blocker angular intervals
   from `cache.fow`.
4. Tests all target cells inside the native 1600-unit cell radius.
5. Emits visible native-grid cells for the existing timeline renderer.

The renderer uses a `0.5` cell-center offset because native FoW coordinates are
cell indices rather than world-space cell centers.

## Preserved Application Behavior

- StarRocks/replay ward lifetimes and teams.
- 7.41 parser-to-world conversion.
- 14-point world-to-map affine projection.
- Per-second Radiant/Dire timeline.
- Observer wards use occlusion.
- Sentry wards remain unobstructed true-sight circles.

## Validation

The batch implementation was compared cell-by-cell with the reference
`can_unit_see()` implementation for three real wards:

| ehandle | batch cells | reference cells | differences |
| ---: | ---: | ---: | ---: |
| 12780452 | 1037 | 1037 | 0 |
| 2329196 | 487 | 487 | 0 |
| 11306896 | 1643 | 1643 | 0 |

Across all 26 observer wards in match `8831926213`:

- Candidate cells: 50,986.
- Visible cells: 26,192.
- Visible ratio: 51.37%.

## Current Limitation

The native grid currently represents the initial static map state. Tree death
and respawn events, temporary revealers, and dynamic FoW blockers are not yet
applied per second. The output metadata records this explicitly under
`occlusionSource.dynamicState`.
