# Native FoW 视野计算方案

本文档记录当前正式使用的 Dota 2 7.41 ward 视野计算方法。旧的
shadowcasting、树圆 LOS raycast 和眼位吸附方案不再作为主链路文档维护。

## 目标

项目当前针对比赛 `8831926213` 生成按秒 ward 视野时间线：

1. 读取回放/StarRocks 导出的真假眼生命周期。
2. 使用 Valve `cache.fow` 和 native FoW tile-byte 网格计算真眼遮挡。
3. 把每秒可见 native grid cells 合并回 timeline。
4. 投影到 7.41 地图底图，输出 `index.html` 可视化页面。

端到端入口：

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_8831926213_timeline.ps1
```

## 输入资源

- `resources/matches/8831926213/ward_timeline_source.json`
  - 比赛眼位生命周期，包含 `type`、`team`、`start`、`end`、`x/y`、`ehandle`。
- `resources/native-fow/cache.fow`
  - Valve 的 701 x 701 相对格角度遮挡查找表。
- `resources/native-fow/dota_static_fow_grid.json`
  - 296 x 296 native FoW 网格。
  - 每格 64 世界单位。
  - 世界范围 `[-9472, -9472]` 到 `[9472, 9472]`。
- `resources/trees/static_trees_full_from_vents.json`
  - 静态树坐标，用于把 tree id 解析为 native FoW cell。
- `resources/calibration/projection_741_aerial_14pt.json`
  - 世界坐标到 7.41 地图像素的 14 点仿射校准。
- `resources/maps/7.41_map.png`
  - 7.41 鸟瞰地图底图。

native FoW 网格的 tile byte 语义：

```text
bits 0..4  height level
bit 5      0x20 height/cliff edge
bit 6      0x40 explicit FoW blocker
bit 7      0x80 tree
```

当前静态网格包含：

- 2306 个 tree cells。
- 393 个 FoW blocker nodes，对应 376 个唯一 cells。
- 5326 个 height-edge cells。
- 高度阈值 `[96, 224, 352, 480]`。

## 坐标系统

眼位输入使用 parser 坐标，计算前统一转换为 Dota 世界坐标：

```text
world_x = parser_x * 128 - 16384
world_y = parser_y * 128 - 16384
```

native FoW 计算使用 64 世界单位一个 cell：

```text
cell_x = floor((world_x - world_min_x) / 64)
cell_y = floor((world_y - world_min_y) / 64)
```

渲染阶段从 native cell 还原 cell center，再通过仿射校准投影到地图像素。当前
renderer 使用 `cellCenterOffset = 0.5`，因为 FoW 输出保存的是 cell index。

## 真眼计算

真眼 `obs` 使用 `tools/compute_ward_occlusion_native.py` 计算，默认视野半径
为 1600 世界单位。

每个真眼的计算流程：

1. 把 parser 坐标转换为世界坐标。
2. 用世界坐标定位 viewer 所在 native FoW cell。
3. 从 viewer tile byte 读取 viewer height。
4. 在半径范围内扫描可能参与遮挡的 tile。
5. 根据 tile byte 和 viewer height 判断遮挡类型：
   - 活树 `0x80` 通常作为 `tree` blocker。
   - 显式 FoW blocker `0x40` 作为 `occluder`。
   - 高低坡/悬崖边 `0x20` 按高度关系作为 `occluder`。
6. 对每个 blocker 从 `cache.fow` 获取相对 cell 的角度遮挡区间。
7. 遍历 1600 半径内所有目标 cell。
8. 如果目标 cell 的角度被更近 blocker 区间覆盖，则该 cell 不可见。
9. 未被遮挡的目标 cell 输出到 `cells`。

核心实现文件：

- `tools/native_fow.py`
  - `CacheFow` 读取 `cache.fow`。
  - `VisibilityGrid` 管理 native FoW tile-byte 网格。
  - `visible_cells()` 返回可见 cells 和统计信息。
- `tools/compute_ward_occlusion_native.py`
  - 管理眼位生命周期。
  - 按秒应用动态树事件。
  - 生成 `ward_occlusion_cells.json`。

## 假眼计算

假眼 `sen` 不参与 FoW 遮挡计算。渲染时按 1000 世界单位画无遮挡真视范围：

```text
sentry radius = 1000
occlusion = none
```

## 动态树状态

动态树事件可以从 JSON 或 StarRocks/MySQL 读取：

```powershell
$env:DOTA_TREE_EVENTS_SQL = "SELECT time, event_type, world_x, world_y FROM dota2_stats.tree_events WHERE match_id=%s ORDER BY time"
```

支持的 tree 定位方式：

- native grid 坐标：`grid_x/grid_y`、`cell_x/cell_y`、`fow_x/fow_y`。
- tree id：`tree_id/treeid/id`，需要同时提供 `--tree-points`。
- 世界坐标：`world_x/world_y`，也接受 `x/y`。
- parser 坐标：`parser_x/parser_y`。

事件动作支持：

- 砍树/死亡：`death`、`destroy`、`cut`、`kill`、`dead`。
- 重生/存活：`respawn`、`spawn`、`alive`、`grow`。
- 也可直接传 `alive`、`is_alive`、`tree_alive`。

处理规则：

1. 事件时间用 `ceil(time)` 转成生效整数秒。
2. 每秒计算活跃眼位前，先应用该秒所有树事件。
3. 树存活时设置 tile byte `0x80`。
4. 树死亡时清除 tile byte `0x80`。
5. 只有树状态版本变化后，相关真眼视野才重新计算。
6. 相同 cells 的连续秒会压缩为一个 `visionTimeline` segment。

当前未纳入 temporary revealers 或其他动态 FoW blocker。

## 输出格式

`compute_ward_occlusion_native.py` 输出：

```text
outputs/8831926213_ward_vision_native_fow/ward_occlusion_cells.json
```

每个真眼包含：

- `ehandle`
- `world`
- `originGrid`
- `viewerHeight`
- `candidateCellCount`
- `lightArea`
- `blockedByKind`
- `cells`
- `visionTimeline`

`render_ward_vision.py` 读取该文件后，把 `visionTimeline` 合并到眼位数据中，
再生成：

```text
outputs/8831926213_ward_vision_native_fow/ward_timeline.json
outputs/8831926213_ward_vision_native_fow/index.html
outputs/8831926213_ward_vision_native_fow/preview_t*.jpg
```

## 验证记录

native FoW 批量实现已与参考 `can_unit_see()` 实现做过逐 cell 对比：

| ehandle | batch cells | reference cells | differences |
| ---: | ---: | ---: | ---: |
| 12780452 | 1037 | 1037 | 0 |
| 2329196 | 487 | 487 | 0 |
| 11306896 | 1643 | 1643 | 0 |

比赛 `8831926213` 的 26 个真眼静态 FoW 统计：

- Candidate cells: 50,986
- Visible cells: 26,192
- Visible ratio: 51.37%

## 当前边界

- 主链路只对真眼使用 native FoW 遮挡。
- 假眼仍然作为无遮挡 1000 范围渲染。
- 动态树已接入 tile byte `0x80`，但依赖外部事件数据。
- temporary revealers 和其他动态 blocker 暂未接入。
