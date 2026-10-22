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
import re
import secrets
import socket
import sys
import threading
import time
import traceback
import unicodedata
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")
PROMPTS_PATH = os.path.join(ROOT, "prompts.json")
STORIES_DIR = os.path.join(ROOT, "stories")

LEVELS = ["light", "warm", "deeper"]
REACTIONS = ["❤️", "😂", "😮", "👏", "🥲"]
MAX_GROUPS = 6
MAX_PLAYERS = 60
HAND_SIZE = 3  # cards each person holds privately to think about before their turn
MAX_HAND = HAND_SIZE + 1  # unlocking a level can add a 4th card for a while
NAME_LEN = 24
PROMPT_LEN = 300
HOST_TRIES_PER_MIN = 10  # wrong host keys allowed per device per minute
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
}


class GameError(Exception):
    """A friendly message for the person who tapped something."""


def now_ms():
    return int(time.time() * 1000)


def fmt_secs(secs):
    secs = int(secs or 0)
    return f"{secs // 60}:{secs % 60:02d}"


def long_date(ts):
    d = datetime.fromtimestamp(ts)
    return f"{d:%B} {d.day}, {d.year}"  # portable: no %-d, which Windows lacks


def as_int(value, lo, hi, default=None):
    """A number from a client, clamped to [lo, hi]. Bad input -> default, or an error."""
    try:
        f = float(value)
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError
        return max(lo, min(hi, int(f)))
    except (TypeError, ValueError, OverflowError):
        if default is None:
            raise GameError("Expected a number")
        return default


# Characters we strip from the end of a cut string so emoji don't break in half.
_TAIL_JUNK = {"‍", "︎", "️"}


def clean_text(value, limit, words=False):
    """One tidy line of text: no control/invisible characters, collapsed spaces,
    cut at `limit` without leaving a broken emoji (or mid-word, with `words`)."""
    out = []
    for ch in str(value if value is not None else ""):
        cat = unicodedata.category(ch)
        if cat == "Cc":
            out.append(" ")
        elif cat == "Cf" and ch != "‍":  # keep zero-width joiner (emoji sequences)
            continue
        else:
            out.append(ch)
    s = re.sub(r"\s+", " ", "".join(out)).strip()
    if len(s) > limit:
        s = s[:limit]
        if words and " " in s[limit // 2:]:
            s = s[: s.rindex(" ")].rstrip(",.;:—- ") + "…"
        while s and (s[-1] in _TAIL_JUNK or unicodedata.category(s[-1]) == "Mn"):
            s = s[:-1]
        s = s.strip()
    return s


def visible(s):
    return any(unicodedata.category(ch)[0] not in "ZC" for ch in s)


# ---------- prompts ----------

def parse_prompts(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("prompts.json should be an object with \"light\", \"warm\" and \"deeper\" lists")
    out = {}
    for lvl in LEVELS:
        items = data.get(lvl, [])
        if not isinstance(items, list):
            raise ValueError(f'"{lvl}" in prompts.json should be a list')
        cleaned = [clean_text(p, PROMPT_LEN) for p in items if isinstance(p, str)]
        out[lvl] = list(dict.fromkeys(p for p in cleaned if p))  # drop blanks and duplicates
    if not out["light"]:
        raise ValueError('prompts.json needs at least one "light" prompt')
    return out


class PromptStore:
    """prompts.json, re-read whenever it changes on disk. If an edit breaks it
    mid-party, keep playing with the last good version and say so once."""

    def __init__(self, path):
        self.path = path
        self.data = None
        self.mtime = None
        self.warned_for = None

    def get(self):
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except OSError as e:
            if self.data is None:
                raise
            self._warn(("missing", None), f"can't find prompts.json ({e.strerror})")
            return self.data
        if mtime != self.mtime:
            try:
                self.data = parse_prompts(self.path)
                self.mtime = mtime
                self.warned_for = None
            except (OSError, ValueError) as e:
                if self.data is None:
                    raise
                self._warn(mtime, str(e))
        return self.data

    def _warn(self, tag, msg):
        if self.warned_for != tag:
            self.warned_for = tag
            print(f"  ⚠  {msg} — still using the last good prompts until it's fixed.", flush=True)


PROMPTS = PromptStore(PROMPTS_PATH)


# ---------- the game ----------

class Game:
    """All game state lives here, guarded by one lock. Every change bumps
    `version` and wakes the live-update streams."""

    def __init__(self, host_key, join_url):
        self.cond = threading.Condition()  # re-entrant (RLock inside)
        self.version = 0
        self.host_key = host_key
        self.join_url = join_url
        self.key_fails = {}  # ip -> recent wrong-key timestamps
        self.settings = {
            "story_secs": 150,
            "question_secs": 180,
            "max_level": 1,  # 1 = light, 2 = light + warm, 3 = everything
            "allow_deeper": False,  # Deeper cards are hidden unless the host opts in
            "families": ["Everyone"],  # one group = no families; add up to MAX_GROUPS in Settings
        }
        self.players = {}  # public id -> player dict (includes the secret token)
        self.tokens = {}  # secret token -> public id
        self.order = []  # join order
        self.reactions = []
        self.reaction_n = 0
        self.last_react = {}
        os.makedirs(STORIES_DIR, exist_ok=True)
        self._new_session()

    # ---------- helpers ----------

    def _new_session(self):
        self.started = time.time()
        self.session_path = os.path.join(STORIES_DIR, time.strftime("%Y-%m-%d_%H%M%S") + ".json")
        self.stories = []
        self.phase = "lobby"  # lobby | ready | card | telling | questions
        self.teller = None
        self.partner = None  # someone telling the story together with the teller
        self.prompt = None
        self.prompt_from = None  # "hand" | "deck" | "custom"
        self.custom_draft = None  # a host-written prompt that was put back, to edit or reuse
        self.used = set()  # prompts dealt or drawn since the last reshuffle
        self.cards = {}  # player id -> private hand of prompt cards
        self.timer = None
        self.asker = None
        self.current = None  # the story being told, until it's saved
        self.saved_phase = None  # the turn that "Show join screen" set aside
        self.lobby_paused = False  # whether "Show join screen" paused a running timer
        self.turn_no = 0
        self.group_last_turn = {}  # group index -> turn number it last told a story

    def _bump(self):
        self.version += 1
        self.cond.notify_all()

    @staticmethod
    def _need(ok, msg="Only the host can do that"):
        if not ok:
            raise GameError(msg)

    def _turn_phase(self):
        """The phase of the current turn, even while the join screen is showing."""
        return self.saved_phase if self.phase == "lobby" and self.saved_phase else self.phase

    def _is_teller(self, me):
        return me is not None and me["id"] == self.teller

    def _is_telling(self, me):
        """The teller, or the person telling it with them."""
        return me is not None and me["id"] in (self.teller, self.partner)

    def host_ok(self, key, ip):
        """Check a host key, with a limit on wrong guesses per device."""
        if key in (None, ""):
            return False
        with self.cond:
            now = time.time()
            fails = [t for t in self.key_fails.get(ip, []) if now - t < 60]
            self.key_fails[ip] = fails
            self._need(len(fails) < HOST_TRIES_PER_MIN, "Too many wrong host keys. Wait a minute and try again.")
            ok = secrets.compare_digest(str(key).encode(), self.host_key.encode())
            if not ok:
                fails.append(now)
            return ok

    def _add(self, name, family, device):
        self._need(len(self.players) < MAX_PLAYERS, "The game is full")
        name = clean_text(name, NAME_LEN)
        self._need(visible(name), "Please enter a name")
        self._need(all(p["name"].casefold() != name.casefold() for p in self.players.values()),
                   f"Someone called {name} is already here — add an initial, like “{name} K.”")
        family = as_int(family, 0, len(self.settings["families"]) - 1, default=0)
        hand = []
        self._top_up(hand)  # deal before adding anyone, so a failure leaves no half-joined player
        pid, token = secrets.token_urlsafe(6), secrets.token_urlsafe(16)
        self.players[pid] = {
            "id": pid, "token": token, "name": name, "family": family,
            "told": 0,  # stories told (alone or together)
            "turns": 0,  # stories told + turns passed: everyone gets a turn before anyone gets two
            "asked": 0,
            "hand": None,  # when their hand went up (raised hand, not cards)
            "device": device,
            # False when they joined before families existed; their phone will ask
            "family_set": len(self.settings["families"]) > 1,
        }
        self.tokens[token] = pid
        self.cards[pid] = hand
        self.order.append(pid)
        return pid, token

    def _pick_next(self):
        """Everyone gets a turn before anyone gets two (passing counts as your turn
        for this round); among those, the group that told least recently goes next."""
        if not self.players:
            return None
        people = [self.players[i] for i in self.order]
        low = min(p["turns"] for p in people)
        cands = ([p for p in people if p["turns"] == low and p["id"] != self.teller]
                 or [p for p in people if p["id"] != self.teller] or people)

        def recency(p):
            return self.group_last_turn.get(p["family"], -1)
        best = min(recency(c) for c in cands)
        return next(c for c in cands if recency(c) == best)["id"]

    def _begin_turn(self, pid):
        self.teller = pid
        self.partner = None
        self.prompt = None
        self.prompt_from = None
        self.timer = None
        self.asker = None
        self.current = None
        self.saved_phase = None
        self.lobby_paused = False
        self.phase = "ready" if pid else "lobby"

    # ---------- cards ----------

    def _deal_one(self, hand, level=None, exclude=()):
        """Next card from the deck: one nobody has seen if possible, else one nobody
        is holding, else anything not in this hand (or on the table)."""
        prompts = PROMPTS.get()
        levels = [level] if level else LEVELS[: self.settings["max_level"]]
        pool = [(t, lvl) for lvl in levels for t in prompts[lvl]]
        mine = {c["text"] for c in hand} | set(exclude)
        if self.prompt:
            mine.add(self.prompt["text"])
        held = {c["text"] for h in self.cards.values() for c in h} | mine
        fresh = []
        for ok in (lambda t: t not in self.used and t not in held,
                   lambda t: t not in held,
                   lambda t: t not in mine):
            fresh = [x for x in pool if ok(x[0])]
            if fresh:
                break
        if not fresh:
            return None
        text, lvl = random.choice(fresh)
        self.used.add(text)
        return {"text": text, "level": lvl}

    def _top_up(self, hand, exclude=()):
        """Fill a hand back up to HAND_SIZE, always keeping at least one Light
        card so there's an easy option even after deeper levels are unlocked."""
        while len(hand) < HAND_SIZE:
            need_light = not any(c["level"] == "light" for c in hand)
            card = (self._deal_one(hand, "light", exclude) if need_light else None) or self._deal_one(hand, None, exclude)
            if not card:
                break
            hand.append(card)

    def _fill(self, pid):
        self._top_up(self.cards.setdefault(pid, []))

    def _card_on_table(self):
        return self._turn_phase() == "card" and self.prompt_from == "hand"

    def _rebalance_hands(self, old, new):
        """Depth changed. Going up: everyone gets one card from the new level as an
        extra card (nobody loses the card they were planning to tell). Going down:
        cards that are now too deep are replaced."""
        if new < old:
            allowed = set(LEVELS[:new])
            for hand in self.cards.values():
                hand[:] = [c for c in hand if c["level"] in allowed]
                self._top_up(hand)
            if (self._turn_phase() == "card" and self.prompt
                    and self.prompt["level"] in LEVELS and self.prompt["level"] not in allowed):
                self.prompt, self.prompt_from = None, None  # too deep now: back to choosing
                if self.phase == "lobby":
                    self.saved_phase = "ready"
                else:
                    self.phase = "ready"
        elif new > old:
            top = LEVELS[new - 1]
            for pid, hand in self.cards.items():
                if pid == self.teller and self._card_on_table():
                    continue  # their chosen card comes back if they change their mind
                if len(hand) >= MAX_HAND or any(c["level"] == top for c in hand):
                    continue
                card = self._deal_one(hand, level=top)
                if card:
                    hand.append(card)

    def _return_prompt(self):
        """An un-told card goes back where it came from."""
        p = self.prompt
        if p and self.prompt_from == "hand" and self.teller in self.cards:
            hand = self.cards[self.teller]
            if (p["level"] in LEVELS[: self.settings["max_level"]] and len(hand) < MAX_HAND
                    and all(c["text"] != p["text"] for c in hand)):
                hand.append({"text": p["text"], "level": p["level"]})
        elif p and self.prompt_from == "custom":
            self.custom_draft = p["text"]
        self.prompt = None
        self.prompt_from = None

    def _card_index(self, hand, d):
        """Which card was tapped: by its text (robust if the hand changed), else by index."""
        text = d.get("text")
        if isinstance(text, str):
            for i, c in enumerate(hand):
                if c["text"] == text:
                    return i
            raise GameError("That card isn't in the hand any more")
        i = as_int(d.get("index"), -1, 99, default=-1)
        self._need(0 <= i < len(hand), "That card isn't in the hand")
        return i

    # ---------- timer ----------

    def _start_timer(self, secs):
        n = now_ms()
        self.timer = {"total": secs * 1000, "ends_at": n + secs * 1000, "paused_left": None,
                      "started_at": n, "paused_ms": 0, "paused_at": None}

    def _remaining(self):
        t = self.timer
        if not t:
            return 0
        if t["ends_at"] is None:
            return t["paused_left"]
        return max(0, t["ends_at"] - now_ms())

    def _pause_timer(self):
        t = self.timer
        if t and t["ends_at"] is not None:
            t["paused_left"] = self._remaining()
            t["ends_at"] = None
            t["paused_at"] = now_ms()
            return True
        return False

    def _resume_timer(self):
        t = self.timer
        if t and t["ends_at"] is None:
            n = now_ms()
            t["ends_at"] = n + t["paused_left"]
            t["paused_left"] = None
            t["paused_ms"] += n - (t["paused_at"] or n)
            t["paused_at"] = None

    def _elapsed_secs(self):
        """Real talking time, including any overtime, minus pauses."""
        t = self.timer
        if not t:
            return 0
        n = now_ms()
        paused = t["paused_ms"] + ((n - t["paused_at"]) if t["paused_at"] else 0)
        return max(0, round((n - t["started_at"] - paused) / 1000))

    # ---------- stories ----------

    def _record_story(self):
        """Save the story being told to the keepsake and count it for the tellers."""
        if not self.current:
            return
        if "told_secs" not in self.current:
            self.current["told_secs"] = self._elapsed_secs()
        for pid in (self.teller, self.partner):
            p = self.players.get(pid)
            if p:
                p["told"] += 1
                p["turns"] += 1
        self.current["n"] = len(self.stories) + 1
        self.stories.append(self.current)
        self.current = None
        t = self.players.get(self.teller)
        if t:
            self.turn_no += 1
            self.group_last_turn[t["family"]] = self.turn_no
        self._save()

    def _end_turn(self):
        """Wrap up this turn (told or passed) and move to the next storyteller."""
        for p in self.players.values():
            p["hand"] = None
        if self.teller in self.players:
            self._fill(self.teller)
        self._begin_turn(self._pick_next())

    def _save(self):
        try:
            tmp = self.session_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"started": long_date(self.started), "families": self.settings["families"],
                           "stories": self.stories}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.session_path)
        except OSError as e:
            print("  ⚠  couldn't save stories:", e, flush=True)

    # ---------- public view ----------

    def public(self, viewer=None, is_host=False):
        order = [self.players[i] for i in self.order if i in self.players]
        teller = self.players.get(self.teller)
        tf = teller["family"] if teller else -1
        queue = [p for p in order if p["hand"] and p["id"] not in (self.teller, self.partner)]
        # People from other families ask first, then people who haven't asked much yet.
        queue.sort(key=lambda p: (p["family"] == tf, p["asked"], p["hand"]))
        timer = None
        if self.timer:
            timer = {k: self.timer[k] for k in ("total", "ends_at", "paused_left")}
        return {
            "v": self.version,
            "server_now": now_ms(),
            "phase": self.phase,
            "paused_turn": self.phase == "lobby" and bool(self.saved_phase),
            "settings": self.settings,
            "players": [{k: v for k, v in p.items() if k != "token"} for p in order],
            "me": viewer if viewer in self.players else None,
            "teller": self.teller,
            "partner": self.partner,
            "prompt": self.prompt,
            "timer": timer,
            "asker": self.asker,
            "queue": [p["id"] for p in queue],
            "story_no": len(self.stories) + 1,
            "stories": len(self.stories),
            "reactions": self.reactions[-15:],
            "join_url": self.join_url,
            "my_cards": self.cards.get(viewer, []) if viewer in self.players else [],
            "teller_cards": self.cards.get(self.teller, []) if (is_host or viewer == self.teller) else [],
            "custom_draft": self.custom_draft if is_host else None,
        }

    def public_json(self, token=None, key=None, ip=""):
        """The state for one viewer, as JSON text (built under the lock, sent outside it)."""
        is_host = self.host_ok(key, ip) if key else False
        with self.cond:
            viewer = self.tokens.get(token or "")
            return json.dumps(self.public(viewer, is_host), ensure_ascii=False)

    # ---------- actions ----------

    def act(self, data, ip=""):
        action = str(data.get("action", ""))
        with self.cond:
            is_host = self.host_ok(data.get("key"), ip)
            if action == "check_host":
                return {"ok": is_host}
            me = self.players.get(self.tokens.get(str(data.get("token") or ""), ""))
            fn = getattr(self, "_a_" + action, None)
            self._need(fn is not None, "Unknown action")
            out = fn(data, is_host, me) or {}
            self._bump()
            return {"ok": True, **out}

    def _a_join(self, d, host, me):
        pid, token = self._add(d.get("name"), d.get("family"), device=True)
        return {"player_id": pid, "token": token}

    def _a_add_player(self, d, host, me):
        self._need(host)
        pid, _ = self._add(d.get("name"), d.get("family"), device=False)
        return {"player_id": pid}

    def _a_remove_player(self, d, host, me):
        pid = str(d.get("target") or "")
        self._need(host or (me is not None and me["id"] == pid))
        if pid not in self.players:
            return
        was_teller = pid == self.teller
        if was_teller and self._turn_phase() in ("telling", "questions"):
            self._record_story()  # they told it: keep it in the keepsake
        if pid == self.partner:
            self.partner = None
        p = self.players.pop(pid)
        self.tokens.pop(p["token"], None)
        self.order.remove(pid)
        self.cards.pop(pid, None)
        self.last_react.pop(pid, None)
        if self.asker == pid:
            self.asker = None
        if was_teller:
            self.prompt, self.prompt_from = None, None
            if self.phase == "lobby":
                self._begin_turn(None)  # the set-aside turn is gone; Start picks someone new
            else:
                self._end_turn()

    def _a_settings(self, d, host, me):
        self._need(host)
        s = self.settings
        # Check everything first, then apply, so a bad value changes nothing.
        new = {}
        if "story_secs" in d:
            new["story_secs"] = as_int(d["story_secs"], 30, 900)
        if "question_secs" in d:
            new["question_secs"] = as_int(d["question_secs"], 30, 900)
        if "allow_deeper" in d:
            self._need(isinstance(d["allow_deeper"], bool), "Bad request")
            new["allow_deeper"] = d["allow_deeper"]
        allow_deeper = new.get("allow_deeper", s["allow_deeper"])
        if "max_level" in d:
            new["max_level"] = as_int(d["max_level"], 1, 3 if allow_deeper else 2)
        elif not allow_deeper and s["max_level"] > 2:
            new["max_level"] = 2  # Deeper was just switched off
        remap = None
        if "families" in d:
            raw = d["families"]
            self._need(isinstance(raw, list) and all(isinstance(n, str) for n in raw), "Family names should be a list")
            names = [clean_text(n, NAME_LEN) for n in raw[:MAX_GROUPS]]
            self._need(names, "Keep at least one group")
            names = [n if visible(n) else f"Family {chr(65 + i)}" for i, n in enumerate(names)]
            # slots[new_index] = old index (or None for a new family); defaults to same positions
            slots = d.get("slots")
            slots = list(slots)[: len(names)] if isinstance(slots, list) else list(range(len(names)))
            if len(names) == 1:  # turning families off merges everyone into one group
                remap = {i: 0 for i in range(len(s["families"]))}
            else:
                remap = {}
                for new_i, old_i in enumerate(slots):
                    if type(old_i) is int and 0 <= old_i < len(s["families"]) and old_i not in remap:
                        remap[old_i] = new_i
            for p in self.players.values():
                self._need(p["family"] in remap,
                           f"Move or remove the people in {s['families'][p['family']]} before removing it")
            new["families"] = names

        old_level = s["max_level"]
        for k in ("story_secs", "question_secs", "allow_deeper", "max_level"):
            if k in new:
                s[k] = new[k]
        if s["max_level"] != old_level:
            self._rebalance_hands(old_level, s["max_level"])
        if remap is not None:
            for p in self.players.values():
                p["family"] = remap[p["family"]]
            self.group_last_turn = {remap[k]: v for k, v in self.group_last_turn.items() if k in remap}
            if len(new["families"]) == 1:  # back to no families: ask again if families come back
                for p in self.players.values():
                    p["family_set"] = False
            s["families"] = new["families"]

    def _a_start(self, d, host, me):
        self._need(host)
        self._need(self.players, "Add at least one person first")
        if self.phase != "lobby":
            return
        if self.saved_phase and self.teller in self.players:
            self.phase, self.saved_phase = self.saved_phase, None
            if self.lobby_paused:
                self._resume_timer()
            self.lobby_paused = False
        else:
            self._begin_turn(self._pick_next())

    def _a_lobby(self, d, host, me):
        """Back to the join screen (e.g. a late arrival). Sets the current turn aside."""
        self._need(host)
        if self.phase == "lobby":
            return
        self.lobby_paused = self._pause_timer()
        self.saved_phase = self.phase
        self.phase = "lobby"

    def _a_reset(self, d, host, me):
        self._need(host)
        for p in self.players.values():
            p.update(told=0, turns=0, asked=0, hand=None)
        self._new_session()
        for pid in self.order:
            self._fill(pid)

    def _a_pick_teller(self, d, host, me):
        self._need(host)
        pid = str(d.get("target") or "")
        self._need(pid in self.players, "Unknown person")
        self._need(self._turn_phase() not in ("telling", "questions"), "Finish or skip the current story first")
        if self._turn_phase() == "card":
            self._return_prompt()
        self._begin_turn(pid)

    def _a_play_card(self, d, host, me):
        """The storyteller picks one of their cards."""
        self._need(host or self._is_teller(me), "It's not your turn yet")
        self._need(self.phase == "ready", "A card is already chosen")
        hand = self.cards.get(self.teller, [])
        self.prompt = hand.pop(self._card_index(hand, d))
        self.prompt_from = "hand"
        self.phase = "card"

    def _a_draw(self, d, host, me):
        """Surprise me: a blind card from the deck."""
        self._need(host or self._is_teller(me), "It's not your turn yet")
        self._need(self.phase == "ready", "A card is already chosen")
        card = self._deal_one(self.cards.get(self.teller, []))
        self._need(card, "No prompts found — check prompts.json")
        self.prompt, self.prompt_from = card, "deck"
        self.phase = "card"

    def _a_unplay(self, d, host, me):
        """Pick a different card: back to choosing, hand card returned."""
        self._need(host or self._is_teller(me), "It's not your turn")
        self._need(self.phase == "card", "Nothing to put back")
        self._return_prompt()
        self.phase = "ready"

    def _a_swap_card(self, d, host, me):
        """Trade one of your cards for a new one, any time (the host can do it for anyone)."""
        pid = str(d.get("target") or "") if host and d.get("target") else (me["id"] if me else "")
        self._need(pid in self.players, "Join the game first")
        hand = self.cards.setdefault(pid, [])
        i = self._card_index(hand, d)
        old = hand.pop(i)
        before = len(hand)
        self._top_up(hand, exclude=[old["text"]])
        if len(hand) == before:  # the deck had nothing else: keep the old card
            hand.insert(i, old)
            raise GameError("No other cards left to swap in")
        hand.insert(i, hand.pop())  # new card takes the old one's place

    def _a_pass_turn(self, d, host, me):
        """'Come back to me later': no story this time; they go to the end of the round."""
        self._need(host or self._is_teller(me), "It's not your turn")
        self._need(self._turn_phase() in ("ready", "card"), "The story has already started")
        if self._turn_phase() == "card":
            self._return_prompt()
        t = self.players.get(self.teller)
        if t:
            t["turns"] += 1
        self._end_turn()

    def _a_set_partner(self, d, host, me):
        """'Tell it with someone': a couple or a parent and child share one story."""
        self._need(host or self._is_teller(me), "It's not your turn")
        self._need(self.phase in ("ready", "card"), "Choose before the story starts")
        pid = d.get("target")
        if not pid:
            self.partner = None
            return
        self._need(pid in self.players and pid != self.teller, "Can't tell it with that person")
        self.partner = pid
        self.players[pid]["hand"] = None

    def _a_custom_prompt(self, d, host, me):
        self._need(host)
        self._need(self.phase in ("ready", "card"), "Finish this story first")
        text = clean_text(d.get("text"), PROMPT_LEN, words=True)
        self._need(visible(text), "Write a prompt first")
        self._return_prompt()
        self.custom_draft = None
        self.prompt = {"text": text, "level": "custom"}
        self.prompt_from = "custom"
        self.phase = "card"

    def _a_start_story(self, d, host, me):
        self._need(host or self._is_telling(me), "It's not your turn yet")
        self._need(self.phase == "card", "Choose a card first")
        t = self.players[self.teller]
        partner = self.players.get(self.partner)
        self.current = {
            "teller": t["name"] + (f" & {partner['name']}" if partner else ""),
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
        self._need(host or self._is_telling(me))
        self._need(self._pause_timer(), "Timer isn't running")

    def _a_resume(self, d, host, me):
        self._need(host or self._is_telling(me))
        self._need(self.timer and self.timer["ends_at"] is None, "Timer isn't paused")
        self._resume_timer()

    def _a_add_time(self, d, host, me):
        self._need(host or self._is_telling(me))
        t = self.timer
        self._need(t, "No timer running")
        self._need(self._remaining() <= 15 * 60000, "That's plenty of time")
        if t["ends_at"] is None:
            t["paused_left"] += 30000
            t["total"] += 30000
        elif t["ends_at"] <= now_ms():  # time was up: a fresh 30 seconds
            t["ends_at"] = now_ms() + 30000
            t["total"] = 30000
        else:
            t["ends_at"] += 30000
            t["total"] += 30000

    def _a_end_story(self, d, host, me):
        self._need(host or self._is_telling(me), "It's not your turn")
        self._need(self.phase == "telling", "No story in progress")
        self.current["told_secs"] = self._elapsed_secs()
        self.phase = "questions"
        self.asker = None
        self._start_timer(self.settings["question_secs"])

    def _a_set_family(self, d, host, me):
        """Host moves anyone; a guest can set their own family."""
        pid = str(d.get("target") or (me["id"] if me else ""))
        self._need(host or (me is not None and me["id"] == pid), "Only the host can move other people")
        self._need(pid in self.players, "Unknown person")
        fam = as_int(d.get("family"), -1, 99)
        self._need(0 <= fam < len(self.settings["families"]), "Unknown family")
        p = self.players[pid]
        p["family"] = fam
        p["family_set"] = True

    def _a_raise_hand(self, d, host, me):
        self._need(me, "Join the game first")
        self._need(not self._is_telling(me), "You're telling this one!")
        self._need(self.phase in ("telling", "questions"), "Hands go up during a story")
        if me["hand"] is None:
            me["hand"] = now_ms()

    def _a_lower_hand(self, d, host, me):
        self._need(me, "Join the game first")
        me["hand"] = None

    def _a_call_on(self, d, host, me):
        self._need(host or self._is_telling(me))
        self._need(self.phase == "questions" and self.current, "Questions come after the story")
        pid = str(d.get("target") or "")
        self._need(pid in self.players and pid not in (self.teller, self.partner), "Can't call on that person")
        p = self.players[pid]
        p["hand"] = None
        p["asked"] += 1
        self.asker = pid
        self.current["questions"].append(p["name"])

    def _a_done_asking(self, d, host, me):
        self._need(host or self._is_telling(me) or (me is not None and me["id"] == self.asker))
        self.asker = None

    def _a_next_turn(self, d, host, me):
        self._need(host or (self._is_telling(me) and self.phase == "questions"))
        self._need(self.phase != "lobby", "Start the game first")
        if self.phase in ("ready", "card"):
            return self._a_pass_turn(d, host, me)  # "Come back to them later"
        self._record_story()
        self._end_turn()

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

def story_meta(s):
    parts = []
    if s.get("family"):
        parts.append(s["family"])
    if s.get("told_secs", 0) >= 5:
        parts.append(fmt_secs(s["told_secs"]))
    if s["questions"]:
        parts.append("questions from " + ", ".join(dict.fromkeys(s["questions"])))
    return parts


def keepsake_html(families, stories, started):
    items = []
    for s in stories:
        meta = "".join(f'<span class="kbit">{escape(m)}</span>' for m in story_meta(s))
        items.append(
            f'<li class="kstory"><div class="knum">{s["n"]}</div><div>'
            f'<p class="kprompt">{escape(s["prompt"])}</p>'
            f'<div class="kmeta"><span class="chip fam-{s["family_index"]}">{escape(s["teller"])}</span>{meta}</div>'
            f'</div></li>')
    body = "".join(items) or '<li class="hint">No stories yet — they appear here as each turn finishes.</li>'
    fams = escape(" · ".join(families)) + " — " if len(families) > 1 else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Our stories · Tell Me More</title>
<link rel="stylesheet" href="/web/app.css"></head><body class="keepsake">
<header><div class="brand-big">Our stories</div>
<p class="hint">{fams}{long_date(started)}</p>
<p><a class="btn" href="/keepsake.md">Download as text</a></p></header>
<ol class="klist">{body}</ol></body></html>"""


def keepsake_md(families, stories, started):
    fams = ", ".join(families) + " · " if len(families) > 1 else ""
    lines = ["# Tell Me More — our stories", f"_{fams}{long_date(started)}_", ""]
    for s in stories:
        lines.append(f"## {s['n']}. {s['prompt']}")
        lines.append(" · ".join([f"**{s['teller']}**"] + story_meta(s)))
        lines.append("")
    return "\n".join(lines)


# ---------- HTTP ----------

GAME = None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64  # all the phones reconnect at once after a Wi-Fi blip


class Handler(BaseHTTPRequestHandler):
    server_version = "TellMeMore/1.0"
    timeout = 30  # drop connections that go quiet mid-request

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
            pass  # the phone went away; nothing to do

    def _file(self, name):
        name = os.path.normpath(name)
        if name.startswith("..") or os.path.isabs(name) or "\x00" in name:
            return self._send(404, {"error": "not found"})
        path = os.path.join(WEB_DIR, name)
        if not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, MIME.get(os.path.splitext(path)[1], "application/octet-stream"))

    def _query(self):
        q = parse_qs(urlparse(self.path).query)
        return (q.get("token") or [""])[0], (q.get("key") or [""])[0]

    def _events(self):
        """Server-Sent Events: push the state whenever it changes. Each viewer
        also gets their own private cards (and the host, the storyteller's)."""
        token, key = self._query()
        ip = self.client_address[0]
        try:
            GAME.public_json(token, key, ip)  # check the key once (rate-limited)
        except GameError as e:
            return self._send(429, {"error": str(e)})
        is_host = bool(key) and secrets.compare_digest(key.encode(), GAME.host_key.encode())
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
                        viewer = GAME.tokens.get(token)
                        payload = json.dumps(GAME.public(viewer, is_host), ensure_ascii=False)
                chunk = f"data: {payload}\n\n" if payload else ": ping\n\n"
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (OSError, socket.timeout):
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
            token, key = self._query()
            try:
                body = GAME.public_json(token, key, self.client_address[0])
            except GameError as e:
                return self._send(429, {"error": str(e)})
            return self._send(200, body)
        if path == "/api/events":
            return self._events()
        if path in ("/keepsake", "/keepsake.md"):
            with GAME.cond:
                fams, stories, started = list(GAME.settings["families"]), list(GAME.stories), GAME.started
            if path == "/keepsake":
                return self._send(200, keepsake_html(fams, stories, started), "text/html; charset=utf-8")
            return self._send(200, keepsake_md(fams, stories, started), "text/markdown; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="tell-me-more-stories.md"'})
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/api/act":
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= 20000:
                raise GameError("Bad request")
            raw = self.rfile.read(length) if length else b""
            data = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise GameError("Bad request")
            code, body = 200, GAME.act(data, self.client_address[0])
        except GameError as e:
            code, body = 400, {"error": str(e)}
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError):
            code, body = 400, {"error": "Bad request"}
        except (OSError, socket.timeout):
            return
        except Exception:  # a bug: say so, keep the game running
            traceback.print_exc()
            code, body = 500, {"error": "Something went wrong — try again"}
        self._send(code, body)


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

    try:
        PROMPTS.get()  # fail fast if prompts.json is broken at startup
    except (OSError, ValueError) as e:
        sys.exit(f"Can't start: prompts.json has a problem — {e}")
    ip = lan_ip()
    join_url = f"http://{ip}:{args.port}"
    key = args.host_key or secrets.token_urlsafe(9)
    GAME = Game(key, join_url)

    srv = Server(("0.0.0.0", args.port), Handler)
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
""", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print(f"\n  Thanks for the stories! Saved in {STORIES_DIR}\n")


if __name__ == "__main__":
    main()
