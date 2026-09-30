UV ?= uv

.PHONY: install check fmt test clean openclaw-workspace openclaw-check calendar-helper neo4j-up graph-push

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

# Calendar access for the gateway: see calendar-helper/main.swift. Ad-hoc
# signed, so every rebuild is a new app to macOS and needs a fresh Allow.
CALENDAR_APP ?= $(HOME)/Applications/MitsyncCalendar.app

calendar-helper:
	rm -rf $(CALENDAR_APP)
	mkdir -p $(CALENDAR_APP)/Contents/MacOS
	cp calendar-helper/Info.plist $(CALENDAR_APP)/Contents/Info.plist
	swiftc -O -framework EventKit -o $(CALENDAR_APP)/Contents/MacOS/MitsyncCalendar calendar-helper/main.swift
	codesign --force --sign - $(CALENDAR_APP)
	@echo "built $(CALENDAR_APP); now run: open $(CALENDAR_APP)  (and click Allow)"

# A local Neo4j for `mitsync graph push`, with the password from .env. The
# Docker image only accepts the user `neo4j`. Browser: http://localhost:7474,
# and NEO4J_URI=bolt://localhost:7687 in .env.
neo4j-up:
	@set -a; . ./.env; set +a; \
	docker start mitsync-neo4j 2>/dev/null || docker run -d --name mitsync-neo4j \
	  -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH="neo4j/$$NEO4J_PASSWORD" \
	  -v mitsync-neo4j-data:/data neo4j:5
	@echo "neo4j starting: http://localhost:7474 (bolt://localhost:7687)"

graph-push:
	$(UV) run mitsync graph backbone
	$(UV) run mitsync graph rebuild
	$(UV) run mitsync graph push
