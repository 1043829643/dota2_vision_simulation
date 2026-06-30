#!/bin/bash

# 设置数据库连接环境变量
export DOTA_DB_HOST=47.86.96.51
export DOTA_DB_PORT=9030
export DOTA_DB_USER=dota2_reader
export DOTA_DB_PASSWORD='readerDota.'

# 启动服务
cd ${COZE_WORKSPACE_PATH}
python -m uvicorn web.backend.app:app --host 0.0.0.0 --port ${DEPLOY_RUN_PORT}