# Tell Me More — no dependencies, so no virtualenv needed.
# Uses uv (pinned Python) when installed, otherwise python3.

PORT ?= 8765
KEY  ?=
PY   := $(shell command -v uv >/dev/null 2>&1 && echo "uv run --no-project --python 3.12" || echo python3)

.PHONY: run help

help:
	@echo "make run                 start the game server"
	@echo "make run KEY=dinner      keep the same host link every time (bookmark it on the tablet)"
	@echo "make run PORT=9000       use a different port"

run:
	$(PY) server.py --port $(PORT) $(if $(KEY),--host-key $(KEY))
