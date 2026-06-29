# Dota 2 Vision Simulation 项目说明

## 项目概览
Dota 2 7.41 ward-vision 模拟项目，用于分析和可视化比赛中守卫的视野范围和英雄可见性。

## 技术栈
- **后端**: Python 3.12 + FastAPI + Uvicorn
- **数据库**: MySQL/StarRocks (通过 PyMySQL 连接)
- **前端**: 静态 HTML (位于 web/static/index.html)

## 核心功能
1. **视野计算**: 基于 Valve cache.fow 的守卫视野模拟
2. **英雄可见性分析**: 计算守卫覆盖的英雄秒数和持续出现次数
3. **团队对比**: 多团队守卫价值对比分析
4. **时间线渲染**: 逐秒守卫时间线可视化

## 项目结构
```
.
├── web/
│   ├── backend/
│   │   └── app.py          # FastAPI 后端主文件
│   └── static/
│       └── index.html      # 前端静态页面
├── demo/
│   └── 8831926213/         # 示例比赛数据
├── tools/
│   ├── compute_ward_hero_visibility.py  # 英雄可见性计算
│   ├── compute_ward_value_metrics.py    # 守卫价值指标计算
│   └── build_8831926213_timeline.ps1    # 构建时间线脚本
├── resources/              # 运行时输入数据
│   ├── cache.fow          # Valve FoW 文件
│   ├── ward_timeline_source.json  # 守卫时间线数据
│   └── map_741_calibrated.png     # 7.41 地图图片
├── outputs/                # 输出结果
├── map-data-741/           # 7.41 地图数据
├── requirements.txt        # Python 依赖
└── .coze                   # Coze 配置文件
```

## 构建和部署命令
- **安装依赖**: `python -m pip install -r requirements.txt`
- **开发环境**: `coze dev` (自动启动 FastAPI 服务)
- **生产环境**: `coze start` (生产部署)

## API 接口
基于 FastAPI 的后端服务提供以下主要接口:
- `/` - 静态首页
- `/api/visibility` - 英雄可见性查询
- `/api/ward-value` - 守卫价值指标
- `/api/team-comparison` - 团队对比分析

## 环境变量
数据库连接需要以下环境变量:
- `DOTA_DB_HOST`: 数据库主机地址
- `DOTA_DB_PORT`: 数据库端口 (默认 9030)
- `DOTA_DB_USER`: 数据库用户名
- `DOTA_DB_PASSWORD`: 数据库密码
- `DOTA_DB_DATABASE`: 数据库名称 (默认 dota2_analysis)

## 开发注意事项
1. 项目使用 Python 3.12，不支持其他语言
2. 数据库环境变量必须配置才能使用查询功能
3. 所有静态资源位于 web/static 目录
4. 输出结果存储在 outputs 目录下
5. 地图数据使用手动校准的 14 点仿射拟合

## 常见问题
1. **数据库连接失败**: 检查环境变量配置是否正确
2. **端口占用**: 使用环境变量 DEPLOY_RUN_PORT 指定端口
3. **资源文件缺失**: 确保 resources 目录下文件完整

## 测试验证
- 启动服务后访问根路径应返回静态 HTML 页面
- API 接口需要配置数据库环境变量才能正常响应