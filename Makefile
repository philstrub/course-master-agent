UV ?= uv

.PHONY: install check fmt test clean openclaw-workspace openclaw-check

# OpenClaw refuses symlinked bootstrap files ("symlink path component not
# allowed"), so the workspace gets copies; the originals stay here, in git.
WORKSPACE ?= ..
BOOTSTRAP := AGENTS SOUL USER

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

openclaw-workspace:
	@for f in $(BOOTSTRAP); do \
	  rm -f $(WORKSPACE)/$$f.md && cp openclaw/workspace/$$f.md $(WORKSPACE)/$$f.md && echo "copied $$f.md"; \
	done

openclaw-check:
	@for f in $(BOOTSTRAP); do \
	  if [ -L $(WORKSPACE)/$$f.md ]; then echo "$$f.md is a symlink: run make openclaw-workspace"; exit 1; fi; \
	  cmp -s openclaw/workspace/$$f.md $(WORKSPACE)/$$f.md || { echo "$$f.md is stale: run make openclaw-workspace"; exit 1; }; \
	done; echo "workspace bootstrap files are current"
