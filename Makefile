UV ?= uv

.PHONY: install check fmt test clean

install:
	$(UV) sync --extra dev

check:
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run pytest -q

fmt:
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

test:
	$(UV) run pytest -q

clean:
	rm -rf .pytest_cache .ruff_cache dist build
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
