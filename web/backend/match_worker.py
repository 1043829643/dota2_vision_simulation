"""独立进程内的单场眼位计算 worker。

设计要点：
- 只依赖 pymysql + tools/compute_ward_value_metrics + tools/native_fow，
  刻意不导入 FastAPI/app.py，避免 forkserver/spawn 子进程重复初始化 Web 应用。
- 每个 worker 进程复用一次加载的 grid/cache/tree_cells 与一条 DB 连接。
- 只做纯计算并返回可序列化结果，文件缓存读写全部留在主进程，保证逻辑集中。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TOOLS = str(_PROJECT_ROOT / "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import pymysql  # noqa: E402
from native_fow import CacheFow, VisibilityGrid  # noqa: E402
import compute_ward_value_metrics as ward_value  # noqa: E402


# 进程内缓存：同一 worker 处理多场时复用，避免每场重复加载 15MB cache.fow 等资源。
_RESOURCES: dict[tuple[str, str, str], tuple] = {}
_CONN = None
_CONN_KEY: tuple | None = None


def _ensure_resources(grid_path: str, cache_path: str, tree_points: str):
    key = (grid_path, cache_path, tree_points)
    cached = _RESOURCES.get(key)
    if cached is not None:
        return cached
    grid = VisibilityGrid.load(Path(grid_path))
    cache = CacheFow.load(Path(cache_path))
    tree_cells = ward_value.load_tree_id_cells(Path(tree_points), grid)
    _RESOURCES[key] = (grid, cache, tree_cells)
    return grid, cache, tree_cells


def _ensure_conn(db_cfg: dict):
    global _CONN, _CONN_KEY
    key = (db_cfg.get("host"), db_cfg.get("port"), db_cfg.get("user"), db_cfg.get("database"))
    if _CONN is not None and _CONN_KEY == key:
        try:
            _CONN.ping(reconnect=True)
            return _CONN
        except Exception:
            try:
                _CONN.close()
            except Exception:
                pass
            _CONN = None
            _CONN_KEY = None
    _CONN = pymysql.connect(
        host=db_cfg["host"],
        port=int(db_cfg["port"]),
        user=db_cfg["user"],
        password=db_cfg["password"],
        database=db_cfg["database"],
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=180,
        cursorclass=pymysql.cursors.DictCursor,
    )
    _CONN_KEY = key
    return _CONN


def compute_match_raw(match_id: int, args_dict: dict, db_cfg: dict) -> dict:
    """计算单场眼位原始数据（含双方 instances、评分前），返回可序列化 dict。

    与主进程 load_or_compute_match_raw 的计算部分等价，但不触碰文件缓存。
    """
    grid, cache, tree_cells = _ensure_resources(
        args_dict["grid"], args_dict["cache"], args_dict["tree_points"]
    )
    conn = _ensure_conn(db_cfg)
    match_args = SimpleNamespace(
        **args_dict,
        team_side_filter=None,
        progress_callback=None,
        progress_step=10 ** 9,
    )
    with conn.cursor() as cursor:
        match = ward_value.compute_match(cursor, int(match_id), match_args, grid, cache, tree_cells)
        match_info = ward_value.load_match_info(cursor, int(match_id))
    return {
        "matchId": int(match_id),
        "matchInfo": match_info,
        "invisibility": match.get("invisibility"),
        "timeWindow": match.get("timeWindow"),
        "instances": match.get("instances") or [],
    }
