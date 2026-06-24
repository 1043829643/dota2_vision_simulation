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

To include tree events from StarRocks/MySQL, set `DOTA_TREE_EVENTS_SQL` to a
query with one `%s` placeholder for `match_id`. The query should return event
time, action/alive state, and either tree/grid/world coordinates. Example:

```powershell
$env:DOTA_TREE_EVENTS_SQL = "SELECT time, event_type, world_x, world_y FROM dota2_stats.tree_events WHERE match_id=%s ORDER BY time"
```

The native FoW step applies each death/respawn to tile bit `0x80` before
recalculating active observer wards for that game second.

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

## Ward Hero Visibility

To compute how many hero-seconds and continuous appearances a team sees through
wards, query by team tag and one or more match IDs:

```powershell
python tools\compute_ward_hero_visibility.py `
  --match-id 8831926213 `
  --team-tag KRD `
  --output outputs\ward_hero_visibility\KRD_8831926213.json
```

The metric uses `-80` to match duration by default. Enemy heroes must be inside
allied observer-ward visible cells. Invisible enemy heroes also require allied
sentry coverage in the same second. The script writes both the JSON result and
a same-name HTML report unless `--html-output` is provided.

## Query Web App

Run the local query website with the same database environment variables:

```powershell
$env:DOTA_DB_HOST = "47.86.96.51"
$env:DOTA_DB_PORT = "9030"
$env:DOTA_DB_USER = "dota2_reader"
$env:DOTA_DB_PASSWORD = "password"
python -m uvicorn web.backend.app:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`, enter a team tag, select one or more matches,
and run the ward hero visibility calculation. The browser calls the backend API;
database credentials stay on the server side. Query results are cached under
`outputs/web_cache/`; use the page's force-refresh option to recompute. The
result page includes both enemy-hero sighting details and observer ward
contribution rankings, plus a map timeline that replays active wards and visible
enemy hero points. The match browser supports opponent, patch, and league
filters, selecting the latest N matches, and exporting the current report as
JSON or CSV. Enable the compare-both-sides option to compute both teams in the
selected matches and show metric deltas. Use the start/end second inputs or
quick presets to analyze specific game windows such as pre-game, 0-10 minutes,
or 10-20 minutes. Click an enemy hero row or observer contribution row to filter
the map timeline to that hero or ward. Observer wards are tagged as high,
normal, low, or no-value based on hero-seconds, unique heroes seen, and
efficiency. High-value observers are highlighted on the map and can be isolated
with the high-value-only map option. The map timeline can draw each active
observer's current native FoW vision cells in real time. The web app also exposes
`POST /api/ward-value`, which builds a team-filtered ward spot library and
leaderboards for the selected matches, then lets the frontend jump from a spot
instance back into the corresponding match timeline second.

## Main Tools

- `tools/compute_ward_occlusion_native.py`: current native FoW observer calculation.
- `tools/compute_ward_hero_visibility.py`: ward-based enemy hero sighting metrics.
- `tools/render_ward_vision.py`: timeline HTML and preview renderer.
- `tools/build_8831926213_timeline.ps1`: reproducible end-to-end build.
- `tools/render_occlusion_diagnostic.py`: blocker and ray diagnostics.

The current vision engine uses Valve `cache.fow` angular intervals with the
native FoW tile-byte grid for height, tree, and explicit-blocker occlusion.
