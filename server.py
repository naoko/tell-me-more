#!/usr/bin/env python3
"""
Tell Me More — a storytelling game for gatherings.

Run it on any computer on your home Wi-Fi:

    python3 server.py

The tablet opens the host URL it prints; guests scan the QR code on the
tablet (or type the address) to join from their phones.

Zero dependencies: Python 3.8+ standard library only.
"""
import argparse
import json
import os
import random
import secrets
import socket
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")
PROMPTS_PATH = os.path.join(ROOT, "prompts.json")
STORIES_DIR = os.path.join(ROOT, "stories")

LEVELS = ["light", "warm", "deeper"]
REACTIONS = ["❤️", "😂", "😮", "👏", "🥲"]
MAX_GROUPS = 6
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
}


def now_ms():
    return int(time.time() * 1000)


def fmt_secs(secs):
    secs = int(secs or 0)
    return f"{secs // 60}:{secs % 60:02d}"


def load_prompts():
    """Read prompts.json fresh each draw, so edits apply without a restart."""
    with open(PROMPTS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return {lvl: [p.strip() for p in data.get(lvl, []) if str(p).strip()] for lvl in LEVELS}


class GameError(Exception):
    pass


class Game:
    """All game state lives here, guarded by one lock. Every change bumps
    `version` and wakes the live-update streams."""

    def __init__(self, host_key, join_url):
        self.cond = threading.Condition()
        self.version = 0
        self.host_key = host_key
        self.join_url = join_url
        self.settings = {
            "story_secs": 150,
            "question_secs": 180,
            "max_level": 1,  # 1 = light, 2 = light + warm, 3 = everything
            "families": ["Everyone"],  # one group = no families; add up to MAX_GROUPS in Settings
        }
        self.players = {}  # id -> player dict
        self.order = []  # join order
        self.stories = []
        self.reactions = []
        self.reaction_n = 0
        self.last_react = {}
        os.makedirs(STORIES_DIR, exist_ok=True)
        self._new_session_file()
        self._new_round()

    # ---------- helpers ----------

    def _new_session_file(self):
        self.session_path = os.path.join(STORIES_DIR, time.strftime("%Y-%m-%d_%H%M%S") + ".json")

    def _new_round(self):
        self.phase = "lobby"  # lobby | ready | card | telling | questions
        self.teller = None
        self.prompt = None
        self.used = set()
        self.timer = None
        self.asker = None
        self.current = None
        self.saved_phase = None
        self.turn_no = 0
        self.group_last_turn = {}  # group index -> turn number it last told a story

    def _bump(self):
        self.version += 1
        self.cond.notify_all()

    @staticmethod
    def _need(ok, msg="Only the host can do that"):
        if not ok:
            raise GameError(msg)

    def _is_teller(self, me):
        return me is not None and me["id"] == self.teller

    def _add(self, name, family, device):
        name = str(name or "").strip()[:24]
        self._need(name, "Please enter a name")
        try:
            family = int(family)
        except (TypeError, ValueError):
            family = 0
        if not 0 <= family < len(self.settings["families"]):
            family = 0
        pid = secrets.token_urlsafe(8)
        self.players[pid] = {
            "id": pid, "name": name, "family": family, "told": 0, "asked": 0,
            "swap_used": False, "hand": None, "device": device,
            # False when they joined before families existed; their phone will ask
            "family_set": len(self.settings["families"]) > 1,
        }
        self.order.append(pid)
        return pid

    def _pick_next(self):
        """Everyone tells once before anyone tells twice; among those, the group
        that told least recently goes next, so turns rotate across groups."""
        if not self.players:
            return None
        low = min(p["told"] for p in self.players.values())
        cands = [self.players[i] for i in self.order if self.players[i]["told"] == low and i != self.teller]
        if not cands:
            cands = [self.players[i] for i in self.order if i != self.teller] or [self.players[i] for i in self.order]
        def recency(p):
            return self.group_last_turn.get(p["family"], -1)
        best = min(recency(c) for c in cands)
        return next(c for c in cands if recency(c) == best)["id"]

    def _begin_turn(self, pid):
        self.teller = pid
        self.prompt = None
        self.timer = None
        self.asker = None
        self.saved_phase = None
        self.phase = "ready" if pid else "lobby"

    def _draw(self, exclude=None):
        prompts = load_prompts()
        pool = [(text, lvl) for lvl in LEVELS[: self.settings["max_level"]] for text in prompts[lvl]]
        fresh = [x for x in pool if x[0] not in self.used and x[0] != exclude]
        if not fresh:  # the deck ran out — reshuffle
            self.used = set()
            fresh = [x for x in pool if x[0] != exclude] or pool
        self._need(fresh, "No prompts found — check prompts.json")
        text, lvl = random.choice(fresh)
        self.used.add(text)
        self.prompt = {"text": text, "level": lvl}
        self.phase = "card"

    def _start_timer(self, secs):
        self.timer = {"total": secs * 1000, "ends_at": now_ms() + secs * 1000, "paused_left": None}

    def _remaining(self):
        t = self.timer
        if not t:
            return 0
        if t["ends_at"] is None:
            return t["paused_left"]
        return max(0, t["ends_at"] - now_ms())

    def _save(self):
        try:
            tmp = self.session_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"families": self.settings["families"], "stories": self.stories},
                          f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.session_path)
        except OSError as e:
            print("warning: couldn't save stories:", e)

    # ---------- public view ----------

    def public(self):
        order = [self.players[i] for i in self.order if i in self.players]
        teller = self.players.get(self.teller)
        tf = teller["family"] if teller else -1
        queue = [p for p in order if p["hand"] and p["id"] != self.teller]
        # Other groups get first pick at questions — that's where the connection happens.
        queue.sort(key=lambda p: (p["family"] == tf, p["hand"]))
        return {
            "v": self.version,
            "server_now": now_ms(),
            "phase": self.phase,
            "settings": self.settings,
            "players": order,
            "teller": self.teller,
            "prompt": self.prompt,
            "timer": self.timer,
            "asker": self.asker,
            "queue": [p["id"] for p in queue],
            "story_no": len(self.stories) + 1,
            "stories": len(self.stories),
            "questions_asked": len(self.current["questions"]) if self.current else 0,
            "reactions": self.reactions[-15:],
            "join_url": self.join_url,
        }

    # ---------- actions ----------

    def act(self, data):
        action = str(data.get("action", ""))
        with self.cond:
            key = str(data.get("key", "")).encode()
            is_host = secrets.compare_digest(key, self.host_key.encode())
            me = self.players.get(str(data.get("player_id") or ""))
            if action == "check_host":
                return {"ok": is_host}
            fn = getattr(self, "_a_" + action, None)
            self._need(fn is not None, "Unknown action")
            out = fn(data, is_host, me) or {}
            self._bump()
            return {"ok": True, **out}

    def _a_join(self, d, host, me):
        return {"player_id": self._add(d.get("name"), d.get("family"), device=True)}

    def _a_add_player(self, d, host, me):
        self._need(host)
        return {"player_id": self._add(d.get("name"), d.get("family"), device=False)}

    def _a_remove_player(self, d, host, me):
        pid = str(d.get("target") or "")
        self._need(host or (me is not None and me["id"] == pid))
        if pid not in self.players:
            return
        was_teller = pid == self.teller
        del self.players[pid]
        self.order.remove(pid)
        if self.asker == pid:
            self.asker = None
        if was_teller and self.phase != "lobby":
            self.current = None
            self.teller = None
            self._begin_turn(self._pick_next())

    def _a_settings(self, d, host, me):
        self._need(host)
        s = self.settings
        if "story_secs" in d:
            s["story_secs"] = max(30, min(900, int(d["story_secs"])))
        if "question_secs" in d:
            s["question_secs"] = max(30, min(900, int(d["question_secs"])))
        if "max_level" in d:
            s["max_level"] = max(1, min(3, int(d["max_level"])))
        if "families" in d:
            names = [str(n).strip()[:24] for n in list(d["families"])[:MAX_GROUPS]]
            self._need(names, "Keep at least one group")
            names = [n or f"Group {i + 1}" for i, n in enumerate(names)]
            # slots[new_index] = old index (or None for a new group); defaults to same positions
            slots = list(d.get("slots") or range(len(names)))[:len(names)]
            remap = {}
            for new_i, old_i in enumerate(slots):
                if isinstance(old_i, int) and 0 <= old_i < len(s["families"]) and old_i not in remap:
                    remap[old_i] = new_i
            if len(names) == 1:  # turning families off merges everyone into one group
                remap = {i: 0 for i in range(len(s["families"]))}
            for p in self.players.values():
                self._need(p["family"] in remap,
                           f"Move or remove the people in {s['families'][p['family']]} before removing it")
            for p in self.players.values():
                p["family"] = remap[p["family"]]
            self.group_last_turn = {remap[k]: v for k, v in self.group_last_turn.items() if k in remap}
            if len(names) == 1:  # back to no families: ask again if families come back
                for p in self.players.values():
                    p["family_set"] = False
            s["families"] = names

    def _a_start(self, d, host, me):
        self._need(host)
        self._need(self.players, "Add at least one person first")
        if self.phase != "lobby":
            return
        if self.saved_phase and self.teller in self.players:
            self.phase, self.saved_phase = self.saved_phase, None
        else:
            self._begin_turn(self._pick_next())

    def _a_lobby(self, d, host, me):
        """Back to the join screen (e.g. a late arrival). Pauses the current turn."""
        self._need(host)
        if self.phase == "lobby":
            return
        if self.timer and self.timer["ends_at"] is not None:
            self.timer["paused_left"] = self._remaining()
            self.timer["ends_at"] = None
        self.saved_phase = self.phase
        self.phase = "lobby"

    def _a_reset(self, d, host, me):
        self._need(host)
        for p in self.players.values():
            p.update(told=0, asked=0, swap_used=False, hand=None)
        self.stories = []
        self._new_session_file()
        self._new_round()

    def _a_pick_teller(self, d, host, me):
        self._need(host)
        pid = str(d.get("target") or "")
        self._need(pid in self.players, "Unknown person")
        self.current = None
        self._begin_turn(pid)

    def _a_draw(self, d, host, me):
        self._need(host or self._is_teller(me), "It's not your turn yet")
        self._need(self.phase == "ready", "A card is already drawn")
        self._draw()

    def _a_swap(self, d, host, me):
        self._need(host or self._is_teller(me), "It's not your turn yet")
        self._need(self.phase == "card", "Nothing to swap yet")
        t = self.players[self.teller]
        self._need(host or not t["swap_used"], "You've used your free swap")
        t["swap_used"] = True
        self._draw(exclude=self.prompt["text"] if self.prompt else None)

    def _a_custom_prompt(self, d, host, me):
        self._need(host)
        self._need(self.phase in ("ready", "card"), "Finish this story first")
        text = str(d.get("text", "")).strip()[:200]
        self._need(text, "Write a prompt first")
        self.prompt = {"text": text, "level": "custom"}
        self.phase = "card"

    def _a_start_story(self, d, host, me):
        self._need(host or self._is_teller(me), "It's not your turn yet")
        self._need(self.phase == "card", "Draw a card first")
        t = self.players[self.teller]
        self.current = {
            "teller": t["name"],
            "family": self.settings["families"][t["family"]] if len(self.settings["families"]) > 1 else None,
            "family_index": t["family"],
            "prompt": self.prompt["text"],
            "level": self.prompt["level"],
            "at": time.strftime("%Y-%m-%d %H:%M"),
            "questions": [],
        }
        self.phase = "telling"
        self._start_timer(self.settings["story_secs"])

    def _a_pause(self, d, host, me):
        self._need(host or self._is_teller(me))
        t = self.timer
        self._need(t and t["ends_at"] is not None, "Timer isn't running")
        t["paused_left"] = self._remaining()
        t["ends_at"] = None

    def _a_resume(self, d, host, me):
        self._need(host or self._is_teller(me))
        t = self.timer
        self._need(t and t["ends_at"] is None, "Timer isn't paused")
        t["ends_at"] = now_ms() + t["paused_left"]
        t["paused_left"] = None

    def _a_add_time(self, d, host, me):
        self._need(host or self._is_teller(me))
        t = self.timer
        self._need(t, "No timer running")
        if t["ends_at"] is None:
            t["paused_left"] += 30000
        else:
            t["ends_at"] = max(t["ends_at"], now_ms()) + 30000
        t["total"] += 30000

    def _a_end_story(self, d, host, me):
        self._need(host or self._is_teller(me), "It's not your turn")
        self._need(self.phase == "telling", "No story in progress")
        self.current["told_secs"] = round((self.timer["total"] - self._remaining()) / 1000)
        self.phase = "questions"
        self.asker = None
        self._start_timer(self.settings["question_secs"])

    def _a_set_family(self, d, host, me):
        """Host moves anyone; a guest can set their own family."""
        pid = str(d.get("target") or (me["id"] if me else ""))
        self._need(host or (me is not None and me["id"] == pid), "Only the host can move other people")
        self._need(pid in self.players, "Unknown person")
        fam = int(d.get("family"))
        self._need(0 <= fam < len(self.settings["families"]), "Unknown family")
        p = self.players[pid]
        p["family"] = fam
        p["family_set"] = True

    def _a_raise_hand(self, d, host, me):
        self._need(me, "Join the game first")
        self._need(not self._is_teller(me), "You're the storyteller!")
        self._need(self.phase in ("telling", "questions"), "Hands go up during a story")
        if me["hand"] is None:
            me["hand"] = now_ms()

    def _a_lower_hand(self, d, host, me):
        self._need(me, "Join the game first")
        me["hand"] = None

    def _a_call_on(self, d, host, me):
        self._need(host or self._is_teller(me))
        self._need(self.phase == "questions", "Questions come after the story")
        pid = str(d.get("target") or "")
        self._need(pid in self.players and pid != self.teller, "Can't call on that person")
        p = self.players[pid]
        p["hand"] = None
        p["asked"] += 1
        self.asker = pid
        self.current["questions"].append(p["name"])

    def _a_done_asking(self, d, host, me):
        self._need(host or self._is_teller(me) or (me is not None and me["id"] == self.asker))
        self.asker = None

    def _a_next_turn(self, d, host, me):
        self._need(host or (self._is_teller(me) and self.phase == "questions"))
        self._need(self.phase != "lobby", "Start the game first")
        if self.phase in ("telling", "questions") and self.current:
            if "told_secs" not in self.current:
                self.current["told_secs"] = round((self.timer["total"] - self._remaining()) / 1000)
            t = self.players.get(self.teller)
            if t:
                t["told"] += 1
            self.current["n"] = len(self.stories) + 1
            self.stories.append(self.current)
            self._save()
        self.current = None
        for p in self.players.values():
            p["hand"] = None
        if self.teller in self.players:
            self.turn_no += 1
            self.group_last_turn[self.players[self.teller]["family"]] = self.turn_no
        self._begin_turn(self._pick_next())

    def _a_react(self, d, host, me):
        self._need(me, "Join the game first")
        emoji = d.get("emoji")
        self._need(emoji in REACTIONS, "Unknown reaction")
        t = time.time()
        if t - self.last_react.get(me["id"], 0) < 0.6:
            return  # gentle rate limit
        self.last_react[me["id"]] = t
        self.reaction_n += 1
        self.reactions.append({"n": self.reaction_n, "e": emoji, "name": me["name"], "family": me["family"]})
        self.reactions = self.reactions[-30:]


# ---------- keepsake pages ----------

def keepsake_html(families, stories):
    items = []
    for s in stories:
        meta = [f'<span class="chip fam-{s["family_index"]}">{escape(s["teller"])}</span>']
        if s.get("family"):
            meta.append(escape(s["family"]))
        if s.get("told_secs", 0) >= 5:
            meta.append(fmt_secs(s["told_secs"]))
        if s["questions"]:
            meta.append("questions from " + escape(", ".join(s["questions"])))
        items.append(
            f'<li class="kstory"><div class="knum">{s["n"]}</div><div>'
            f'<p class="kprompt">{escape(s["prompt"])}</p>'
            f'<div class="kmeta">{" · ".join(meta)}</div></div></li>')
    body = "".join(items) or '<li class="hint">No stories yet — they appear here as each turn finishes.</li>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Our stories · Tell Me More</title>
<link rel="stylesheet" href="/web/app.css"></head><body class="keepsake">
<header><div class="brand-big">Our stories</div>
<p class="hint">{escape(" · ".join(families)) + " — " if len(families) > 1 else ""}{time.strftime("%B %-d, %Y")}</p>
<p><a class="btn" href="/keepsake.md">Download as text</a></p></header>
<ol class="klist">{body}</ol></body></html>"""


def keepsake_md(families, stories):
    lines = [f"# Tell Me More — our stories", f"_{', '.join(families) + ' · ' if len(families) > 1 else ''}{time.strftime('%B %-d, %Y')}_", ""]
    for s in stories:
        lines.append(f"## {s['n']}. {s['prompt']}")
        q = f" · questions from {', '.join(s['questions'])}" if s["questions"] else ""
        dur = f" · {fmt_secs(s['told_secs'])}" if s.get("told_secs", 0) >= 5 else ""
        fam = f" ({s['family']})" if s.get("family") else ""
        lines.append(f"**{s['teller']}**{fam}{dur}{q}")
        lines.append("")
    return "\n".join(lines)


# ---------- HTTP ----------

GAME = None


class Handler(BaseHTTPRequestHandler):
    server_version = "TellMeMore/1.0"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name):
        name = os.path.normpath(name)
        if name.startswith("..") or os.path.isabs(name):
            return self._send(404, {"error": "not found"})
        path = os.path.join(WEB_DIR, name)
        if not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, MIME.get(os.path.splitext(path)[1], "application/octet-stream"))

    def _events(self):
        """Server-Sent Events: push the full state whenever it changes."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last = -1
        try:
            while True:
                with GAME.cond:
                    if GAME.version == last:
                        GAME.cond.wait(timeout=15)
                    payload = None
                    if GAME.version != last:
                        last = GAME.version
                        payload = json.dumps(GAME.public(), ensure_ascii=False)
                chunk = f"data: {payload}\n\n" if payload else ": ping\n\n"
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/play"):
            return self._file("play.html")
        if path == "/host":
            return self._file("host.html")
        if path.startswith("/web/"):
            return self._file(path[len("/web/"):])
        if path == "/api/state":
            with GAME.cond:
                return self._send(200, GAME.public())
        if path == "/api/events":
            return self._events()
        if path in ("/keepsake", "/keepsake.md"):
            with GAME.cond:
                fams, stories = list(GAME.settings["families"]), list(GAME.stories)
            if path == "/keepsake":
                return self._send(200, keepsake_html(fams, stories), "text/html; charset=utf-8")
            return self._send(200, keepsake_md(fams, stories), "text/markdown; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="tell-me-more-stories.md"'})
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/api/act":
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 20000:
                raise GameError("Request too large")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise GameError("Bad request")
            self._send(200, GAME.act(data))
        except GameError as e:
            self._send(400, {"error": str(e)})
        except (ValueError, TypeError, KeyError):
            self._send(400, {"error": "Bad request"})


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    global GAME
    ap = argparse.ArgumentParser(description="Tell Me More — a storytelling game server")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host-key", default=None, help="key for the host screen (default: random each run)")
    args = ap.parse_args()

    load_prompts()  # fail fast if prompts.json is broken
    ip = lan_ip()
    join_url = f"http://{ip}:{args.port}"
    key = args.host_key or secrets.token_hex(3)
    GAME = Game(key, join_url)

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    srv.daemon_threads = True
    host_name = socket.gethostname()
    if not host_name.endswith(".local"):
        host_name += ".local"
    print(f"""
  Tell Me More is running

  Tablet (host):   {join_url}/host?key={key}
  Guests' phones:  {join_url}
                   (or http://{host_name}:{args.port})
  Keepsake:        {join_url}/keepsake

  Everyone needs to be on the same Wi-Fi. Ctrl+C to stop.
""")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print(f"\n  Thanks for the stories! Saved in {STORIES_DIR}\n")


if __name__ == "__main__":
    main()
