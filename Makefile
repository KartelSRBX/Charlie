.DEFAULT_GOAL := help
COMPOSE ?= docker compose -f deploy/docker-compose.yml
PY ?= python3
# Demo-only passwords of the service accounts; real ones come from a secret store.
export SVC_LOADER_PASSWORD ?= loader-pass
export SVC_REPORTING_PASSWORD ?= report-pass
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
	@echo "MinIO:      http://localhost:9101  (charlie / charlie-secret), S3 API on :9100"
	@echo "ClickHouse: http://localhost:8123/play  (admin / charlie, one port per node: 8123-8126)"

down: ## Stop the cluster, keep data
	$(COMPOSE) down

clean: ## Stop the cluster and delete all data
	$(COMPOSE) down -v

upgrade: ## Rolling upgrade to the images in topology.yaml, under load (render first)
	$(CHARLIE) upgrade

backup: ## Full backup of every database to the object store
	$(CHARLIE) backup create

backup-drill: ## Back up, DROP every database, restore, compare (destructive)
	$(CHARLIE) backup drill

access-plan: ## Show how ClickHouse and PostgreSQL differ from access/model.yaml
	$(CHARLIE) access plan

access: ## Seed the demo LDAP directory, then make both databases match the access model
	$(CHARLIE) directory seed
	$(CHARLIE) access apply

access-drift: ## Exit 1 when either database differs from the access model
	$(CHARLIE) access drift

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

e2e: ## Access tests and failure drills against the running cluster (takes ~7 minutes)
	pytest -m e2e tests/e2e

lint: ## Ruff lint + format check
	ruff check .
	ruff format --check .

fmt: ## Auto-format
	ruff format .
	ruff check --fix .

dashboards: ## Regenerate Grafana dashboards from grafana/build_dashboards.py
	$(PY) grafana/build_dashboards.py
