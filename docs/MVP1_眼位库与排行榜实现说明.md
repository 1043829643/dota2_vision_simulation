# MVP1 眼位库与排行榜实现说明

## 目标

MVP1 在 MVP0 的眼位实例评分基础上，补齐“可浏览的眼位库”和“全局排行榜”能力，第一批样本使用：

- `8852716636`
- `8852757973`

本版本用于验证产品方向和计算链路，不代表全量版本结论。

## 已实现范围

### 1. Ward Instance 指标

每个眼位实例会计算：

- 基础信息：`matchId`、阵营、眼类型、开始/结束时间、坐标、移除原因。
- 假眼视野价值：敌方英雄可见秒数、独立敌方英雄数、首次看到敌方英雄耗时。
- 真眼价值：真视范围内敌方隐身英雄秒数、可覆盖隐身英雄数。
- 反眼事件：被排眼、存活到期或疑似友方移除。
- 综合价值分：`valueScore`。

真眼价值评分必须依赖真眼覆盖判断，当前实现使用逐秒英雄位置与隐身状态计算。

### 2. Ward Spot 聚类

脚本按眼类型和世界坐标距离聚合点位，输出点位级指标：

- 样本数量
- 平均价值分
- 中位价值分
- 最好/最差实例
- 排眼率
- 平均存活时间
- 平均敌方可见秒数
- 平均真视收益秒数

### 3. 排行榜

当前输出包含：

- 全局最佳点位
- 全局最差点位
- 假眼最佳点位
- 假眼最差点位
- 真眼最佳点位
- 真眼最差点位
- 最容易被排点位
- 最高真视收益点位

### 4. 基础筛选

HTML 报告支持：

- Patch
- Ward Type
- Side
- Match
- Min Score
- Start From
- Start To

当前第一批样本统一标记为 `7.41`，后续接入更多版本时会按上游 match metadata 或外部 patch 映射表填充。

### 5. 地图点位展示

HTML 报告内嵌 `7.41_map.png`，并使用 `projection_741_aerial_14pt.json` 将世界坐标投影到底图像素坐标。

地图上的点：

- 蓝色：假眼点位
- 粉色：真眼点位
- 点大小：样本量
- 点透明度：价值分

点击地图点位会打开右侧点位详情。

### 6. 单个眼位详情

点位详情包含：

- 点位 ID、类型、阵营、坐标
- 样本数、平均分、排眼率、平均存活
- 最佳实例
- 该点位下的全部眼实例列表

## 运行方式

```powershell
$env:DOTA_DB_HOST='<starrocks-host>'
$env:DOTA_DB_PORT='<starrocks-port>'
$env:DOTA_DB_USER='<starrocks-user>'
$env:DOTA_DB_PASSWORD='<starrocks-password>'
$env:PYTHONUNBUFFERED='1'
python tools\compute_ward_value_metrics.py --match-id 8852716636 --match-id 8852757973 --output-dir outputs\ward_value_mvp1
```

## 输出文件

生成目录：

```text
outputs/ward_value_mvp1/
```

主要文件：

- `index.html`：可分享的 MVP1 眼位库与排行榜页面。
- `summary.json`：完整报告数据。
- `ward_instances.json`：眼位实例明细。
- `ward_spots.json`：点位聚类汇总。
- `spot_details.json`：点位详情与实例列表。
- `leaderboards.json`：排行榜数据。
- `match_debug/*.json`：单场调试数据。

## 当前验证结果

本次运行结果：

- 比赛数：2
- 眼位实例：124
- 假眼实例：47
- 真眼实例：77
- 聚类点位：113
- 所有点位均已生成底图像素坐标
- 隐身状态数据可用

## 已知边界

- 当前样本只有 2 场，排行榜只能用于链路验证，不能用于稳定结论。
- 当前 patch 字段使用本批样本的地图版本 `7.41`，后续全量化时需要接入 match metadata 或外部 patch 映射表。
- 价值分权重是 MVP 阶段的初始权重，后续应结合人工复盘和更多样本校准。
- 树木死亡/复活事件已在视野时间线方向明确需要接入；本 MVP1 评分报告当前重点是眼位价值库，树木动态遮挡不作为本报告的主链路。
