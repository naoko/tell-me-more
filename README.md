# Tell Me More — a story game for gatherings

Pick a card, tell a short true story against a gentle timer, then take questions.
The tablet is the shared table screen; everyone else joins from their phone.

No installs: Python 3.8+ standard library only.

![The tablet during questions, with a note for the keepsake](docs/tablet.png)

![Phone screens: your turn with swap buttons, choosing someone to tell it with, your card, and the partner's view](docs/phones.png)

## Run it

```bash
cd tell-me-more
make run            # or: python3 server.py
```

`make run` uses [uv](https://docs.astral.sh/uv/) if it's installed (pinned Python 3.12) and plain `python3` otherwise. There are no dependencies, so no virtualenv is needed.

It prints three addresses:

```
Tablet (host):   http://192.168.1.20:8765/host?key=Xq3v9LmT2aKp
Guests' phones:  http://192.168.1.20:8765
Keepsake:        http://192.168.1.20:8765/keepsake
```

1. Open the **host** link on the tablet. Tip: in Safari, Share → Add to Home Screen gives you full-screen.
2. Optional: open ⚙︎ Settings to change the story length, add families (see below), or enter your **Wi-Fi name and password**. The join screen then shows a second QR code that puts phones on the Wi-Fi, which is where less techy guests usually get stuck. Settings are saved on this computer for next time.
3. Guests scan the QR code on the tablet (or type the address) and type their name.
4. For anyone who'd rather not use a phone, use "Add someone without a phone" on the tablet; the host taps for them.

The host key changes each run. To keep it the same, use `make run KEY=something` (or `--host-key something`), and pick something a curious guest wouldn't guess. Then the tablet can just open `/host`: it asks for the key once and remembers it on that device, and the key is removed from the address bar so guests can't read it over your shoulder. Tap **Just watch** instead to show the join screen on a TV without host controls. After 10 wrong keys in a minute, a device has to wait. Use `PORT=9000` (or `--port 9000`) to change the port.

## How a turn works

1. **Ready.** The next storyteller is picked automatically. Everyone gets a turn before anyone gets two. If you've added families, turns also rotate between them. Tap any name to pick someone else instead.
2. **Choose a card.** Everyone holds **3 private cards** on their phone from the moment they join, so they can think about their story while others are telling theirs. Every hand always has at least one Light card. On their turn they tap the one they want, or 🎲 **Surprise me** for a blind draw. After telling, they get a fresh card. For someone without a phone, the tablet shows their cards to read out and tap. The host can also write a one-off prompt.
3. **Tell.** A ring timer counts down (2½ min by default). It turns gold for the last 30 seconds and plays a soft chime at zero. Nothing cuts anyone off. The storyteller or host can pause or add 30 seconds.
4. **Questions.** People raise a hand on their phone; their phone suggests a few easy questions in case nothing comes to mind. People who haven't asked yet go ahead of people who have, and if you've added families, people from a different family than the storyteller's go first. The storyteller or host taps "Call on". Asking out loud works too.
5. **Next storyteller →**

**Nobody gets put on the spot.** Any time, a guest can tap ↻ next to a card to swap it for a new one. On their turn they can also:

- **👥 Tell it with someone**: a couple or a parent and child share one story.
- **Come back to me later**: no story this time; they move to the end of the round.

The host has the same "Come back to … later" button on the tablet.

**Notes and ending.** During questions, the tablet has a box for a one-line note about the story ("the flat tire outside Yosemite, 1979"), which goes into the keepsake. When you're done, tap **That was the last one — wrap up** (or **Wrap up the evening** in Settings). The closing screen thanks everyone, asks the whole table one last question ("one thing you learned about someone tonight"), and lists the stories so you can add notes. **Keep playing** goes back to the game.

Phones can also send ❤️ 😂 😮 👏 🥲, which float up on the tablet.

## Families (optional)

By default everyone is in one group and guests only enter a name. When the evening is about families getting to know each other, add them in ⚙︎ Settings (up to six, e.g. each family's last name):

- Turns rotate between families, so you don't get a run of stories from one side of the table. It's fair per person whatever the family sizes: everyone tells once before anyone tells twice.
- Questions from a different family than the storyteller's go first. Left alone, people ask their own relatives; this nudges the families toward each other.
- Each family gets a color on the tablet and in the keepsake.

If you add families after people have joined, their phones ask which family they're with. For people without a phone, tap the colored dot next to their name on the tablet to move them. Removing families back down to one merges everyone again.

## Depth levels

The buttons at the top of the tablet control which prompts can be dealt. Unlocking a level gives everyone one card from it; going back down replaces cards that are now too deep.

- **Light**: easy, fun stories. Good for the first round.
- **+ Warm**: adds family, love, advice, and growing up.
- **+ Deeper**: adds grief, hard years, and turning points. It's hidden until you turn it on in ⚙︎ Settings, because it suits close friends better than a first meeting.

There are 88 prompts (46 Light, 34 Warm, 8 Deeper). Two optional lists help you tailor an evening:

- `"tonight"`: prompts you write for this event. They appear as a pick-list when you tap ✎ on the tablet, so you don't have to type in front of everyone. For example: `"A road trip you took with someone at this table"`. Check prompts about the couple with them first.
- `"skip"`: prompts not to deal this time, without deleting them.

 Edit `prompts.json` to add your own; changes apply to the next card dealt, with no restart. If an edit has a typo, the game keeps using the last good version and prints a warning in the terminal. Since everyone holds 3 cards, keep at least ~4 Light prompts per guest.

## Hosting tips

- **Open with 30 seconds:** "It's not a test. Tell a true story, short is great, and you can always pass." Then tap **Go first** on yourself and tell a light, slightly self-deprecating story of about a minute and a half to set the tone.
- **Next, someone outgoing** from the other family. Don't start with the couple or the shyest guest.
- **One full round** (everyone once) is the centerpiece: plan 45–60 minutes, between the main course and dessert. Offer a second round only if the table asks. With 8 or more guests, set 2 minutes for stories and 2 for questions.
- **Start on Light,** then unlock Warm after a few stories. Every hand keeps a Light card, so Warm stays optional.
- **Phones face-up on the table, eyes on the storyteller.**
- Don't spring a custom prompt about the couple (like the proposal) on them unless they've agreed to it beforehand.

## Keepsake

Every finished story is logged with the teller, their family, the prompt, and who asked questions.
The tablet's Settings has a "Story keepsake" link, and `/keepsake.md` downloads it as text.
Each game is also saved as JSON in `stories/` (which git ignores). It could be nice material for a wedding toast. If you restart the server during the evening, the story list carries on where it left off (start with `--fresh` to begin a new one).

## If phones can't connect

- **macOS firewall:** the first run may ask "Allow incoming connections for Python?" Click Allow. If you missed it, check System Settings → Network → Firewall → Options.
- **Same network:** phones must be on your home Wi-Fi, not cellular. Many routers' *guest* networks block device-to-device traffic, so put guests on the main network or turn off "client isolation" for the evening.
- The QR code is drawn by a small library loaded from the internet. If the tablet is offline it's skipped and the address is shown instead.

## License

MIT
