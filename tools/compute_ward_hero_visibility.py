from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from pathlib import Path

import pymysql

from native_fow import CacheFow, VisibilityGrid, visible_cells


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = PROJECT_ROOT / "resources"
WORLD_UNITS_PER_PARSER_UNIT = 128.0
WORLD_PARSER_OFFSET = 16384.0
OBSERVER_RADIUS = 1600.0
SENTRY_RADIUS = 1000.0


def parser_to_world(x: float, y: float) -> tuple[float, float]:
    return (
        x * WORLD_UNITS_PER_PARSER_UNIT - WORLD_PARSER_OFFSET,
        y * WORLD_UNITS_PER_PARSER_UNIT - WORLD_PARSER_OFFSET,
    )


def project_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def connect(args):
    return pymysql.connect(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=args.db_password,
        database=args.database,
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=90,
        cursorclass=pymysql.cursors.DictCursor,
    )


def fetch_all(cursor, query: str, params=()) -> list[dict]:
    cursor.execute(query, params)
    return list(cursor.fetchall())


def fetch_one(cursor, query: str, params=()) -> dict | None:
    cursor.execute(query, params)
    return cursor.fetchone()


def normalize_ward_type(value: str) -> str:
    return str(value or "").replace("_left", "")


def side_from_slot(slot) -> str | None:
    if slot is None:
        return None
    return "radiant" if int(slot) < 5 else "dire"


def side_from_team_num(team) -> str | None:
    if team is None:
        return None
    value = int(team)
    if value == 2:
        return "radiant"
    if value == 3:
        return "dire"
    return None


def build_ward_intervals(rows: list[dict]) -> list[dict]:
    by_handle: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        by_handle[int(row["ehandle"])].append(row)

    intervals = []
    for ehandle, events in by_handle.items():
        events.sort(key=lambda row: (int(row["time"]), int(row["log_index"])))
        placements = [
            row
            for row in events
            if str(row.get("entityleft")).lower() == "false"
        ]
        lefts = [
            row
            for row in events
            if str(row.get("entityleft")).lower() == "true"
        ]
        if not placements:
            continue
        place = placements[0]
        ward_type = normalize_ward_type(place.get("type"))
        left = next(
            (
                row
                for row in lefts
                if normalize_ward_type(row.get("type")) == ward_type
            ),
            None,
        )
        intervals.append(
            {
                "ehandle": ehandle,
                "type": ward_type,
                "team": side_from_slot(place.get("slot")),
                "slot": None if place.get("slot") is None else int(place["slot"]),
                "start": int(place["time"]),
                "end": int(left["time"]) if left else None,
                "x": float(place["x"]),
                "y": float(place["y"]),
                "z": None if place.get("z") is None else float(place["z"]),
            }
        )
    intervals.sort(key=lambda ward: (ward["start"], ward["team"] or "", ward["type"], ward["ehandle"]))
    return intervals


def active_wards(wards: list[dict], second: int, team: str, ward_type: str) -> list[dict]:
    return [
        ward
        for ward in wards
        if ward["team"] == team
        and ward["type"] == ward_type
        and ward["start"] <= second
        and (ward["end"] is None or second < ward["end"])
    ]


def load_occlusion_cells(path: Path | None) -> dict[int, dict]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {int(row["ehandle"]): row for row in payload.get("results", [])}


def cells_for_occlusion_result(result: dict, second: int) -> set[tuple[int, int]]:
    for segment in result.get("visionTimeline", []):
        if int(segment["start"]) <= second < int(segment["end"]):
            return {tuple(cell) for cell in segment.get("cells", [])}
    return {tuple(cell) for cell in result.get("cells", [])}


def build_static_observer_cells(
    wards: list[dict],
    grid: VisibilityGrid,
    cache: CacheFow,
) -> dict[int, set[tuple[int, int]]]:
    result = {}
    for ward in wards:
        if ward["type"] != "obs":
            continue
        world_x, world_y = parser_to_world(float(ward["x"]), float(ward["y"]))
        cells, _stats = visible_cells(
            grid,
            cache,
            world_x,
            world_y,
            OBSERVER_RADIUS,
        )
        result[int(ward["ehandle"])] = {tuple(cell) for cell in cells}
    return result


def observer_visible_cells_at(
    wards: list[dict],
    second: int,
    team: str,
    occlusion_by_handle: dict[int, dict],
    static_cells_by_handle: dict[int, set[tuple[int, int]]],
) -> set[tuple[int, int]]:
    cells: set[tuple[int, int]] = set()
    for ward in active_wards(wards, second, team, "obs"):
        ehandle = int(ward["ehandle"])
        if ehandle in occlusion_by_handle:
            cells.update(cells_for_occlusion_result(occlusion_by_handle[ehandle], second))
        else:
            cells.update(static_cells_by_handle.get(ehandle, set()))
    return cells


def observer_cell_sets_at(
    wards: list[dict],
    second: int,
    team: str,
    occlusion_by_handle: dict[int, dict],
    static_cells_by_handle: dict[int, set[tuple[int, int]]],
) -> list[tuple[dict, set[tuple[int, int]]]]:
    result = []
    for ward in active_wards(wards, second, team, "obs"):
        ehandle = int(ward["ehandle"])
        if ehandle in occlusion_by_handle:
            cells = cells_for_occlusion_result(occlusion_by_handle[ehandle], second)
        else:
            cells = static_cells_by_handle.get(ehandle, set())
        if cells:
            result.append((ward, cells))
    return result


def map_vision_cells_for_ward(
    ward: dict,
    occlusion_by_handle: dict[int, dict],
    static_cells_by_handle: dict[int, set[tuple[int, int]]],
) -> dict:
    ehandle = int(ward["ehandle"])
    if ehandle in occlusion_by_handle:
        result = occlusion_by_handle[ehandle]
        return {
            "visionCells": result.get("cells", []),
            "visionTimeline": result.get("visionTimeline", []),
        }
    return {
        "visionCells": [list(cell) for cell in sorted(static_cells_by_handle.get(ehandle, set()))],
        "visionTimeline": [],
    }


def sentry_covers_position(sentries: list[dict], world_x: float, world_y: float) -> bool:
    radius_sq = SENTRY_RADIUS * SENTRY_RADIUS
    for sentry in sentries:
        sentry_world_x, sentry_world_y = parser_to_world(float(sentry["x"]), float(sentry["y"]))
        dx = world_x - sentry_world_x
        dy = world_y - sentry_world_y
        if dx * dx + dy * dy <= radius_sq:
            return True
    return False


def load_match_duration(cursor, match_id: int, overview_database: str) -> int:
    row = fetch_one(
        cursor,
        f"SELECT duration FROM `{overview_database}`.`dwd_match_overview` WHERE match_id=%s",
        (match_id,),
    )
    if row and row.get("duration") is not None:
        return int(row["duration"])
    row = fetch_one(
        cursor,
        "SELECT MAX(time) AS max_time FROM player_intervals2 WHERE match_id=%s",
        (str(match_id),),
    )
    if row and row.get("max_time") is not None:
        return int(row["max_time"])
    raise ValueError(f"cannot determine duration for match {match_id}")


def load_match_info(cursor, match_id: int) -> dict:
    row = fetch_one(
        cursor,
        "SELECT match_id,radiant_team_tag,dire_team_tag FROM match_info WHERE match_id=%s",
        (str(match_id),),
    )
    if not row:
        raise ValueError(f"match_info not found for match {match_id}")
    return row


def resolve_team_side(match_info: dict, team_tag: str) -> str:
    wanted = team_tag.strip().lower()
    radiant = str(match_info.get("radiant_team_tag") or "").strip().lower()
    dire = str(match_info.get("dire_team_tag") or "").strip().lower()
    if wanted == radiant:
        return "radiant"
    if wanted == dire:
        return "dire"
    raise ValueError(
        f"team tag {team_tag!r} is not in match {match_info['match_id']} "
        f"({match_info.get('radiant_team_tag')} vs {match_info.get('dire_team_tag')})"
    )


def load_players(cursor, match_id: int) -> tuple[dict[int, dict], dict[str, int]]:
    rows = fetch_all(
        cursor,
        """
SELECT slot,steamid,hero_name,hero_id,persona,team
FROM players
WHERE match_id=%s
ORDER BY slot
""",
        (str(match_id),),
    )
    players = {}
    hero_to_slot = {}
    for row in rows:
        slot = int(row["slot"])
        side = side_from_team_num(row.get("team")) or side_from_slot(slot)
        item = {
            "slot": slot,
            "steamid": None if row.get("steamid") is None else int(row["steamid"]),
            "heroName": row.get("hero_name"),
            "heroId": row.get("hero_id"),
            "persona": row.get("persona"),
            "team": side,
        }
        players[slot] = item
        if row.get("hero_name"):
            hero_to_slot[str(row["hero_name"]).lower()] = slot
    if len(players) != 10:
        raise ValueError(f"expected 10 players for match {match_id}, got {len(players)}")
    return players, hero_to_slot


def load_positions(cursor, match_id: int, start: int, end: int) -> dict[int, list[dict]]:
    rows = fetch_all(
        cursor,
        """
WITH latest_hp AS (
  SELECT h.match_id,h.time,h.slot,h.hp,h.log_index
  FROM hero_status_update h
  JOIN (
    SELECT match_id,time,slot,MAX(log_index) AS max_log
    FROM hero_status_update
    WHERE match_id=%s AND time BETWEEN %s AND %s
    GROUP BY match_id,time,slot
  ) m
    ON h.match_id=m.match_id
   AND h.time=m.time
   AND h.slot=m.slot
   AND h.log_index=m.max_log
)
SELECT pi.time,pi.slot,pi.unit,pi.x,pi.y,hp.hp
FROM player_intervals2 pi
JOIN latest_hp hp
  ON pi.match_id=hp.match_id
 AND pi.time=hp.time
 AND pi.slot=hp.slot
WHERE pi.match_id=%s
  AND pi.time BETWEEN %s AND %s
  AND pi.x <> ''
  AND pi.y <> ''
  AND CAST(hp.hp AS SIGNED) > 0
ORDER BY pi.time,pi.slot
""",
        (str(match_id), start, end, str(match_id), start, end),
    )
    by_second: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        by_second[int(row["time"])].append(
            {
                "slot": int(row["slot"]),
                "unit": row.get("unit"),
                "x": float(row["x"]),
                "y": float(row["y"]),
                "hp": int(float(row["hp"])),
            }
        )
    return by_second


def load_wards(cursor, match_id: int) -> list[dict]:
    rows = fetch_all(
        cursor,
        """
SELECT time,slot,type,attackername,x,y,z,entityleft,ehandle,log_index
FROM ward_placed_left_fact
WHERE match_id=%s
ORDER BY time,log_index
""",
        (str(match_id),),
    )
    return build_ward_intervals(rows)


def load_invisible_seconds(
    cursor,
    match_id: int,
    start: int,
    end: int,
    hero_to_slot: dict[str, int],
) -> dict[int, set[int]]:
    rows = fetch_all(
        cursor,
        """
SELECT time,log_index,type,targetname,inflictor
FROM combat_logs
WHERE match_id=%s
  AND invisibility_modifier='true'
  AND type IN ('DOTA_COMBATLOG_MODIFIER_ADD','DOTA_COMBATLOG_MODIFIER_REMOVE')
  AND time <= %s
ORDER BY time,log_index
""",
        (str(match_id), end),
    )
    active: dict[tuple[int, str], int] = {}
    intervals: dict[int, list[tuple[int, int]]] = defaultdict(list)

    for row in rows:
        target = str(row.get("targetname") or "").lower()
        slot = hero_to_slot.get(target)
        if slot is None:
            continue
        key = (slot, str(row.get("inflictor") or ""))
        second = int(row["time"])
        if row["type"] == "DOTA_COMBATLOG_MODIFIER_ADD":
            active[key] = second
            continue
        added_at = active.pop(key, None)
        if added_at is not None:
            intervals[slot].append((added_at, second))

    for (slot, _inflictor), added_at in active.items():
        intervals[slot].append((added_at, end + 1))

    invisible_seconds: dict[int, set[int]] = defaultdict(set)
    for slot, ranges in intervals.items():
        for range_start, range_end in ranges:
            clamped_start = max(start, range_start)
            clamped_end = min(end + 1, range_end)
            if clamped_start < clamped_end:
                invisible_seconds[slot].update(range(clamped_start, clamped_end))
    return invisible_seconds


def append_visible_second(hero_state: dict, second: int, invisible: bool) -> None:
    hero_state["visibleSeconds"] += 1
    if invisible:
        hero_state["invisibleVisibleSeconds"] += 1

    segments = hero_state["segments"]
    if segments and segments[-1]["end"] == second and segments[-1]["invisible"] == invisible:
        segments[-1]["end"] = second + 1
        segments[-1]["duration"] += 1
        return
    segments.append(
        {
            "start": second,
            "end": second + 1,
            "duration": 1,
            "invisible": invisible,
        }
    )


def append_ward_visible_second(
    ward_state: dict,
    hero: dict,
    second: int,
    invisible: bool,
) -> None:
    ward_state["heroSeconds"] += 1
    if invisible:
        ward_state["invisibleHeroSeconds"] += 1

    slot = int(hero["slot"])
    hero_state = ward_state["heroes"].setdefault(
        slot,
        {
            "slot": slot,
            "heroName": hero.get("heroName"),
            "persona": hero.get("persona"),
            "visibleSeconds": 0,
            "invisibleVisibleSeconds": 0,
            "appearances": 0,
            "segments": [],
        },
    )
    hero_state["visibleSeconds"] += 1
    if invisible:
        hero_state["invisibleVisibleSeconds"] += 1

    segments = hero_state["segments"]
    if segments and segments[-1]["end"] == second and segments[-1]["invisible"] == invisible:
        segments[-1]["end"] = second + 1
        segments[-1]["duration"] += 1
        return
    segments.append(
        {
            "start": second,
            "end": second + 1,
            "duration": 1,
            "invisible": invisible,
        }
    )


def merge_appearance_segments(segments: list[dict]) -> int:
    if not segments:
        return 0
    appearances = 1
    previous_end = segments[0]["end"]
    for segment in segments[1:]:
        if segment["start"] != previous_end:
            appearances += 1
        previous_end = segment["end"]
    return appearances


def classify_observer_value(item: dict) -> tuple[str, str]:
    if item["heroSeconds"] == 0:
        return "none", "0 hero-seconds"
    if item["heroSeconds"] >= 60 or item["uniqueHeroes"] >= 3:
        return "high", "hero-seconds >= 60 or unique heroes >= 3"
    if item["heroSeconds"] < 10 and item["efficiency"] < 0.05:
        return "low", "hero-seconds < 10 and efficiency < 5%"
    return "normal", "default"


def write_html_report(path: Path, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Ward Hero Visibility</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0f141b;
      --panel: #161d27;
      --panel-2: #1d2633;
      --line: #2d3948;
      --text: #eef4fb;
      --muted: #9fafc1;
      --green: #42d276;
      --red: #ee5252;
      --blue: #50b4ff;
      --gold: #f6c35b;
      font-family: Arial, "Microsoft YaHei", sans-serif;
    }}
    body {{
      margin: 0;
      background: radial-gradient(circle at top, #17202b 0, var(--bg) 42rem);
      color: var(--text);
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 28px 20px 48px;
    }}
    h1, h2, h3 {{ margin: 0; }}
    .subtitle {{ color: var(--muted); margin-top: 8px; }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin: 22px 0;
    }}
    .card {{
      background: rgba(22, 29, 39, 0.92);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 16px;
      box-shadow: 0 12px 28px rgba(0, 0, 0, 0.22);
    }}
    .label {{ color: var(--muted); font-size: 13px; }}
    .value {{ font-size: 30px; font-weight: 700; margin-top: 6px; }}
    .match {{
      background: rgba(22, 29, 39, 0.9);
      border: 1px solid var(--line);
      border-radius: 16px;
      margin-top: 18px;
      overflow: hidden;
    }}
    .match-head {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      padding: 18px;
      background: var(--panel-2);
      border-bottom: 1px solid var(--line);
    }}
    .pills {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
    .pill {{
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      padding: 5px 10px;
      font-size: 12px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      background: rgba(255, 255, 255, 0.02);
    }}
    tr:last-child td {{ border-bottom: 0; }}
    .hero-name {{ font-weight: 700; }}
    .muted {{ color: var(--muted); }}
    .bar {{
      height: 9px;
      border-radius: 999px;
      background: #263242;
      overflow: hidden;
      margin-top: 7px;
    }}
    .bar-fill {{
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--green), var(--blue));
    }}
    details summary {{
      color: var(--blue);
      cursor: pointer;
      user-select: none;
    }}
    .segments {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 9px;
      max-width: 440px;
    }}
    .segment {{
      background: #243044;
      border: 1px solid #34445a;
      border-radius: 8px;
      padding: 4px 7px;
      font-size: 12px;
      color: #dce8f5;
    }}
    .segment.invisible {{
      border-color: rgba(246, 195, 91, 0.8);
      color: var(--gold);
    }}
    .rules {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.7;
      margin-top: 22px;
    }}
    @media (max-width: 820px) {{
      .cards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .match-head {{ display: block; }}
      table {{ font-size: 13px; }}
      th, td {{ padding: 10px 8px; }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>Ward Hero Visibility</h1>
    <div class="subtitle">Observer 视野命中敌方英雄；隐身英雄需要同时处于己方 Sentry 范围。</div>
    <section class="cards" id="totals"></section>
    <section id="matches"></section>
    <section class="rules" id="rules"></section>
  </main>
  <script>
    const report = {data};

    function fmtTime(value) {{
      const sign = value < 0 ? "-" : "";
      value = Math.abs(value);
      return `${{sign}}${{Math.floor(value / 60)}}:${{String(value % 60).padStart(2, "0")}}`;
    }}

    function cleanHeroName(name) {{
      return String(name || "")
        .replace("npc_dota_hero_", "")
        .split("_")
        .map(part => part ? part[0].toUpperCase() + part.slice(1) : part)
        .join(" ");
    }}

    function card(label, value) {{
      return `<div class="card"><div class="label">${{label}}</div><div class="value">${{value}}</div></div>`;
    }}

    function renderTotals() {{
      const totals = report.totals || {{}};
      document.getElementById("totals").innerHTML = [
        card("Hero-seconds", totals.heroSeconds || 0),
        card("Unique seconds", totals.uniqueSeconds || 0),
        card("Appearances", totals.appearances || 0),
        card("Invisible hero-seconds", totals.invisibleHeroSeconds || 0),
      ].join("");
    }}

    function renderSegments(hero) {{
      if (!hero.segments || !hero.segments.length) return '<span class="muted">无可见区间</span>';
      const segments = hero.segments.map(segment => {{
        const cls = segment.invisible ? "segment invisible" : "segment";
        const suffix = segment.invisible ? " 隐身" : "";
        return `<span class="${{cls}}">${{fmtTime(segment.start)}}-${{fmtTime(segment.end - 1)}} · ${{segment.duration}}s${{suffix}}</span>`;
      }}).join("");
      return `<details><summary>查看 ${{hero.segments.length}} 个区间</summary><div class="segments">${{segments}}</div></details>`;
    }}

    function renderMatch(match) {{
      const heroes = [...(match.enemyHeroes || [])].sort((a, b) => b.visibleSeconds - a.visibleSeconds);
      const maxSeconds = Math.max(1, ...heroes.map(hero => hero.visibleSeconds || 0));
      const rows = heroes.map(hero => {{
        const pct = Math.round((hero.visibleSeconds || 0) / maxSeconds * 100);
        return `<tr>
          <td>
            <div class="hero-name">${{cleanHeroName(hero.heroName)}}</div>
            <div class="muted">${{hero.persona || ""}} · slot ${{hero.slot}}</div>
          </td>
          <td>
            <strong>${{hero.visibleSeconds}}</strong>
            <div class="bar"><div class="bar-fill" style="width:${{pct}}%"></div></div>
          </td>
          <td>${{hero.appearances}}</td>
          <td>${{hero.invisibleVisibleSeconds}}</td>
          <td>${{renderSegments(hero)}}</td>
        </tr>`;
      }}).join("");

      return `<article class="match">
        <div class="match-head">
          <div>
            <h2>${{match.teamTag}} vs ${{match.enemyTeamTag}}</h2>
            <div class="subtitle">Match ${{match.matchId}} · ${{match.teamSide}} 观察 ${{match.enemySide}}</div>
            <div class="pills">
              <span class="pill">时间窗 ${{fmtTime(match.timeWindow.start)}}-${{fmtTime(match.timeWindow.end)}}</span>
              <span class="pill">Observer ${{match.wardCounts.observer}}</span>
              <span class="pill">Sentry ${{match.wardCounts.sentry}}</span>
            </div>
          </div>
          <div class="pills">
            <span class="pill">Hero-seconds ${{match.metrics.heroSeconds}}</span>
            <span class="pill">Unique seconds ${{match.metrics.uniqueSeconds}}</span>
            <span class="pill">Appearances ${{match.metrics.appearances}}</span>
            <span class="pill">Invisible ${{match.metrics.invisibleHeroSeconds}}</span>
          </div>
        </div>
        <table>
          <thead>
            <tr>
              <th>敌方英雄</th>
              <th>可见秒数</th>
              <th>出现次数</th>
              <th>隐身可见秒数</th>
              <th>可见区间</th>
            </tr>
          </thead>
          <tbody>${{rows}}</tbody>
        </table>
      </article>`;
    }}

    function renderRules() {{
      const rules = (report.source && report.source.rules) || {{}};
      document.getElementById("rules").innerHTML = `
        <strong>统计口径</strong><br />
        秒数指标：${{rules.secondsMetric || ""}}；出现次数：连续可见区间计数，中断 1 秒算新出现。<br />
        存活过滤：${{rules.aliveFilter || ""}}。<br />
        普通英雄：${{rules.normalHeroVision || ""}}。<br />
        隐身英雄：${{rules.invisibleHeroVision || ""}}。`;
    }}

    renderTotals();
    document.getElementById("matches").innerHTML = (report.matches || []).map(renderMatch).join("");
    renderRules();
  </script>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def compute_match(
    cursor,
    match_id: int,
    team_tag: str,
    args,
    grid: VisibilityGrid,
    cache: CacheFow,
) -> dict:
    match_info = load_match_info(cursor, match_id)
    duration = load_match_duration(cursor, match_id, args.overview_database)
    start = args.start if args.start is not None else -80
    end = args.end if args.end is not None else duration
    team_side = resolve_team_side(match_info, team_tag)
    enemy_side = "dire" if team_side == "radiant" else "radiant"
    enemy_tag = (
        match_info.get("dire_team_tag")
        if team_side == "radiant"
        else match_info.get("radiant_team_tag")
    )

    players, hero_to_slot = load_players(cursor, match_id)
    positions_by_second = load_positions(cursor, match_id, start, end)
    wards = load_wards(cursor, match_id)
    invisible_seconds = load_invisible_seconds(
        cursor,
        match_id,
        start,
        end,
        hero_to_slot,
    )

    occlusion_by_handle = load_occlusion_cells(
        Path(args.occlusion_cells) if args.occlusion_cells else None
    )
    static_cells_by_handle = build_static_observer_cells(wards, grid, cache)

    enemy_slots = [
        slot
        for slot, player in players.items()
        if player["team"] == enemy_side
    ]
    hero_stats = {
        slot: {
            **players[slot],
            "visibleSeconds": 0,
            "invisibleVisibleSeconds": 0,
            "appearances": 0,
            "segments": [],
        }
        for slot in enemy_slots
    }
    observer_wards = [
        ward
        for ward in wards
        if ward["team"] == team_side and ward["type"] == "obs"
    ]
    observer_stats = {
        int(ward["ehandle"]): {
            "ehandle": int(ward["ehandle"]),
            "team": ward["team"],
            "slot": ward["slot"],
            "start": ward["start"],
            "end": ward["end"],
            "x": ward["x"],
            "y": ward["y"],
            "heroSeconds": 0,
            "invisibleHeroSeconds": 0,
            "appearances": 0,
            "uniqueHeroes": 0,
            "lifetimeSeconds": max(
                0,
                min(ward["end"] if ward["end"] is not None else end + 1, end + 1)
                - max(ward["start"], start),
            ),
            "efficiency": 0.0,
            "heroes": {},
        }
        for ward in observer_wards
    }
    map_sightings = []
    unique_visible_seconds = set()

    for second in range(start, end + 1):
        observer_cell_sets = observer_cell_sets_at(
            wards,
            second,
            team_side,
            occlusion_by_handle,
            static_cells_by_handle,
        )
        if not observer_cell_sets:
            continue
        sentries = active_wards(wards, second, team_side, "sen")

        for position in positions_by_second.get(second, []):
            slot = int(position["slot"])
            if slot not in hero_stats:
                continue
            world_x, world_y = parser_to_world(position["x"], position["y"])
            hero_cell = grid.world_to_cell(world_x, world_y)
            seeing_observers = [
                ward
                for ward, cells in observer_cell_sets
                if hero_cell in cells
            ]
            if not seeing_observers:
                continue

            invisible = second in invisible_seconds.get(slot, set())
            if invisible and not sentry_covers_position(sentries, world_x, world_y):
                continue

            append_visible_second(hero_stats[slot], second, invisible)
            unique_visible_seconds.add(second)
            map_sightings.append(
                {
                    "time": second,
                    "slot": slot,
                    "heroName": hero_stats[slot].get("heroName"),
                    "persona": hero_stats[slot].get("persona"),
                    "x": position["x"],
                    "y": position["y"],
                    "worldX": world_x,
                    "worldY": world_y,
                    "invisible": invisible,
                    "observerEhandles": [
                        int(ward["ehandle"]) for ward in seeing_observers
                    ],
                }
            )
            for ward in seeing_observers:
                append_ward_visible_second(
                    observer_stats[int(ward["ehandle"])],
                    hero_stats[slot],
                    second,
                    invisible,
                )

    total_appearances = 0
    for item in hero_stats.values():
        item["appearances"] = merge_appearance_segments(item["segments"])
        total_appearances += item["appearances"]

    observer_contributions = []
    observer_value_summary = {"high": 0, "normal": 0, "low": 0, "none": 0}
    for item in observer_stats.values():
        hero_details = []
        appearances = 0
        for hero_item in item["heroes"].values():
            hero_item["appearances"] = merge_appearance_segments(hero_item["segments"])
            appearances += hero_item["appearances"]
            hero_details.append(hero_item)
        item["appearances"] = appearances
        item["uniqueHeroes"] = len(hero_details)
        item["efficiency"] = (
            item["heroSeconds"] / item["lifetimeSeconds"]
            if item["lifetimeSeconds"]
            else 0.0
        )
        item["heroes"] = sorted(
            hero_details,
            key=lambda hero: (-hero["visibleSeconds"], hero["slot"]),
        )
        value_tier, value_reason = classify_observer_value(item)
        item["valueTier"] = value_tier
        item["valueReason"] = value_reason
        observer_value_summary[value_tier] += 1
        observer_contributions.append(item)
    observer_contributions.sort(
        key=lambda ward: (-ward["heroSeconds"], -ward["appearances"], ward["start"], ward["ehandle"])
    )
    hero_ward_contributors: dict[int, list[dict]] = defaultdict(list)
    for ward in observer_contributions:
        for hero_item in ward["heroes"]:
            hero_ward_contributors[int(hero_item["slot"])].append(
                {
                    "ehandle": ward["ehandle"],
                    "slot": ward["slot"],
                    "start": ward["start"],
                    "end": ward["end"],
                    "x": ward["x"],
                    "y": ward["y"],
                    "visibleSeconds": hero_item["visibleSeconds"],
                    "invisibleVisibleSeconds": hero_item["invisibleVisibleSeconds"],
                    "appearances": hero_item["appearances"],
                    "segments": hero_item["segments"],
                }
            )
    for slot, item in hero_stats.items():
        item["wardContributors"] = sorted(
            hero_ward_contributors.get(slot, []),
            key=lambda ward: (-ward["visibleSeconds"], -ward["appearances"], ward["start"], ward["ehandle"]),
        )
    observer_by_handle = {
        int(ward["ehandle"]): ward for ward in observer_contributions
    }
    map_wards = []
    for ward in wards:
        if ward["team"] != team_side or ward["type"] not in ("obs", "sen"):
            continue
        observer_meta = observer_by_handle.get(int(ward["ehandle"]), {})
        item = {
            "ehandle": int(ward["ehandle"]),
            "type": ward["type"],
            "team": ward["team"],
            "slot": ward["slot"],
            "start": ward["start"],
            "end": ward["end"],
            "x": ward["x"],
            "y": ward["y"],
            "valueTier": (
                observer_meta.get("valueTier") if ward["type"] == "obs" else None
            ),
            "valueReason": (
                observer_meta.get("valueReason") if ward["type"] == "obs" else None
            ),
            "heroSeconds": (
                observer_meta.get("heroSeconds", 0) if ward["type"] == "obs" else 0
            ),
            "uniqueHeroes": (
                observer_meta.get("uniqueHeroes", 0) if ward["type"] == "obs" else 0
            ),
        }
        if ward["type"] == "obs":
            item.update(
                map_vision_cells_for_ward(
                    ward,
                    occlusion_by_handle,
                    static_cells_by_handle,
                )
            )
        map_wards.append(item)

    observer_count = sum(1 for ward in wards if ward["team"] == team_side and ward["type"] == "obs")
    sentry_count = sum(1 for ward in wards if ward["team"] == team_side and ward["type"] == "sen")
    return {
        "matchId": match_id,
        "teamTag": team_tag,
        "teamSide": team_side,
        "enemyTeamTag": enemy_tag,
        "enemySide": enemy_side,
        "timeWindow": {
            "start": start,
            "end": end,
            "durationSource": "dwd_match_overview.duration",
        },
        "wardCounts": {
            "observer": observer_count,
            "sentry": sentry_count,
        },
        "observerValueSummary": observer_value_summary,
        "metrics": {
            "heroSeconds": sum(item["visibleSeconds"] for item in hero_stats.values()),
            "uniqueSeconds": len(unique_visible_seconds),
            "appearances": total_appearances,
            "invisibleHeroSeconds": sum(item["invisibleVisibleSeconds"] for item in hero_stats.values()),
            "observerContributionHeroSeconds": sum(
                item["heroSeconds"] for item in observer_contributions
            ),
        },
        "enemyHeroes": [hero_stats[slot] for slot in sorted(hero_stats)],
        "observerContributions": observer_contributions,
        "map": {
            "wards": map_wards,
            "sightings": map_sightings,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute enemy hero sightings through observer/sentry ward vision."
    )
    parser.add_argument("--match-id", type=int, action="append", required=True)
    parser.add_argument("--team-tag", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--html-output",
        help="Optional HTML report path. Defaults to the JSON output path with .html suffix.",
    )
    parser.add_argument("--database", default=os.environ.get("DOTA_DB_DATABASE", "dota2_analysis"))
    parser.add_argument("--overview-database", default=os.environ.get("DOTA_OVERVIEW_DATABASE", "dwd_dota2"))
    parser.add_argument("--db-host", default=os.environ.get("DOTA_DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.environ.get("DOTA_DB_PORT", "9030")))
    parser.add_argument("--db-user", default=os.environ.get("DOTA_DB_USER", ""))
    parser.add_argument("--db-password", default=os.environ.get("DOTA_DB_PASSWORD", os.environ.get("DB_PASS", "")))
    parser.add_argument("--grid", default=str(RESOURCE_ROOT / "native-fow" / "dota_static_fow_grid.json"))
    parser.add_argument("--cache", default=str(RESOURCE_ROOT / "native-fow" / "cache.fow"))
    parser.add_argument(
        "--occlusion-cells",
        help="Optional ward_occlusion_cells.json for a single match. If omitted, static native FoW is computed.",
    )
    parser.add_argument("--start", type=int, help="Override start second. Default: -80.")
    parser.add_argument("--end", type=int, help="Override end second. Default: match duration.")
    args = parser.parse_args()

    if args.occlusion_cells and len(args.match_id) != 1:
        raise ValueError("--occlusion-cells can only be used with one --match-id")

    grid = VisibilityGrid.load(args.grid)
    cache = CacheFow.load(args.cache)

    with connect(args) as conn:
        with conn.cursor() as cursor:
            matches = [
                compute_match(cursor, match_id, args.team_tag, args, grid, cache)
                for match_id in args.match_id
            ]

    output = {
        "source": {
            "database": args.database,
            "overviewDatabase": args.overview_database,
            "grid": project_path(Path(args.grid)),
            "cache": project_path(Path(args.cache)),
            "occlusionCells": None
            if not args.occlusion_cells
            else project_path(Path(args.occlusion_cells)),
            "rules": {
                "secondsMetric": "hero-seconds",
                "appearanceCount": "continuous visible segment per enemy hero; a one-second gap starts a new appearance",
                "aliveFilter": "latest hero_status_update.hp > 0 per match/time/slot",
                "normalHeroVision": "enemy hero native FoW cell is inside allied observer ward visible cells",
                "invisibleHeroVision": "normalHeroVision plus allied sentry 1000-unit radius",
                "invisibilityEvents": "combat_logs ADD is inclusive, REMOVE is exclusive",
                "timeWindowDefault": "-80 to match duration inclusive",
            },
        },
        "matches": matches,
        "totals": {
            "heroSeconds": sum(match["metrics"]["heroSeconds"] for match in matches),
            "uniqueSeconds": sum(match["metrics"]["uniqueSeconds"] for match in matches),
            "appearances": sum(match["metrics"]["appearances"] for match in matches),
            "invisibleHeroSeconds": sum(match["metrics"]["invisibleHeroSeconds"] for match in matches),
            "observerContributionHeroSeconds": sum(
                match["metrics"].get("observerContributionHeroSeconds", 0)
                for match in matches
            ),
        },
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path = Path(args.html_output) if args.html_output else output_path.with_suffix(".html")
    write_html_report(html_path, output)
    print(json.dumps(output["totals"], ensure_ascii=False, indent=2))
    print(f"wrote {output_path}")
    print(f"wrote {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
