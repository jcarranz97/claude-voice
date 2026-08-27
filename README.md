# claude-voice

Give Claude Code a voice, an ear, and a status display — locally, with no
cloud speech services.

It hooks into Claude Code and does four things:

- **Speaks** a one-line summary at the end of every turn, in a local neural
  voice (Piper). Not the response body — a line the model writes *for the ear*.
- **Narrates** progress mid-turn, so a five-minute task is not five minutes of
  silence.
- **Ticks** softly while it works, with a *different* tick when subagents are
  the ones working.
- **Listens** — push-to-talk dictation, or continuous conversation mode with
  end-of-turn detection, delivered straight into your running Claude session.
- **Remembers what was said out loud**, both sides, so you can read back the
  line you missed without leaving the HUD.

And a HUD, in a spare terminal, so you can see all of it at a glance:

```
                        B O R R A   B O T
                          VOICE ON
  m: turn OFF and silence · d: dictate · c: conversation · l: Español · h: history · q: quit

                              ·  ·  ·
                        ○              ○
                    ◦     T H I N K I N G    ◦
                        ○              ○
                              ·  ·  ·
                     ▁▂▅▇▆▃▂▁▂▄▆▇▅▃▁▂▃▅▄▂▁

                        dictation → myrepo · fixing the parser
                   «Done, the tests pass.»
```

Everything runs on your machine. No audio leaves it.

---

## Why the design is the way it is

Most "make the LLM talk" setups read the response aloud and get abandoned in a
week. Markdown, diffs and file paths are unlistenable, and nobody wants to hear
"slash home slash user slash repos". The decisions that make this one liveable:

**It speaks the model's own summary, never the response.** While the voice is
on, a hook injects an instruction telling the model to end each response with
`<!-- TTS: one short spoken sentence -->`. That marker is what gets spoken. No
marker, no sound. The model writes for the ear on purpose — result, not
procedure; "the config file", not the path.

**The switch controls both ends, which is what makes it cheap.** While the
voice is off the hook injects nothing, so the model never writes the marker and
you spend no tokens on spoken summaries nobody will hear. Turning it on is what
makes the instruction appear.

**One audio queue, one player.** Acknowledgement, narration, final answer and
tick all enqueue and return immediately — a slow hook stalls the session. A
single locked player process plays them in order, one at a time.

**The acknowledgement is shown the last few turns, not just the prompt.** The
line spoken the instant you hit enter comes from its own small model call, and
that call used to see one thing: the sentence just submitted. Handed six words
it can only hand six back, so "try it again with the flag" came back as
"Retrying with the flag" — a sentence with no content in it. Worse, a word
dictation got wrong was repeated with total confidence, because there was
nothing to notice it against. It now reads the last `ack.context` turns of the
spoken log first, which is enough to name the actual work and to quietly read
"bump" where the microphone heard "pump".

**Per session, except what is genuinely shared.** You will have several
sessions open, and a machine can be running a bot on the same hooks. What a
*session* is doing — thinking, done — is one file each, and so are the
heartbeat's pidfiles; otherwise the first window to finish writes "ready" over
everyone and its `Stop` hook kills the tick of a window that is still working.
What the *speaker* is doing stays global, because there is one pair of them.
The HUD reads the session it is pointed at and lays the speaker over it.

**Foreign technical terms get their own phonemizer.** espeak takes one language
per utterance, so in Spanish "merge" comes out MER-je and "queue" becomes
KE-u-e. The primary language phonemizes the whole line (correct prosody,
correct word boundaries), then the configured foreign terms are re-phonemized
and spliced in. Piper voices share one IPA alphabet, so it works; the acoustic
model never trained on those phonemes, so they come out accented — which is
exactly how a bilingual developer actually says them.

**Silence detection is not enough for turn-taking.** A fixed silence threshold
forces a choice between cutting people off and being slow: at 600 ms, LiveKit's
open benchmark measures 21.7% mid-sentence cuts, and you need 1600 ms to reach
5%. Conversation mode runs Silero VAD per 32 ms frame, and when it hears
silence, asks a small model whether the phrase *sounds finished*.

**It fails silent, always.** A broken voice must never break coding.

---

## Install

Needs Linux with PipeWire or PulseAudio, and Python 3.11+.

```bash
git clone https://github.com/jcarranz97/claude-voice
cd claude-voice
./install.sh                 # English
./install.sh --preset es     # Spanish (es_MX)
./install.sh --no-stt        # text-to-speech only, no microphone
```

One language is enough to start; a second one is two files, later, with no
reinstall: `claude-voice lang --fetch es`.

That builds a virtualenv, downloads a Piper voice, writes a config file, and
puts `claude-voice` on your PATH. It does **not** touch your Claude settings —
hooks are yours to install:

```bash
claude-voice hooks           # prints the snippet
```

Paste it into the `"hooks"` block of `~/.claude/settings.json` (or a project's
`.claude/settings.json`), then:

```bash
claude-voice on              # off is the default, always
claude-voice hud             # in a spare terminal
```

Updating an existing install? Print the snippet again and compare — it gains hooks over time, and a missing one costs you a feature rather than breaking the voice. `claude-voice doctor` names the one you are missing and what it does.

<details>
<summary>What the hooks do</summary>

| Hook | Script | What it does |
|---|---|---|
| `SessionStart` | `thinking.py --bind` | Notes which tmux pane the conversation is in, before it has said anything |
| `UserPromptSubmit` | `voice.py --hook-context` | Injects the TTS instruction, plays an acknowledgement, starts the heartbeat |
| `MessageDisplay` | `narrate.py` | Speaks progress between tool calls (optional) |
| `Stop` | `speak.py` | Speaks the `<!-- TTS: -->` line, stops the heartbeat |

Drop the `MessageDisplay` entry if you only want the final line spoken.

`SessionStart` is what lets a pane name its conversation. A window that has not exchanged anything yet still carries the default `Claude Code` title and has no transcript to match it against, so without the binding the very first dictated line of a conversation has no session to be filed under and never reaches the history panel. It costs nothing and says nothing; it writes one small file.

</details>

---

## Running it

There are **two separate things**, and confusing them is the usual first
stumble:

| | who starts it | where it lives |
|---|---|---|
| **The voice** (speaking, narration, tick) | Claude Code, via the hooks | nothing to launch — it runs inside your Claude session |
| **The HUD** | **you** | a long-lived process in its own terminal |

So there is no daemon to start for the voice. Install the hooks, run
`claude-voice on`, and the next thing you say to Claude gets spoken back. The
HUD is optional and purely a viewer — closing it breaks nothing.

### Day to day

```bash
claude-voice on                            # start speaking (off is the default)
claude-voice off                           # stop, and silence anything playing now
claude-voice solo                          # mute just this one session
claude-voice silence                       # panic button: cut all sound now
claude-voice status                        # is it on? is this session muted?
claude-voice lang                          # which language speaks, and what else is here
claude-voice lang en                       # switch to it — the same thing l does in the HUD
claude-voice lang --fetch en               # download that language's voice first

claude-voice hud                           # the status window, in a spare terminal
claude-voice history 20                    # the last 20 lines of this conversation
claude-voice history 20 --all              # ... of every session on this machine
claude-voice say "test one two"            # synthesize and play, ignoring the switch
claude-voice ack "and now the tests"       # the acknowledgement for that prompt, printed
claude-voice config                        # what is actually in effect, and from where
claude-voice doctor                        # check the install and say what is wrong
claude-voice --help                        # everything, grouped
```

`doctor` is the one to reach for when something is off. It checks the
interpreter, the voice model, the audio session, whether the hooks are
installed and still point at files that exist, and reports the optional
speech-to-text pieces as notes rather than failures:

```
[  ok  ] piper-tts — importable
[  ok  ] voice model — en_US-amy-medium.onnx (63 MB)
[ FAIL ] hook Stop — points at a missing file: /old/path/speak.py
         fix: claude-voice hooks   (the checkout moved)
[ note ] switch — off
         fix: claude-voice on
```

### The HUD

Open it in a terminal you can leave alone — it repaints 20 times a second and
takes over the window:

```bash
claude-voice hud
```

Worth an alias, since you will open it constantly:

```bash
echo "alias hud='claude-voice hud'" >> ~/.bashrc
```

If you had an alias pointing straight at a checkout — `python /some/path/hud.py`
— repoint it at `claude-voice hud` instead. Going through the CLI means the
path stops mattering: it resolves the interpreter and the module locations for
you, so moving or updating the checkout cannot leave you running a stale copy.

### Without the CLI

The CLI is a thin dispatcher; nothing requires it. Any module runs directly, as
long as you use an interpreter that has `piper-tts` installed:

```bash
/path/to/venv/bin/python /path/to/claude-voice/claude_voice/hud.py
/path/to/venv/bin/python /path/to/claude-voice/claude_voice/voice.py on
```

This is also exactly what the hooks do — they call `voice.py`, `narrate.py` and
`speak.py` by absolute path, which is why `claude-voice hooks` prints the paths
already filled in for your machine.

Already have a virtualenv with `piper-tts` in it? Point the CLI at it instead
of installing a second one:

```bash
echo /path/to/your/venv/bin/python > ~/.config/claude-voice/python
```

### Keys in the HUD

| key | |
|---|---|
| `m` / space | voice off / ON — off silences whatever is playing, instantly |
| `l` | language: switch to the next preset, labelled in the language it gives you |
| `d` | dictate: record, transcribe, send |
| `c` | conversation mode: continuous listening |
| `t` | switch which Claude session receives dictation |
| `h` | history: show/hide the spoken log beside the reactor |
| `x` | close an orphaned microphone capture (emergency) |
| `q` | quit the HUD (the voice keeps working) |

### History: what was actually said

The bottom line of the HUD shows the last spoken line and nothing else — which
tells you it just said *something*, and is useless the moment the next line
replaces it. Press `h` to open the log as a panel down the left:

```
                        B O R R A   B O T
                             VOICE ON
  m: turn OFF and silence · d: dictate · c: conversation · h: hide history · q: quit
                          │
       H I S T O R Y      │
  ↑↓ scroll  ·  g/G: ends │              ·  ·  ·
                          │         ○              ○
 14:02  you › run the     │      ○     T H I N K I N G    ○
             tests again  │         ○              ○
 14:02 said ‹ Checking    │              ·  ·  ·
             the suite.   │
 14:04 said ‹ Two         │        ▁▂▃▅▆▇█▇▆▅▃▂▁▂▃▅▆▇█
             failures.    │
 14:05  you › fix them    │
```

`h` again puts it away. It is a panel, not a mode: the reactor keeps spinning
beside it, so you never trade the state you are watching for the log you are
reading. Newest at the bottom, arrows or `j`/`k` to scroll, `g`/`G` for the
ends, `q` still quits the HUD.

The panel reopens the way you left it — whether it was showing is remembered
in `~/.config/claude-voice/hud-history`, so `h` is a preference you set once
rather than a key you press every time you open a HUD.

It sits on the left by default. `position` moves it:

```toml
[history]
position = "bottom"      # left (default), right, or bottom
```

At the bottom it is a full-width strip under the reactor, and two things
change to buy back the rows: the microphone notice moves onto the divider,
which reads better as a labelled rule anyway, and the single last-spoken line
goes — the strip directly below it already ends with that line.

It matters most in conversation mode, where you are not looking at the Claude
window at all: transcription is imperfect, and a misheard sentence is invisible
until the answer comes back about the wrong thing.

When the window cannot hold both — under about 74 columns beside the reactor,
or too few rows under it — the panel takes the whole window instead. There, and
only there, `q` closes it rather than quitting.

**Spoken lines only** — not the conversation. And they are logged as they are
played, not read back out of the transcript, because the transcript does not
have them: only the final `<!-- TTS: -->` line reaches it. Narration is derived
on the fly, the acknowledgement is a separate model call, and a dictated
sentence arrives indistinguishable from a typed one. So `audioq.enqueue()` —
the one choke point every sound passes through — and `dictate.deliver()` each
append a line to `~/.config/claude-voice/spoken-<session-id>.jsonl`, in the
order things were actually heard. `claude-voice history` prints the same log in
the terminal.

**One log per conversation.** The panel shows the session it is watching — the
one `t` switches, the one dictation reaches. Shared, it was not a dialogue at
all: with two windows open you got a question from one and an answer from the
other, interleaved by the clock, with nothing on screen saying they belonged to
different conversations. `claude-voice history --all` interleaves them on
purpose, which is the right answer to "what did this machine say in the last
hour" and the only way to read a log written before the split.

A pane is joined to its conversation by its title, which Claude Code only sets
once it has named the conversation — so a fresh window still says `Claude Code`
and cannot be matched. There the panel shows the liveliest conversation **of
that pane's project**, and nothing at all when that project has said nothing
yet. Only outside tmux, with no pane to point at, does it fall back to the
liveliest session on the machine. A blank panel beside a new window is the
honest answer; showing whichever window spoke last is how a per-session log
still looks shared.

Resuming a conversation with `--continue` or `--resume` comes back with a new
session id, so the panel starts blank even though the conversation did not. The
old log is still there — `claude-voice history --session <id>` — but it does
not follow you across the resume.

The log is capped and trimmed per session, and a session that has been silent
for `keep_days` is swept away. Turn it off with `enabled = false` under
`[history]` and the panel goes — and with it the context the acknowledgement
reads, which falls back to seeing only the prompt.

### Dictation and conversation mode

Both deliver text into a **running** Claude Code session, which needs tmux:
there is no supported way to push text into an already-started interactive
session, and `tmux send-keys` does it without special permissions and without
stealing focus. Run `claude` inside tmux, then:

```bash
claude-voice dictate --panes        # list panes running claude
claude-voice dictate --pane 0:0.0   # pick one (or press t in the HUD)
```

Sending is **refused** unless the target pane is running `claude`. In a shell,
a bad transcription would execute as a command.

With no such pane, both modes are **disabled rather than silently useless**.
The microphone is not opened at all, the HUD replaces its footer with `⚠ no Claude Code session — dictation disabled`, and pressing `d` or `c` flashes the same reason instead of recording into a void. Otherwise a dead setup and an unheard sentence look identical: silence. Conversation mode also watches for the session closing under it, and **holds** rather than stopping: within about three seconds it stops transcribing, the reactor stops saying `LISTENING` — it says `NOT LISTENING`, stops drawing the inward wave, and the meter goes flat, because nothing is coming in — and the banner becomes `⚠ no Claude Code session — conversation on hold`, which becomes `you are talking to nothing` while you are actually mid-sentence. Voice activity is still detected — that is the whole point, so speaking into a dead setup looks different from speaking into a live one — but nothing is transcribed, because the result has nowhere to go. Open a session again and it resumes on its own, from the next sentence; you never have to remember to switch listening back on. Nothing needs restarting when a session comes back — the HUD re-checks every couple of seconds, and every key press checks afresh.

Conversation mode also gates itself while the voice is speaking, so it does not
transcribe its own output.

---

### The microphone watchdog

The HUD warns about a microphone left open, but only while the HUD is up, and
the failure worth catching is exactly the one where nothing is up. A session
that dies without unwinding leaves its capture stream behind; the tray icon
stays lit; nothing that could explain it is still running. That can go unnoticed
for hours.

So the same checks run on a timer, outside the HUD and outside any session:

```bash
claude-voice mic              # who holds it, and since when
claude-voice mic --install    # notify when anyone holds it too long
claude-voice mic --sweep      # close a capture of ours that was left behind
claude-voice mic --uninstall  # stop watching
```

`--install` writes a user unit and enables a timer that checks every minute. It
is a timer firing a oneshot rather than a daemon, because a daemon would need
something watching *it*, and this exists because the thing that was supposed to
be watching had died. What it says depends on what it finds:

| | |
|---|---|
| a capture of ours, no session behind it | urgent — and `--sweep` can clear it |
| another app recording | said plainly; not ours to close |
| another app holding a stream open, idle | quiet — but this is what lights the tray icon |

Conversation mode working normally is never reported.

Nothing is killed automatically. The watchdog names the holder and stops there,
the same way `x` in the HUD only ever sweeps captures of ours.

Holders are identified by pid *and* process start time. A pid alone is reused,
and a recycled one would inherit the age of whoever held that number before —
reporting two hours against a process born a minute ago, which is a false alarm
shaped exactly like the real thing.

Thresholds live under `[mic.watch]`: `after` (300s before the first word),
`repeat` (1800s between reminders), `interval`, and `ignore`, a list of process
names never worth announcing. `ignore` ships empty on purpose. An allow-list
written in advance hides the one leak you did not predict, and a notice that
fires constantly is one you stop reading — which is the same failure as not
having it at all.

---

## Configuration

Everything lives in `~/.config/claude-voice/config.toml`. Values fall back, key
by key, to `presets/<lang>.toml`, then to built-in defaults — so a config that
sets one value does not wipe out the rest.

```toml
[general]
preset = "en"
name = "Jarvis"                   # the HUD banner

[tts]
voice_model = "~/.local/share/piper-voices/en_US-amy-medium.onnx"
length_scale = 1.06               # >1 is slower — butler pacing lives here

[narrate]
word_limit = 50                   # spoken whole below this, trimmed above
max_per_turn = 12

[stt]
device = "plughw:CARD=Headset,DEV=0"   # arecord -L; prefer a NAME over an index
node = "alsa_input.usb-..."            # pw-record --list-targets

[ack]
context = 6                       # turns of spoken history the acknowledgement sees
timeout = 3.0                     # past this, the cached phrase plays instead

[history]
enabled = true                    # the spoken log behind the HUD's h panel
position = "left"                 # left, right or bottom of the HUD window
cap = 400                         # lines kept per session; older ones trimmed
keep_days = 7                     # a session silent this long is swept away
```

ALSA card *numbers* reorder on reconnect. A setup pinned to `plughw:4,0`
silently started recording from a webcam mic — digital silence — the day a card
moved. Use names.

### Language presets

A preset carries everything that changes with language: which voice speaks,
which acknowledgements are cached, how the model is told to phrase the spoken
line, the dictation glossary, the HUD labels, and the pronunciation tables.
`presets/en.toml` and `presets/es.toml` ship; copy one to add your own.

The **instruction** — the text injected into every prompt — is a config value,
not a constant. The register belongs to you, not to the tool. Make it terse,
make it formal, make it a pirate; it is your ear.

### Switching language

Switching language is switching preset, and it is one keystroke:

```bash
claude-voice lang                # what speaks now, and what else is on disk
claude-voice lang en             # switch — or press l in the HUD
claude-voice lang --fetch en     # download that voice, if it never was
```

The choice lives in `~/.config/claude-voice/preset`, a file holding a name,
next to `enabled` and `hud-history`. Deliberately **not** in `config.toml`:
flipping one key there would mean a tool rewriting TOML you wrote by hand, and
that eventually eats a comment it did not write. Delete the marker and the
config file's own `general.preset` is back in charge.

Nothing needs restarting. The hooks are short-lived processes that read the
config per invocation, so the next prompt gets the new instruction and the next
dictation the new Whisper language. The HUD reloads its labels in place.
Conversation mode is the one exception — its daemon holds the language for the
length of its run — so the HUD restarts it for you when you switch.

Switching **inverts two layers**, and this is the part worth knowing. A config
file written for Spanish carries Spanish in it: the voice model, the
instruction, the acknowledgement phrases. Left on top it would keep speaking
Spanish inside the English preset, which is exactly what makes a language
switch look broken while everything else works. So while the active preset is
not the one your config file names, the language pack wins — for the keys it
defines, and only those. Your microphone device and your panel position are in
no preset, and never move.

To keep a personal setting through a switch, say which language it belongs to:

```toml
[preset.en.tts]
voice_model = "~/.local/share/piper-voices/en_US-lessac-high.onnx"

[preset.es.tts]
voice_model = "~/.local/share/piper-voices/es_MX-claude-high.onnx"
```

That table is the top layer and holds whichever way the switch is thrown.

Two things refuse rather than half-work. A language whose `.onnx` was never
downloaded declines with the reason on screen — switching into a voice that
cannot speak is a silent failure — and `--fetch` is the way out. And the
acknowledgement cache is kept per preset, in `acks/<preset>/`, because it is
indexed by position: one shared directory would play the old language's wav
while the spoken log recorded the new language's words.

### Pronunciation

When a word comes out wrong, fix it by ear — an automated phoneme diff cannot
tell you that "main" came out as two syllables, because both renderings are
five phonemes.

```bash
claude-voice pron say "I merged into main"    # hear it
claude-voice pron diag main merge queue       # see both languages, get a fix
claude-voice pron list                        # what is currently overridden
```

`diag` prints the exact TOML to paste. Two tiers: `foreign_terms` for words the
second language says correctly, `overrides` for words *neither* gets right —
product names, acronyms — where you write the IPA by hand.

---

## Troubleshooting

Start with `claude-voice doctor` — it covers most of what follows. The rest is
for when it says everything is fine and you still hear nothing.

**Nothing is spoken.** Every turn appends one line to
`~/.config/claude-voice/speak.log` saying exactly why:

```
2026-08-25 11:19:16 fields=[...] len=73 marker=yes on=True audio=True
```

- `marker=NO` — the model did not write the `<!-- TTS: -->` line. Either the
  voice was off when you sent the prompt (the instruction is only injected
  while it is on), or the model judged the turn not worth speaking.
- `on=False` — run `claude-voice on`.
- `audio=False` — no PipeWire/PulseAudio session. Expected over plain SSH and
  in systemd services; there is nothing to play through.
- No line at all — the `Stop` hook is not installed or points at a bad path.
  Re-run `claude-voice hooks` and compare.

**The HUD dies with `NameError` or `AttributeError`.** You are running a stale
copy from an old path. This is what an alias frozen on `python .../hud.py`
gets you after the checkout moves. Run `claude-voice hud` instead, or repoint
the alias.

**The HUD says `MICROPHONE OPEN, NO OWNER`.** A capture process outlived its
parent — usually an unclean exit from conversation mode. Press `x`. That
warning reads the kernel's capture state, not our own bookkeeping, precisely so
it still fires when our bookkeeping is what broke.

Two things count as open: a capture stream that is *running*, whoever owns it,
and a `pw-record` of ours being alive whatever state its stream is in — that
second one is the orphan the warning exists for, and the one `x` can clear.

Another app's *parked* stream is not an alarm, but it is not nothing either: it
is what lights your desktop's own microphone indicator, which counts streams
rather than recordings. So when nothing is recording and somebody is still
holding the microphone open, the HUD says so quietly and names them —
`mic held open by claude (852955) — not recording`. Nothing here can close
someone else's stream, and `x` deliberately will not try; quitting that app
releases it. The line exists so a lit tray icon has an explanation instead of
being a thing you learn to ignore.

**Dictation records but nothing arrives.** Delivery is refused unless the
target tmux pane is running `claude`; check `claude-voice dictate --panes` and
`~/.config/claude-voice/dictate.log`. `claude-voice dictate --can-send` answers
the same question in one line, and exits non-zero when nothing can receive text. If it records silence, the device is
wrong — `arecord -L`, and set `stt.device` by name.

**The HUD goes calm while the session is still working.** The HUD watches one
session — the one `t` points at, the same one dictation goes to — and every
session keeps its own state, so another window (or a bot answering messages on
the same machine) finishing its turn no longer speaks for yours. If it still
happens, `claude-voice sessions` prints what each one is doing, and the HUD's
target has to be resolvable: it is found by tmux pane title, so a session
running outside tmux falls back to showing the liveliest one.

**It still speaks the old language after switching.** Something above the
preset is pinning it. `claude-voice config` prints the preset in effect and
where it came from, and `claude-voice doctor` names the voice model actually
loaded. The usual culprit is a `[tts] voice_model` (or an `[instruction] text`)
in your own `config.toml`, written for the language you switched away from —
move it under `[preset.<name>]` so it applies to that language only.

**`l` refuses in the HUD.** The other language's voice was never downloaded.
`claude-voice lang` lists what is on disk and what is missing;
`claude-voice lang --fetch <name>` gets it, and caches its acknowledgements
while it is there.

**The tick keeps going after the answer.** The `Stop` hook is what kills it, so
a session that died mid-turn (out of tokens, a hang, Ctrl-C) leaves it running.
It caps itself, or `claude-voice silence` ends it now.

---

## Requirements

| | |
|---|---|
| OS | Linux with PipeWire (PulseAudio works for playback) |
| Python | 3.11+ (`tomllib`) |
| System | `aplay`, and for input `arecord` + `pw-record` |
| TTS | [Piper](https://github.com/OHF-Voice/piper1-gpl) — local, neural, CPU |
| STT | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — local, CPU |
| Turn-taking | [smart-turn-v3](https://huggingface.co/pipecat-ai/smart-turn-v3) |
| Optional | `tmux` for dictation; an Anthropic credential for contextual acknowledgements |

The contextual acknowledgement — a one-line "Checking the disk space" spoken
the instant you hit enter — costs one small model call per prompt, and that
call is sent the last `ack.context` turns of what was said out loud as well as
the prompt itself. That is more tokens and more of the conversation leaving the
machine than a single sentence was; `ack.context = 0` sends the prompt alone.
Set `ack.contextual = false` to use the cached phrases instead, or
`ack.enabled = false` to skip it entirely.

`claude-voice ack "some prompt"` prints what would be said, with how long the
call took and how many turns it read — the way to choose `ack.context` for your
own connection, since a late acknowledgement is worse than a vague one.

---

## Layout

```
bin/claude-voice        the only entry point you need
claude_voice/
  config.py             layered configuration
  lang.py               the language switch: preset in, preset out
  voice.py              the switch; the UserPromptSubmit hook
  speak.py              synthesis, phoneme mixing; the Stop hook
  narrate.py            mid-turn progress; the MessageDisplay hook
  ack.py                the instant acknowledgement
  audioq.py             one sound at a time, in order
  thinking.py           the heartbeat, subagent detection, pane -> session
  hud.py                the status window
  mic.py                who holds the microphone; the watchdog timer
  spokenlog.py          the log of what was said out loud, both sides
  dictate.py            push-to-talk, and delivery into tmux
  listen.py             conversation mode: VAD + turn detection
  pron.py               pronunciation workbench
presets/                language packs
skills/                 a Claude Code skill for fixing pronunciation
```

## License

MIT.
