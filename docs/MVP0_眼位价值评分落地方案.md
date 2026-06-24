# MVP-0：眼位价值评分落地方案

本文档是 `计划目标.md` 的工程落地版。目标不是一次性实现完整 Vision Intelligence Platform，而是先用指定比赛样本做出可验证的眼位价值评分闭环。

## 1. 本期目标

对指定 match_id 的真假眼进行离线分析，输出：

1. 每个 Ward Instance 的可解释指标。
2. 每个 Ward Instance 的价值评分。
3. Ward Spot 聚类结果。
4. Best / Worst Ward 排行榜。
5. 可打开分享的静态 HTML / JSON 报告。

本期接受本地静态产物，不做后端 API。

## 2. 第一批样本

只处理用户指定的两场比赛：

```text
8852716636
8852757973
```

后续可以扩展为任意 match_id 列表或 patch 批量样本。

## 3. 数据来源

### 3.1 已使用数据

- `dota2_stats.ward_placed_left_fact`
  - 真假眼放置、消失、坐标、阵营、ehandle。
- `dota2_stats.player_intervals2`
  - 每秒英雄位置。
- `dota2_stats.players`
  - slot、英雄、阵营、选手信息。
- `dota2_stats.dota_tree_state_change`
  - 树死亡 / 复活事件。
- `resources/native-fow/cache.fow`
  - Valve 原生 FoW 角度遮挡查表。
- `resources/native-fow/dota_static_fow_grid.json`
  - native FoW tile-byte 网格。
- `resources/native-fow/scripts/npc/npc_heroes.txt`
  - 英雄白天 / 夜晚视野。
- `resources/source/dota-map-trees.csv`
  - treeId 到静态树坐标映射。

### 3.2 需要确认或接入的数据

- `combat_logs`
  - 用于识别隐身 modifier 的 ADD / REMOVE。
  - 如果该表或字段不可用，本期需要在报告中标记 `invisibilityDataAvailable=false`，并禁止计算真眼反隐价值评分。
- 眼位是否被反
  - 优先使用 `ward_placed_left_fact` 中的消失事件和 attacker 字段。
  - 如果无法可靠区分自然过期与被反，需要输出 `removedReason=unknown`，不强行记为 deward。

## 4. 坐标与视野规则

### 4.1 坐标转换

parser 坐标统一转 world 坐标：

```text
world_x = parser_x * 128 - 16384
world_y = parser_y * 128 - 16384
```

### 4.2 Observer Ward 视野

Observer Ward 使用 native FoW 遮挡计算：

- 半径：1600 world units
- 遮挡来源：
  - 地形高度
  - height edge
  - explicit FoW blocker
  - 当前秒存活的树
- 树状态来自 `dota_tree_state_change`

### 4.3 Sentry Ward 真视

Sentry Ward 不做地形 / 树遮挡。

- 真视半径：1000 world units
- 用途：
  - 判断隐身敌方英雄是否在真眼覆盖内
  - 计算真眼反隐价值
  - 辅助判定 Observer 看到隐身英雄时是否真实可见

### 4.4 英雄可见判断

`Enemy Hero Seen Seconds` 定义如下：

某一秒内，敌方活英雄满足：

1. `player_intervals2.life_state = '0'`
2. 敌方英雄当前位置所在 native FoW cell 落入该 Observer Ward 当前秒的 `visionTimeline` cells
3. 如果该英雄处于隐身状态，则还必须落入己方当前秒任一 Sentry Ward 1000 半径真视范围

满足以上条件时，该 ward 在该秒获得 1 个 hero-second。

## 5. 隐身与真眼判断

本期必须处理隐身英雄。

### 5.1 隐身状态来源

优先从 combat log 中读取：

```text
DOTA_COMBATLOG_MODIFIER_ADD
DOTA_COMBATLOG_MODIFIER_REMOVE
invisibility_modifier = true
targetname
inflictor
time
log_index
```

处理规则：

1. ADD 秒开始视为隐身。
2. REMOVE 秒结束，REMOVE 秒不再视为隐身。
3. 多个隐身 modifier 可叠加，只有全部移除后才视为不隐身。
4. 如果隐身状态跨过统计窗口，窗口内仍然生效。

### 5.2 隐身英雄被看到的条件

隐身英雄被 Observer 理论视野覆盖不等于真实可见。

必须同时满足：

```text
Observer visible cell contains hero cell
AND allied sentry true sight covers hero position
```

否则该秒不计入 `Enemy Hero Seen Seconds`，但可以计入 `invisibleBlockedSeconds`，用于解释“理论看到但因缺真眼不可见”。

### 5.3 Sentry 反隐价值

Sentry Ward 的价值评分不看普通英雄视野遮挡，而看：

- `invisibleHeroTrueSightSeconds`
- `uniqueInvisibleHeroesCovered`
- `observerAssistedInvisibleSightings`
- `antiInvisWindowSeconds`

如果样本中没有隐身事件，Sentry 的反隐指标应显示为 0，并标注“该场无隐身机会样本”，不能因此判定为差眼。

## 6. Ward Instance 指标

每个具体眼位实例输出以下字段。

### 6.1 基础字段

```text
matchId
ehandle
wardType
team
slot
start
end
duration
parserX
parserY
worldX
worldY
originGrid
removedReason
```

### 6.2 Observer 指标

```text
enemyHeroSeenSeconds
uniqueHeroesSeen
sightingCount
firstContactCount
invisibleHeroSeenSeconds
invisibleBlockedSeconds
overlapRate
lowOverlapSeenSeconds
avgVisibleCellCount
treeDynamicApplied
```

### 6.3 Sentry 指标

```text
invisibleHeroTrueSightSeconds
uniqueInvisibleHeroesCovered
observerAssistedInvisibleSightings
antiInvisOpportunitySeconds
antiInvisEfficiency
```

### 6.4 风险指标

```text
lifetimeSeconds
dewarded
fastDewarded30
fastDewarded60
fastDewarded90
removedReasonConfidence
```

`dewarded` 只有在消失事件能被可靠判定为敌方反眼时才为 true。否则使用 `unknown`，不混入 deward rate 分母。

## 7. 价值评分

本期必须做价值评分，但评分必须可解释，不只输出一个分数。

### 7.1 Observer Ward Value Score

满分 100，建议初版公式：

```text
ObserverValueScore =
  35 * normalizedEnemyHeroSeenSeconds
+ 20 * normalizedLowOverlapSeenSeconds
+ 15 * normalizedFirstContactCount
+ 10 * normalizedUniqueHeroesSeen
+ 10 * normalizedLifetimeScore
+ 10 * survivalRiskScore
-  penaltyHighOverlap
-  penaltyFastDeward
```

解释：

- `enemyHeroSeenSeconds` 是核心信息收益。
- `lowOverlapSeenSeconds` 奖励不重复覆盖。
- `firstContactCount` 奖励首次发现敌方动向。
- `uniqueHeroesSeen` 奖励覆盖多个敌方英雄。
- `lifetimeScore` 奖励合理存活，但不允许单独决定高分。
- `survivalRiskScore` 奖励不被快速反。

### 7.2 Sentry Ward Value Score

满分 100，建议初版公式：

```text
SentryValueScore =
  40 * normalizedInvisibleHeroTrueSightSeconds
+ 25 * normalizedObserverAssistedInvisibleSightings
+ 15 * normalizedUniqueInvisibleHeroesCovered
+ 10 * normalizedAntiInvisEfficiency
+ 10 * survivalRiskScore
-  penaltyNoOpportunity
-  penaltyFastDeward
```

如果该场没有隐身机会，Sentry 的 `penaltyNoOpportunity` 不应直接把分数打成 0，而应降低置信度。

### 7.3 归一化方式

MVP-0 使用样本内分位归一化：

```text
normalizedMetric = min(value / p90(metric), 1.0)
```

要求：

- Observer 只和 Observer 比。
- Sentry 只和 Sentry 比。
- Radiant / Dire 可分开看，也可合并看，但报告必须标明。
- 样本数量过低时输出 `confidence=LOW`。

### 7.4 置信度

```text
HIGH: sampleCount >= 20
MEDIUM: sampleCount >= 8
LOW: sampleCount < 8
```

对 MVP-0 的两场比赛，spot 层面大概率多为 LOW / MEDIUM。报告必须显示样本数量，避免误导。

## 8. Ward Spot 聚类

### 8.1 聚类维度

只在以下字段完全一致时聚类：

```text
mapVersion
wardType
teamSide
```

暂不跨 Radiant / Dire 镜像合并。

### 8.2 聚类算法

MVP-0 使用 DBSCAN：

```text
eps = 200 world units
min_samples = 1
```

输出：

```text
spotId
wardType
teamSide
centerWorldX
centerWorldY
sampleCount
instanceIds
avgScore
avgSeenSeconds
avgLifetimeSeconds
dewardRate
confidence
```

### 8.3 人工标签

MVP-0 预留人工标签文件：

```text
resources/labels/ward_spot_labels.json
```

用于给聚类结果命名，例如：

```json
{
  "obs_radiant_001": {
    "name": "Radiant 三角区入口眼",
    "region": "Triangle",
    "notes": "人工校正标签"
  }
}
```

## 9. 输出物

### 9.1 JSON 输出

```text
outputs/ward_value_mvp0/ward_instances.json
outputs/ward_value_mvp0/ward_spots.json
outputs/ward_value_mvp0/leaderboards.json
outputs/ward_value_mvp0/match_debug/*.json
```

### 9.2 HTML 输出

```text
outputs/ward_value_mvp0/index.html
```

页面包含：

1. Match 样本摘要。
2. Best Observer Wards。
3. Worst Observer Wards。
4. Best Sentry Wards。
5. Ward Spot 聚类表。
6. 每个眼位的指标拆解。
7. 可点击跳转到已有 match timeline 的链接。

## 10. 排行榜

MVP-0 至少输出：

### 10.1 Observer 排行

- Best Overall Observer
- Most Seen Observer
- Best Low-Overlap Observer
- Best First-Contact Observer
- Worst Low-Value Observer
- Fast Dewarded Observer

### 10.2 Sentry 排行

- Best Anti-Invis Sentry
- Best Observer-Assisted Sentry
- Fast Dewarded Sentry

如果没有隐身样本，则 Sentry anti-invis 排行榜显示为空状态，并解释原因。

## 11. 验收标准

### 11.1 数据验收

- 两场 match_id 均能成功读取 players。
- 两场 match_id 均能成功读取 player_intervals2。
- 两场 match_id 均能成功读取 ward_placed_left_fact。
- 如果 combat_logs 可用，报告中 `invisibilityDataAvailable=true`。
- 如果 combat_logs 不可用，报告中明确显示该能力不可用，并阻止计算隐身相关分数。

### 11.2 算法验收

- Observer ward 使用 native FoW + 动态树状态。
- Sentry ward 真视无遮挡，半径 1000。
- 隐身英雄只有在己方真眼覆盖时才计入被看到。
- 每个 Ward Instance 都有指标拆解，不只给总分。
- 每个得分能回溯到 matchId、ehandle、时间段和英雄 slot。

### 11.3 输出验收

- 生成 `ward_instances.json`。
- 生成 `ward_spots.json`。
- 生成 `leaderboards.json`。
- 生成可打开的静态 `index.html`。
- HTML 中展示样本数量和置信度。
- HTML 中明确标注 MVP-0 的限制。

## 12. 本期不做

以下能力不进入 MVP-0：

- 后端 API。
- 多战队 benchmark。
- 自动 scouting 报告。
- Opportunity Adjusted Metrics。
- Roshan 专项窗口。
- Smoke 暴露分析。
- 技能临时视野。
- 召唤物、熊、幻象、守卫等非英雄单位共享视野。
- 机器学习预测敌方眼位。

## 13. 实现建议

### 13.1 新增脚本

```text
tools/compute_ward_value_metrics.py
```

职责：

1. 接收 match_id 列表。
2. 调用或复用 native ward occlusion 计算。
3. 读取英雄逐秒位置。
4. 读取隐身状态。
5. 计算 Ward Instance 指标。
6. 计算价值评分。
7. 聚类 Ward Spot。
8. 输出 JSON 和 HTML。

### 13.2 命令示例

```powershell
$env:DOTA_DB_HOST='...'
$env:DOTA_DB_PORT='9030'
$env:DOTA_DB_USER='...'
$env:DOTA_DB_PASSWORD='...'
python tools\compute_ward_value_metrics.py `
  --match-id 8852716636 `
  --match-id 8852757973 `
  --output-dir outputs\ward_value_mvp0
```

### 13.3 性能策略

- 每场比赛先生成 ward occlusion cache。
- 同一 ward 的 `visionTimeline` 复用，不重复计算。
- 英雄位置按秒读取，只检测活英雄。
- 对每秒当前活跃 ward 构建 cell set。
- 大 JSON 输出只保留指标和必要回溯，不保存每秒完整大 cell。

## 14. 当前开放问题

1. `combat_logs` 是否在当前数据库账号下可读。
2. `ward_placed_left_fact` 的消失事件是否足以可靠区分 deward / expire。
3. 两场样本中是否存在隐身英雄或隐身 modifier。
4. 两场样本是否都属于 7.41 地图数据。
5. 是否需要同时为两场比赛生成独立 match timeline，还是只输出价值评分报告。
