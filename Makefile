.PHONY: setup test lint

setup:        ## First-time setup: venv + deps + cophilo init
	./setup.sh

test:         ## Run the test suite
	./.venv/bin/python -m pytest -q

lint:         ## Lint with ruff
	./.venv/bin/ruff check src tests
