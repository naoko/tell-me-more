# Tell Me More — a story game for gatherings

Draw a card, tell a short true story against a gentle timer, then take questions.
The tablet is the shared table screen; everyone else joins from their phone.

No installs: Python 3.8+ standard library only.

![The tablet during questions, with three families](docs/tablet.png)

![Phone screens: joining, telling, raising a hand, taking questions](docs/phones.png)

## Run it

```bash
cd tell-me-more
make run            # or: python3 server.py
```

`make run` uses [uv](https://docs.astral.sh/uv/) if it's installed (pinned Python 3.12) and plain `python3` otherwise. There are no dependencies, so no virtualenv is needed.

It prints three addresses:

```
Tablet (host):   http://192.168.1.20:8765/host?key=a1b2c3
Guests' phones:  http://192.168.1.20:8765
Keepsake:        http://192.168.1.20:8765/keepsake
```

1. Open the **host** link on the tablet. Tip: in Safari, Share → Add to Home Screen gives you full-screen.
2. Optional: open ⚙︎ Settings to change the story length or add families (see below).
3. Guests scan the QR code on the tablet (or type the address) and type their name.
4. For anyone who'd rather not use a phone, use "Add someone without a phone" on the tablet; the host taps for them.

The host key changes each run. To keep the host link the same so you can bookmark it on the tablet, use `make run KEY=something` (or `--host-key something`). Use `PORT=9000` (or `--port 9000`) to change the port.

## How a turn works

1. **Ready.** The next storyteller is picked automatically. Everyone tells once before anyone tells twice. If you've added families, turns also rotate between them. Tap any name to pick someone else instead.
2. **Draw.** The storyteller taps the card, on their phone or on the tablet. They get **one free swap** per game. The host can swap any time, or write a one-off prompt ("Tell us about the proposal!").
3. **Tell.** A ring timer counts down (2½ min by default). It turns gold for the last 30 seconds and plays a soft chime at zero. Nothing cuts anyone off, and the host can pause or add 30 seconds.
4. **Questions.** People raise a hand on their phone. If you've added families, **hands from a different family than the storyteller's go to the front of the line**. The storyteller or host taps "Call on".
5. **Next storyteller →**

Phones can also send ❤️ 😂 😮 👏 🥲, which float up on the tablet.

## Families (optional)

By default everyone is in one group and guests only enter a name. When the evening is about families getting to know each other, add them in ⚙︎ Settings (up to six, e.g. each family's last name):

- Turns rotate between families, so you don't get a run of stories from one side of the table. It's fair per person whatever the family sizes: everyone tells once before anyone tells twice.
- Questions from a different family than the storyteller's go first. Left alone, people ask their own relatives; this nudges the families toward each other.
- Each family gets a color on the tablet and in the keepsake.

If you add families after people have joined, their phones ask which family they're with. For people without a phone, tap the colored dot next to their name on the tablet to move them. Removing families back down to one merges everyone again.

## Depth levels

The buttons at the top of the tablet control which prompts can be drawn:

- **Light**: easy, fun stories. Good for the first round.
- **+ Warm**: adds family, love, advice, and growing up.
- **+ Deeper**: adds hard years, fears, and people we miss. Open this only when the room feels ready.

Edit `prompts.json` to add your own. Changes apply on the next card, with no restart.

## Keepsake

Every finished story is logged with the teller, their family, the prompt, and who asked questions.
The tablet's Settings has a "Story keepsake" link, and `/keepsake.md` downloads it as text.
Each game is also saved as JSON in `stories/`. It could be nice material for a wedding toast.

## If phones can't connect

- **macOS firewall:** the first run may ask "Allow incoming connections for Python?" Click Allow. If you missed it, check System Settings → Network → Firewall → Options.
- **Same network:** phones must be on your home Wi-Fi, not cellular. Many routers' *guest* networks block device-to-device traffic, so put guests on the main network or turn off "client isolation" for the evening.
- The QR code is drawn by a small library loaded from the internet. If the tablet is offline it's skipped and the address is shown instead.

## License

MIT
