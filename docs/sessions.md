---
title: Sessions and focus
---

# Sessions and focus

You will have several sessions open, and a machine can be running a bot on the same hooks. This is how the program keeps them apart, and how you hand the voice to one of them.

## What each session is doing

```bash
claude-voice sessions
```

It prints every live session and its state — thinking, speaking, ready, idle — plus which one dictation is aimed at.

## Per session, except what is genuinely shared

The split is deliberate and the line is drawn in one place:

| | Grain | Why |
|---|---|---|
| What a *session* is doing (thinking, ready) | one file each | otherwise the first window to finish writes "ready" over everyone |
| The heartbeat's pidfiles | one each | otherwise one window's `Stop` hook kills the tick of a window still working |
| The spoken log | one per conversation | a shared log interleaves two conversations by the clock and calls it a dialogue |
| What the *speaker* is doing | global | there is one pair of speakers |
| The on/off switch | global | see below |

The HUD reads the session it is pointed at and lays the speaker's state over the top.

## Focus: one session at a time

The switch is one file for the whole machine, which is the right grain until three windows are open and all three want to tell you what they just did. Turning the voice off to stop two of them stops the third as well.

```bash
claude-voice focus           # only this session speaks, the rest go quiet
claude-voice focus --clear   # give every session its voice back
claude-voice mute            # mute just this one session
```

++f++ in the HUD does the same thing. That session speaks; the others behave exactly as though the voice were off, without anything being turned off and without touching those windows at all.

### Focus and dictation move together

++f++ focuses the session dictation is already aimed at. ++t++ carries the focus along when it switches session. `claude-voice focus` aims dictation at the terminal it was run in.

So the window you talk to and the window that answers out loud are one window, not two settings that happen to agree.

### It is filed under the terminal, not the session

This is what makes it survive. Closing a conversation and starting another one in the same window keeps the voice where you put it, and so does quitting the HUD, and so does a reboot.

A session id would not: a restarted session is a new one, and the focus would quietly fall off it — which is the moment every other window starts talking again.

"The terminal" is the tmux pane (`%12`) for a session in one, and the controlling pty (`pts:/dev/pts/3`) for a session started with `claude-voice run`. A hook inside the session finds its own either way — `$TMUX_PANE` when there is one, and otherwise `$CLAUDE_PID` read back through `/proc`, because a hook has no controlling terminal of its own to ask about.

!!! warning "Two consequences worth knowing"

    A focus left on a window you have since closed means **nothing speaks anywhere**. The HUD says so on its bottom line, and ++f++ clears it.

    And because pane ids belong to a tmux server, a focus set under a server that has since been restarted is ignored rather than applied to whatever pane inherited the number.

## The HUD is the application

Above the switch and above the focus sits one more gate: while no HUD is open, nothing of ours runs at all.

That is [The HUD](hud.md#the-hud-is-the-application), and it is worth reading before you go looking for why a machine with no window open makes no sound.

## Which session the HUD is watching

The HUD watches one session — the one ++t++ points at, the same one dictation goes to — and every session keeps its own state, so another window finishing its turn no longer speaks for yours.

For a session started through the wrapper, the join is exact from the first moment: `claude-voice run` knows the pty it started the session on, and Claude Code lists every live session under `~/.claude/sessions/`. That includes the dictated line that opens the conversation, which is precisely the one a title lookup could never match.

??? info "Sessions not started through the wrapper"

    A pane is joined to its conversation by its title, which Claude Code only sets once it has named the conversation — so a fresh window still says `Claude Code` and cannot be matched.

    There the HUD falls back to the liveliest conversation **of that pane's project**, and to nothing at all when that project has said nothing yet. A blank panel beside a new window is the honest answer; showing whichever window spoke last is how a per-session view still looks shared.

    This is also what the `SessionStart` hook exists to fix: it writes one small file binding the terminal to the conversation before anything has been said.

## Silencing everything

```bash
claude-voice silence
```

The panic button: drains the queue, kills the heartbeat and the acknowledgement of every session, and cuts whatever is playing right now. ++m++ in the HUD does the same on its way to turning the voice off.

It finds orphaned processes by walking `/proc` and matching the actual script path rather than by pattern — `pkill -f` would match an editor that happens to have the file open.
