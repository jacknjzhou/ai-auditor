# ai-auditor 容器编排快捷命令
# 在 deploy/ 目录执行：make up / make logs / make backup ...
# （Windows 无 make 时，用 README-container.md §2 里的等价 docker compose 原生命令）

COMPOSE := docker compose
FILE    := docker-compose.yml
DC      := $(COMPOSE) -f $(FILE)

.DEFAULT_GOAL := help

.PHONY: help env up down ps logs build init load-sops seed check \
        shell-api psql redis-cli backup restore scale-worker reset

help: ## 显示全部可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

env: ## 若不存在则从模板生成 .env
	@test -f .env || (cp .env.example .env \
	  && echo "已生成 deploy/.env —— 请修改全部 change-me 密钥后重跑" \
	  && exit 1)

up: env ## 构建并启动全套服务（后台）
	$(DC) up -d --build
	@echo "→ http://localhost:$${AUDITOR_EXPOSE_PORT:-8300}/healthz"

down: ## 停止并移除容器（保留数据卷）
	$(DC) down

ps: ## 查看服务状态与健康
	$(DC) ps

logs: ## 跟踪全部日志（Ctrl-C 退出）
	$(DC) logs -f --tail=100

build: ## 仅重建应用镜像
	$(DC) build

init: ## 重跑一次性初始化（建表 + 装载 SOP + 种子，幂等）
	$(DC) run --rm --no-deps migrate

load-sops: ## 重新装载 config/sops/（改 SOP 后免重建镜像）
	$(DC) run --rm --no-deps migrate python -m app.cli load-sops

seed: ## 种入演示规则，用法：make seed FLOW=expense
	$(DC) run --rm --no-deps migrate python -m app.cli seed $(FLOW)

check: ## 数据层连通自检（PostgreSQL + Redis）
	$(DC) run --rm --no-deps migrate python -m app.cli check

shell-api: ## 进入 api-1 的 shell
	$(DC) exec api-1 bash

psql: ## 打开生产库 psql
	$(DC) exec postgres psql -U auditor -d auditor

redis-cli: ## 打开 redis-cli（带鉴权）
	$(DC) exec redis sh -c 'redis-cli -a "$$REDIS_PASSWORD"'

backup: ## 备份数据库到 deploy/backups/
	@mkdir -p backups
	$(DC) exec -T postgres pg_dump -U auditor auditor \
	  | gzip > backups/auditor-$$(date +%F-%H%M).sql.gz
	@ls -lh backups | tail -3

restore: ## 从备份恢复，用法：make restore F=backups/auditor-xxx.sql.gz
	@test -n "$(F)" || (echo "用法: make restore F=backups/auditor-xxx.sql.gz" && exit 1)
	gunzip -c $(F) | $(DC) exec -T postgres psql -U auditor -d auditor

scale-worker: ## Worker 扩容到 4 副本（LLM 链堆积时）
	$(DC) up -d --scale worker-1=2 --scale worker-2=2 --no-recreate

reset: ## ⚠️ 清空全部数据（删除数据卷）后重启
	@echo "⚠️  将删除 pg-data / redis-data 全部数据，5 秒内 Ctrl-C 取消..."
	@sleep 5
	$(DC) down -v
	$(DC) up -d --build
