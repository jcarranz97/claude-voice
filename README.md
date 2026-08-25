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

And a HUD, in a spare terminal, so you can see all of it at a glance:

```
                        B O R R A   B O T
                          VOICE ON
   m: turn OFF and silence · d: dictate · c: conversation · t: session · q: quit

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

<details>
<summary>What the hooks do</summary>

| Hook | Script | What it does |
|---|---|---|
| `UserPromptSubmit` | `voice.py --hook-context` | Injects the TTS instruction, plays an acknowledgement, starts the heartbeat |
| `MessageDisplay` | `narrate.py` | Speaks progress between tool calls (optional) |
| `Stop` | `speak.py` | Speaks the `<!-- TTS: -->` line, stops the heartbeat |

Drop the `MessageDisplay` entry if you only want the final line spoken.

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

claude-voice hud                           # the status window, in a spare terminal
claude-voice say "test one two"            # synthesize and play, ignoring the switch
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
| `d` | dictate: record, transcribe, send |
| `c` | conversation mode: continuous listening |
| `t` | switch which Claude session receives dictation |
| `x` | close an orphaned microphone capture (emergency) |
| `q` | quit the HUD (the voice keeps working) |

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

Conversation mode also gates itself while the voice is speaking, so it does not
transcribe its own output.

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

**Dictation records but nothing arrives.** Delivery is refused unless the
target tmux pane is running `claude`; check `claude-voice dictate --panes` and
`~/.config/claude-voice/dictate.log`. If it records silence, the device is
wrong — `arecord -L`, and set `stt.device` by name.

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
the instant you hit enter — costs one small model call per prompt. Set
`ack.contextual = false` to use the cached phrases instead, or
`ack.enabled = false` to skip it entirely.

---

## Layout

```
bin/claude-voice        the only entry point you need
claude_voice/
  config.py             layered configuration
  voice.py              the switch; the UserPromptSubmit hook
  speak.py              synthesis, phoneme mixing; the Stop hook
  narrate.py            mid-turn progress; the MessageDisplay hook
  ack.py                the instant acknowledgement
  audioq.py             one sound at a time, in order
  thinking.py           the heartbeat, and subagent detection
  hud.py                the status window
  dictate.py            push-to-talk, and delivery into tmux
  listen.py             conversation mode: VAD + turn detection
  pron.py               pronunciation workbench
presets/                language packs
skills/                 a Claude Code skill for fixing pronunciation
```

## License

MIT.
