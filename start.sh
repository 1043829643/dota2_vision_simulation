#!/bin/bash
# Dota 2 Vision Simulation 启动脚本
# 设置所有必需的数据库环境变量

export DOTA_DB_HOST=47.86.96.51
export DOTA_DB_PORT=9030
export DOTA_DB_USER=dota2_reader
export DOTA_DB_PASSWORD='readerDota.'
export DOTA_DB_DATABASE=dota2_analysis
export DOTA_OVERVIEW_DATABASE=dwd_dota2

cd /workspace/projects
python -m uvicorn web.backend.app:app --host 0.0.0.0 --port ${DEPLOY_RUN_PORT}