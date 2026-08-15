[Українська](README.md) · **English**

# votebot — a community-filled chart quiz for Telegram channels

Your audience fills a grid of two-parameter combinations with images. Each cell is a
separate round: the bot posts the chart with the active cell highlighted, readers drop
their pictures into the comments and vote on each other's with reactions. When the round
ends, the bot takes the winner, drops their image into the cell and opens the next round.

![Example chart](docs/demo.gif)

*The active cell is highlighted by a "snake" running around its perimeter. The images in
the cells are generated placeholders from `preview.py`.*

The format is the familiar reddit "alignment chart". The whole trick is that the two axes
must **anti-correlate**: ugly things must be able to turn out highly effective, and
beautiful ones to fail. If both axes pull in the same direction, only the diagonal gets
filled, the rest of the grid stays empty and the voting dies out.

> **Note on language.** Log messages, the default caption templates and the code comments
> are in Ukrainian. Everything the audience sees lives in `config.yaml` under `templates:`
> and `quiz:` and is trivially replaced with any language; log output is cosmetic.

---

## How it works

**One round = one post.** When a round ends, a new post is published and the previous one
is deleted — Telegram removes its comment thread along with it, so there is no separate
reply cleanup to do. Every round produces a fresh notification for subscribers.

**A scheduler, not a daemon.** `tick.py` runs once a minute, looks at `state.json` and
decides what to do: refresh the countdown, close the round, or nothing at all. When the
quiz finishes, the state is cleared and the scheduler does nothing until the next
`init_quiz.py`.

**Reactions are accumulated from the update queue.** The Bot API has no "give me the
reaction counts" method, so the bot collects them from `message_reaction` push updates.
Each such update carries the complete set of reactions of one user on one message, which
makes processing idempotent: draining the same batch twice changes nothing. Telegram keeps
the queue for 24 hours, so even a multi-hour scheduler outage loses no votes.

---

## Requirements

- **Python 3.10+**
- A **Telegram channel** with a linked discussion group
- A **dedicated bot** that is not used anywhere else (see the warning below)
- **ffmpeg** — optional but recommended: with it the animation is encoded as mp4 (full
  colour, ~30–50 KB), without it as gif (256 colours visibly damage photos, and the file
  is roughly five times larger)

---

## Setup

### 1. Code and dependencies

```bash
git clone https://github.com/USER/votebot.git && cd votebot
python -m venv .venv
```

```bash
# Linux/macOS
source .venv/bin/activate && pip install -r requirements.txt
```

```powershell
# Windows
.venv\Scripts\Activate.ps1 ; pip install -r requirements.txt
```

On headless Linux you also need fonts that cover your alphabet and, preferably, ffmpeg:

```bash
sudo apt install -y fonts-dejavu-core ffmpeg
```

Without fonts Pillow falls back to its built-in bitmap font and non-Latin text falls apart.

### 2. The bot

In [@BotFather](https://t.me/BotFather):

1. `/newbot` → get the token
2. `/setprivacy` → pick your bot → **Disable**

> ⚠️ **The bot must be dedicated.** Telegram permits exactly one `getUpdates` consumer per
> token, and it also remembers the `allowed_updates` filter server-side — whoever set it
> last decides which update types are queued at all. Sharing a token with another
> application produces the worst possible failure: the quiz keeps publishing rounds
> perfectly while receiving neither comments nor reactions, with nothing in the logs to
> indicate a problem. `init_quiz.py` detects this and refuses to start.

### 3. Channel and discussion group

- The channel must have a linked discussion group (Channel settings → Discussion).
  Without it, comments do not exist at all.
- The bot must be an **administrator of the channel** with rights to post, edit and delete.
- The bot must be an **administrator of the discussion group**. This is not cosmetic:
  Telegram delivers `message_reaction` updates only to chat administrators, so without it
  there is nothing to count votes from.

### 4. Finding the channel ID

Forward any post from your channel to [@RawDataBot](https://t.me/RawDataBot) and look for
`forward_from_chat` → `id` in the reply. It already comes in the correct form, with the
`-100` prefix (for example `-1001234567890`).

**You do not need the discussion group ID** — the bot resolves it itself via `linked_chat_id`.

### 5. Config and secrets

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

Put `BOT_TOKEN` into `.env`, and `telegram.channel_id`, the title and the axis labels into
`config.yaml`. Both files are in `.gitignore`.

### 6. Verify

```bash
python init_quiz.py --check
```

Checks the config, the templates, the bot's rights in the channel and the group, the
channel-to-group link and — most importantly — whether the bot actually **receives**
updates. Publishes nothing.

You can iterate on the visual design separately, without running a quiz:

```bash
python preview.py --filled 6
```

---

## Running

Start the quiz once:

```bash
python init_quiz.py
```

From then on the rounds are driven by a scheduler that must run once a minute.

### Locally (Windows / macOS / Linux)

The simplest option is a loop in a terminal. The window has to stay open for the whole
quiz (nine hours by default) and the machine must not go to sleep.

```powershell
# Windows PowerShell
while ($true) { python tick.py; Start-Sleep 60 }
```

```bash
# Linux/macOS
while true; do python tick.py; sleep 60; done
```

If the loop breaks, nothing is lost: the state lives in `state.json` and Telegram keeps the
update queue for 24 hours. Just start the loop again — the countdown and the round closing
catch up on their own.

### On a server: systemd (recommended)

Logs land in journald with rotation, control is plain `systemctl`, and systemd itself
prevents overlapping runs.

```bash
sudo cp deploy/votebot.service deploy/votebot.timer /etc/systemd/system/
sudo nano /etc/systemd/system/votebot.service   # set User= and the paths
sudo systemctl daemon-reload
sudo systemctl enable --now votebot.timer
```

```bash
journalctl -u votebot -f          # live log
systemctl list-timers votebot.timer
```

### On a server: cron

Simpler, but you have to rotate the log yourself.

```bash
crontab -e
```

```
* * * * * cd /opt/votebot && /opt/votebot/.venv/bin/python tick.py >> /opt/votebot/runtime/tick.log 2>&1
```

> The `runtime/` directory must exist **before** the first run: the shell sets up the `>>`
> redirection before Python gets a chance to create it. `mkdir -p runtime`.

Overlapping runs are prevented by the `runtime/tick.lock` file lock: if the previous tick is
still uploading a video, the next one simply skips its slot.

⚠️ **There must be exactly one scheduler.** Two concurrent tickers on different machines
split the update queue between them, and each sees only part of the reactions.

---

## Commands

```bash
python init_quiz.py                 # start
python init_quiz.py --check         # verify the setup, publish nothing
python init_quiz.py --dry-run       # render round one and print the post text
python init_quiz.py --abort         # stop the active quiz
python init_quiz.py --force         # start a new one over an active one
python preview.py --filled 6        # design preview with placeholder images
python simulate.py                  # offline run through every round, with assertions
```

For a test run it is convenient to shorten the rounds:

```bash
python init_quiz.py --round-seconds 60 --caption-seconds 30 --multi-entry
```

`--multi-entry` allows several submissions from one person, so you can exercise the vote
counting on your own.

---

## Files

| Path | What it is |
|---|---|
| `config.yaml` | quiz parameters (from `config.example.yaml`) |
| `.env` | bot token |
| `state.json` | the active quiz; `active: false` means there is nothing to do |
| `runtime/offset.json` | the `getUpdates` offset, kept separately so it survives the end of a quiz |
| `runtime/archive/<quiz_id>.json` | results of a finished quiz, including winner names |
| `media/` | generated charts and downloaded winner images |

---

## Things worth understanding about the settings

**`vote_weight`.** A regular Telegram user can put one reaction on a message; a Premium
subscriber can put up to three. With `reaction` a Premium account therefore carries three
times the weight of a regular one; with `user` everyone counts as exactly ±1 regardless of
subscription. For a public vote, `user` is the fairer choice.

**`min_votes`.** The minimum score required to win. At `0` any submission wins, even with no
reactions at all. At `1` or higher a cell can end up empty — the quiz simply moves on.

**Privacy of credits.** In the chart and in the winners list the bot writes the **first and
last name**, never the `@username`: the credit is seen by the entire channel audience, while
a username is a clickable link to a profile — a different level of exposure. The username
itself is stored in `runtime/archive/` so you can still find the author.

**What counts as a submission.** Photos (including ones with a caption) and images sent as
files. GIFs, videos and stickers do not count: a cell needs a static frame. Text comments are
ignored and do not consume the author's submission slot.

**Emoji.** Telegram only accepts reactions from its own fixed set — coloured hearts
(💙 💜 💚 🧡 💖 💗) are rejected. The variation selector is normalised, so ❤ and ❤️ are
treated as the same emoji.

---

## Troubleshooting

**The bot publishes rounds but collects zero submissions and zero votes.**
Most likely the token is in use by another application. Check with
`curl -s "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"` — if `allowed_updates` does
not contain the types you need, someone else set that filter and comment updates never enter
the queue. The cure is a dedicated bot. `init_quiz.py --check` catches this.

**`409 Conflict: terminated by other getUpdates request`.**
The same cause: two consumers on one token. Or two schedulers running at once.

**There is no "Comments" button under the posts.**
The channel has no linked discussion group.

**Submissions are collected but reactions are not counted.**
The bot is not an administrator in the discussion group, so Telegram does not send it
`message_reaction` updates.

**Text renders as boxes.**
No system fonts: `sudo apt install -y fonts-dejavu-core`, or point `render.font` /
`render.font_bold` at a `.ttf` in the config.

**You get a gif instead of an mp4.**
ffmpeg was not found.

**A round closed later than its deadline.**
The scheduler runs once a minute and closes the round on the first tick after the deadline.
On hour-long rounds that is about one percent of drift. If you need precision, use
`OnUnitActiveSec=15s`.

**The countdown in the caption never updates.**
`caption_update_*` is smaller than the scheduler interval: the caption is only edited during
a tick.

---

## Internals

```
votebot/
  config.py   parameters from config.yaml + secrets from .env
  render.py   chart and animation drawing (no network calls at all)
  state.py    atomic state storage + lock against overlapping ticks
  api.py      thin Bot API wrapper with retries and flood-wait handling
  updates.py  getUpdates draining, submission and reaction accumulation, scoring
  rounds.py   round lifecycle: start / countdown / results / finale
```

`simulate.py` runs the full cycle of every round against stubs, without a single network
call, and asserts the things that would otherwise only surface after many hours on a live
channel: state transitions, score counting, protection against double-crediting a winner,
credit privacy and the countdown schedule.

---

## License

MIT — see [LICENSE](LICENSE).
