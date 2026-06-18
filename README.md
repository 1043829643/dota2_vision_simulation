# Dota 2 Vision Simulation

Dota 2 7.41 ward-vision simulation for match `8831926213`.

The project converts parser coordinates to world coordinates, projects them
onto a calibrated 7.41 map, and renders a per-second ward timeline:

- Observer wards use Valve `cache.fow` angular intervals and native FoW
  tile-byte terrain, tree, and explicit-blocker rules.
- Sentry wards use an unobstructed 1000-unit true-sight radius.
- Parser coordinates use `world = parser * 128 - 16384` on both axes.
- The current map projection is a manually calibrated 14-point affine fit.

## Demo

Open `demo/8831926213/index.html` in a browser.

## Build

Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

Generate the current timeline:

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_8831926213_timeline.ps1
```

The result is written to:

```text
outputs/8831926213_ward_vision_native_fow/index.html
```

## Resources

All runtime inputs are stored under `resources/`. The pipeline does not depend
on files in `steam_track` or a fixed Steam installation path.

The replay file is stored with Git LFS. Run `git lfs pull` after cloning if it
is needed for event verification; timeline rendering uses the extracted ward
JSON and does not require the replay.

See `resources/README.md` for the resource inventory and provenance.

## Database

Existing `ward_timeline_source.json` can be rendered without a database.
To query StarRocks again, provide credentials through environment variables:

```powershell
$env:DOTA_DB_HOST = "host"
$env:DOTA_DB_PORT = "9030"
$env:DOTA_DB_USER = "user"
$env:DOTA_DB_PASSWORD = "password"
```

## Main Tools

- `tools/compute_ward_occlusion_native.py`: current native FoW observer calculation.
- `tools/compute_ward_occlusion.js`: legacy shadowcasting implementation retained for comparison.
- `tools/render_ward_vision.py`: timeline HTML and preview renderer.
- `tools/build_8831926213_timeline.ps1`: reproducible end-to-end build.
- `tools/render_occlusion_diagnostic.py`: blocker and ray diagnostics.

The vision engine is based on `devilesk/dota-vision-simulation`, with local
changes for current map data and external tree collision raycasts.
