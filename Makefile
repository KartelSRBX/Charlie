.DEFAULT_GOAL := help
COMPOSE ?= docker compose -f deploy/docker-compose.yml
PY ?= python3
CHARLIE ?= $(if $(wildcard .venv/bin/charlie),.venv/bin/charlie,charlie)

help: ## Show targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

render: ## Regenerate deploy/ from topology.yaml
	$(CHARLIE) render

check: ## Fail if deploy/ drifted from topology.yaml
	$(CHARLIE) check

up: ## Start Keepers and ClickHouse nodes, Prometheus and Grafana, then apply the schema
	$(COMPOSE) up -d --wait
	$(CHARLIE) migrate
	@echo "Grafana:    http://localhost:3000   (Charlie / Cluster health)"
	@echo "Prometheus: http://localhost:9090"
	@echo "ClickHouse: http://localhost:8123/play  (admin / charlie, one port per node: 8123-8126)"

down: ## Stop the cluster, keep data
	$(COMPOSE) down

clean: ## Stop the cluster and delete all data
	$(COMPOSE) down -v

status: ## Keeper and replica health
	$(CHARLIE) status

load: ## Insert 100k rows
	$(CHARLIE) load --batches 20 --batch-size 5000

verify: ## Check totals, duplicates, replica equality and shard balance
	$(CHARLIE) verify

drill: ## Kill a replica and a Keeper under load, then prove nothing was lost
	$(PY) scripts/drill.py

dev: ## Create a virtualenv with dev dependencies
	$(PY) -m venv .venv && .venv/bin/pip install -e '.[dev]'

test: ## Unit tests
	pytest

e2e: ## Failure drills against the running cluster (takes ~6 minutes)
	pytest -m e2e tests/e2e

lint: ## Ruff lint + format check
	ruff check .
	ruff format --check .

fmt: ## Auto-format
	ruff format .
	ruff check --fix .

dashboards: ## Regenerate Grafana dashboards from grafana/build_dashboards.py
	$(PY) grafana/build_dashboards.py
