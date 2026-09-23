UV ?= uv

.PHONY: install check fmt test clean openclaw-workspace openclaw-check calendar-helper

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
