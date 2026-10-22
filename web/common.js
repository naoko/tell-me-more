// Shared helpers for the host (tablet) and player (phone) screens.
const TellMeMore = (() => {
  let offset = 0; // server clock minus local clock

  function setConn(ok) {
    document.body.classList.toggle('offline', !ok);
  }

  // query() returns who is watching (?token=… or ?key=…) so the server can
  // include that viewer's private cards. Call reopen() after it changes.
  function connect(onState, query = () => '') {
    let es;
    const open = () => {
      es = new EventSource('/api/events' + query());
      es.onopen = () => setConn(true);
      es.onmessage = (e) => {
        const s = JSON.parse(e.data);
        offset = s.server_now - Date.now();
        setConn(true);
        onState(s);
      };
      es.onerror = () => {
        setConn(false);
        if (es.readyState === EventSource.CLOSED) setTimeout(open, 2000);
      };
    };
    open();
    // Phones drop the connection when the screen sleeps; reconnect on wake.
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && es.readyState === EventSource.CLOSED) open();
    });
    return { reopen() { es.close(); open(); } };
  }

  async function act(action, extra = {}) {
    let res;
    try {
      res = await fetch('/api/act', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, ...extra }),
      });
    } catch (_) {
      throw new Error("Can't reach the game. Check you're on the home Wi-Fi.");
    }
    let body = {};
    try { body = await res.json(); } catch (_) { /* ignore */ }
    if (!res.ok) throw new Error(body.error || 'Something went wrong');
    return body;
  }

  const now = () => Date.now() + offset;

  function remaining(timer) {
    if (!timer) return 0;
    if (timer.ends_at == null) return timer.paused_left;
    return Math.max(0, timer.ends_at - now());
  }

  function fmt(ms) {
    const s = Math.max(0, Math.ceil(ms / 1000));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  let toastTimer;
  function toast(msg) {
    let el = document.getElementById('toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'toast';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
  }
  function hideToast() {
    const el = document.getElementById('toast');
    if (el) el.classList.remove('show');
  }

  // A soft two-note bell — a nudge, not a buzzer.
  let ctx;
  function audio() {
    if (!ctx) {
      const C = window.AudioContext || window.webkitAudioContext;
      if (!C) return null;
      ctx = new C();
    }
    if (ctx.state === 'suspended') ctx.resume();
    return ctx;
  }
  function chime(soft = false) {
    const c = audio();
    if (!c) return;
    const t0 = c.currentTime + 0.02;
    const peak = soft ? 0.10 : 0.22;
    [[659.25, 0], [987.77, 0.22]].forEach(([freq, dt]) => {
      [1, 2.76].forEach((harm, i) => {
        const o = c.createOscillator();
        const g = c.createGain();
        o.type = 'sine';
        o.frequency.value = freq * harm;
        const p = i ? peak * 0.15 : peak;
        g.gain.setValueAtTime(0.0001, t0 + dt);
        g.gain.exponentialRampToValueAtTime(p, t0 + dt + 0.015);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + dt + (i ? 0.8 : 2.4));
        o.connect(g).connect(c.destination);
        o.start(t0 + dt);
        o.stop(t0 + dt + 2.5);
      });
    });
  }
  // Browsers only allow sound after a tap; unlock on the first one.
  document.addEventListener('click', () => audio(), { once: true });

  // Floating emoji reactions. Returns a function to call with each new state;
  // reactions that already existed when the page loaded are not replayed.
  function reactionLayer(container, { gutters = false } = {}) {
    let last = null;
    return (s) => {
      if (last === null) { last = Math.max(0, ...s.reactions.map((r) => r.n)); return; }
      for (const r of s.reactions) {
        if (r.n <= last) continue;
        last = r.n;
        const el = document.createElement('div');
        el.className = `float fam-${r.family}`;
        const x = Math.random();
        el.style.left = gutters ? `${x < 0.5 ? 1 + x * 22 : 77 + (x - 0.5) * 30}%` : `${4 + x * 80}%`;
        el.innerHTML = `<span class="e">${esc(r.e)}</span><span class="who">${esc(r.name)}</span>`;
        container.appendChild(el);
        setTimeout(() => el.remove(), 3600);
      }
    };
  }

  const LEVEL_LABEL = { light: 'Light', warm: 'Warm', deeper: 'Deeper', custom: 'Just for tonight' };

  function cardHTML(prompt, flip) {
    return `<div class="card face lvl-${prompt.level}${flip ? ' flip-in' : ''}">
      <span class="lvl-tag">${LEVEL_LABEL[prompt.level] || ''}</span>
      <p class="prompt">${esc(prompt.text)}</p></div>`;
  }

  // A small card. With `play`, tapping it chooses it; with `swap`, a ↻ button trades it in.
  function handCardHTML(card, index, { play = false, swap = false } = {}) {
    const tag = play ? 'button' : 'div';
    const attrs = play ? ` data-play="${index}"` : '';
    const inner = `<${tag} class="hand-card lvl-${card.level}"${attrs}><span class="lvl-tag">${LEVEL_LABEL[card.level] || ''}</span>${esc(card.text)}</${tag}>`;
    if (!swap) return inner;
    return `<div class="hand-item">${inner}<button class="swap" data-swap="${index}" aria-label="Swap this card for a new one" title="Swap for a new card">↻</button></div>`;
  }

  function miniCardHTML(prompt) {
    return `<div class="mini-card lvl-${prompt.level}"><span class="lvl-tag">${LEVEL_LABEL[prompt.level] || ''}</span>${esc(prompt.text)}</div>`;
  }

  return { connect, act, remaining, fmt, esc, toast, hideToast, chime, cardHTML, miniCardHTML, handCardHTML, reactionLayer, LEVEL_LABEL };
})();
