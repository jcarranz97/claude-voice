---
title: Living in the HUD
---

# Tutorial 3 — Living in the HUD

The window has been open since tutorial 1 and nobody has told you what most of it says. This walks the whole surface: what the reactor's states mean, what each panel answers, and the keys that matter once three sessions are open.

Nothing here needs a microphone.

## Step 1 — Open it on its own

The HUD opens with your first session, but you can open it by itself:

```bash
claude-voice hud
```

You will do this constantly, so it is worth an alias:

```bash
echo "alias hud='claude-voice hud'" >> ~/.bashrc
```

!!! warning "Do not alias a path into a checkout"

    An alias like `python /some/path/hud.py` freezes on that path, and the day the checkout moves or updates you get a `NameError` from a stale copy. `claude-voice hud` resolves the interpreter and the module locations for you.

## Step 2 — Read the reactor

The reactor in the middle carries the state, and only the state. The instrument panel around it never changes colour — a window whose chrome dims when nothing is happening reads as a window that is broken.

| What it says | What is true |
|---|---|
| `STANDING BY` | Nothing running. The voice is on and nothing has been asked |
| `THINKING` | A turn is in progress |
| `AGENTS` | The turn is waiting on subagents — small reactors orbit the main one, one each |
| `SPEAKING` | Amber, and moving with the actual audio |
| `LISTENING` | A dashed ring: conversation mode is armed |
| `VOICE OFF` | The switch is off |

**Speaking follows the voice literally.** A line being spoken is a finished file before a sample of it is played, so its shape is known in advance: the player measures it once, publishes the envelope with the moment playback started, and every window draws it off the clock. Nothing is streamed and nothing can drift — a window opened mid-sentence catches up on the right syllable.

The microphone is the other direction and has no such luxury, so its level is published as it is heard, about twenty-five times a second, and the reactor rises fast and falls slowly the way an ear does rather than the way a graph does.

## Step 3 — Read the panels

Four blocks, all on by default:

<div class="grid cards" markdown>

-   **System** — CPU, memory, disk, and the graphics card's load and VRAM, named by its actual board.

-   **Repo** — the repository, the branch, the pull request that branch has open, and the state of its checks: how many passing, how many running, the names of the ones that failed.

-   **Session** — where dictation is going, which language speaks, and what the microphone is doing.

-   **Agents** — the running subagents, and what each one is doing.

</div>

The repo panel is the one worth pausing on. It exists for the ten minutes after a push, when the only question in the room is whether the thing went green. The branch is read off disk and is always current; the rest comes from `gh` on a slow clock — about once a minute, every twelve seconds while something is still running — asked in the background, so a slow network can make that row late but can never make the window stutter.

No repository, no `gh`, no pull request: the rows with no answer are simply not drawn.

## Step 4 — Open the spoken log

Press ++h++.

The log opens as a panel down the left, beside the reactor rather than instead of it — you never trade the state you are watching for the log you are reading:

```text
 14:02  you › run the tests again
 14:02 said ‹ Checking the suite.
 14:04 said ‹ Two failures.
 14:05  you › fix them
```

Newest at the bottom. ++arrow-up++ / ++arrow-down++ or ++j++ / ++k++ scroll; ++g++ and ++shift+g++ jump to the ends; ++h++ puts it away.

It reopens the way you left it — whether it was showing is remembered in `~/.config/claude-voice/hud-history`, so ++h++ is a preference you set once rather than a key you press every time.

Move it if the left is not where you want it:

```toml
[history]
position = "bottom"      # left (default), right, or bottom
```

!!! info "It is spoken lines only, not the conversation"

    And they are logged as they are *played*, not read back out of the transcript — because the transcript does not have them. Only the final `<!-- TTS: -->` line reaches it; narration is derived on the fly, the acknowledgement is a separate model call, and a dictated sentence arrives indistinguishable from a typed one. [The spoken log](../history.md) explains what that buys you.

## Step 5 — Three sessions, one voice

Open a second terminal and start another session:

```bash
claude-voice
```

It attaches to the same window. There is only ever one HUD.

Now the problem: both sessions want to tell you what they just did. Turning the voice off to stop one stops the other.

Press ++f++.

That hands the voice to one session — the one dictation is already aimed at. It speaks; the others behave exactly as though the voice were off, without anything being turned off and without touching those windows at all. Press ++f++ again to give everyone their voice back.

From inside a session, the same thing:

```bash
claude-voice focus           # only this session speaks
claude-voice focus --clear   # everyone gets their voice back
claude-voice sessions        # what each one is doing right now
```

++t++ switches which session dictation reaches, and carries the focus with it — so the window you talk to and the window that answers are one window.

## Step 6 — The rest of the keys

| Key | |
|---|---|
| ++m++ / ++space++ | voice off / on, for the whole machine — off silences whatever is playing, instantly |
| ++f++ | mute every session except the one ++t++ points at, and back |
| ++l++ | language: switch to the next preset, labelled in the language it gives you |
| ++d++ | dictate: record, transcribe, send |
| ++c++ | conversation mode: continuous listening |
| ++t++ | switch which session receives dictation — the voice follows it |
| ++h++ | history: show or hide the spoken log |
| ++x++ | close an orphaned microphone capture (emergency) |
| ++q++ | quit |

!!! danger "++q++ stops everything"

    The HUD is the application. The last window closing stops the voice, the microphone and the heartbeat with it. Closing does not turn the voice **off**, though — it suspends it. Open a HUD again and it picks up where the switch left it, with no keys pressed.

## Step 7 — The terminal surface

On a machine with no desktop, over ssh, or in a pane you already have open:

```bash
claude-voice hud --terminal
```

Same HUD, drawn out of ring glyphs on a character grid. The reactor, the history, the system meters, the keys and every refusal come from one module that both windows read, so there is one answer to "is the microphone open", not two. Pressing ++m++ in the terminal and clicking the same button in the window run the same function.

It is a straight downgrade anywhere with a desktop — a character grid cannot draw a curve.

## Step 8 — Move and resize it

A frameless window has no title bar and no resize grips, so the page lends it both:

- Drag the HUD's own bar to move the window.
- Drag any edge or corner to resize.
- Double-click the bar to maximise.
- The `✕` at the right of the bar closes it, as do ++q++ and ++escape++.

Where you put it and how big you made it are written down when it closes and used again next time, so it only opens in the middle of the screen once.

The layout is sized in `em` off one number that follows the window, so a bigger window is a bigger HUD rather than the same HUD with more background around it.

## What you have now

- A reading of every state the reactor shows.
- The spoken log on ++h++, positioned where you want it.
- Focus, on ++f++, for when several sessions are open.
- A terminal surface for machines with no desktop.

## Next

[Tutorial 4 — Making it yours](making-it-yours.md): a different voice, a second language, and the panels you do not want.
