.PHONY: venv dev test lint image helm-lint

venv:
	python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -e ".[dev]"

dev:  ## run with auto-reload on code changes
	OPSDECK_RELOAD=true .venv/bin/opsdeck

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check opsdeck tests

image:
	docker build -t opsdeck:$$(git rev-parse --short HEAD 2>/dev/null || echo dev) -t opsdeck:latest .

helm-lint:
	helm lint deploy/helm/opsdeck && helm template t deploy/helm/opsdeck >/dev/null
