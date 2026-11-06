# Tell Me More — no dependencies, so no virtualenv needed.
# Uses uv (pinned Python) when installed, otherwise python3.

PORT ?= 8765
# Host key: KEY=... on the command line, else the contents of .host-key
# (gitignored), else server.py picks a random one each run.
KEY  ?= $(shell cat .host-key 2>/dev/null)
PY   := $(shell command -v uv >/dev/null 2>&1 && echo "uv run --no-project --python 3.12" || echo python3)

.PHONY: run help

help:
	@echo "make run                 start the game server"
	@echo "make run KEY=dinner      use this host key for this run"
	@echo "                         (or put a key in .host-key to use it every run)"
	@echo "make run PORT=9000       use a different port"

run:
	$(PY) server.py --port $(PORT) $(if $(KEY),--host-key $(KEY))
