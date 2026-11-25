# Tell Me More — no dependencies, so no virtualenv needed.
# Uses uv (pinned Python) when installed, otherwise python3.

PORT ?= 8765
# Host key: KEY=... on the command line, else the contents of .host-key
# (gitignored), else server.py picks a random one each run.
KEY  ?= $(shell cat .host-key 2>/dev/null)
PY   := $(shell command -v uv >/dev/null 2>&1 && echo "uv run --no-project --python 3.12" || echo python3)

.PHONY: run stop restart help

help:
	@echo "make run                 start the game server"
	@echo "make restart             stop the running game and start it again"
	@echo "make stop                stop the running game"
	@echo "make run KEY=dinner      use this host key for this run"
	@echo "                         (or put a key in .host-key to use it every run)"
	@echo "make run PORT=9000       use a different port"

run:
	$(PY) server.py --port $(PORT) $(if $(KEY),--host-key $(KEY))

# Stop whatever is holding the port, as long as it's this game; anything else
# is left alone. Ctrl+C first, then TERM, then KILL: a server started in the
# background ignores Ctrl+C, so one signal isn't always enough.
# Guests are signed out either way — the player list lives in memory — but
# each story is written to the stories folder as it's told, so nothing is lost
# and the next start picks tonight's list up again.
stop:
	@pids=$$(lsof -ti tcp:$(PORT) -sTCP:LISTEN 2>/dev/null); \
	if [ -z "$$pids" ]; then echo "  Nothing is listening on port $(PORT)."; exit 0; fi; \
	for pid in $$pids; do \
		case "$$(ps -o command= -p $$pid)" in \
			*server.py*) ;; \
			*) echo "  Port $(PORT) is held by pid $$pid, which isn't this game. Leaving it alone."; exit 1 ;; \
		esac; \
	done; \
	echo "  Stopping the game (pid $$(echo $$pids))..."; \
	for sig in INT TERM KILL; do \
		kill -$$sig $$pids 2>/dev/null; \
		for _ in 1 2 3 4 5 6 7 8 9 10; do \
			[ -z "$$(lsof -ti tcp:$(PORT) -sTCP:LISTEN 2>/dev/null)" ] && exit 0; \
			sleep 0.2; \
		done; \
	done; \
	echo "  Port $(PORT) is still busy. Stop it by hand and try again."; exit 1

restart:
	@$(MAKE) stop
	@$(MAKE) run
