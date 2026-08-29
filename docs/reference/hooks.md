---
title: Hooks
---

# Hooks

Four entries in Claude Code's settings file. They are what makes the voice attach to a session at all.

```bash
claude-voice hooks              # print the snippet, paste it yourself
claude-voice hooks --install    # merge it into ~/.claude/settings.json
claude-voice hooks --settings <path>
```

The installer does this for you. `--install` is for a machine where you installed the program by hand, and for adding hooks a newer version brought.

## What each one does

| Event | Command | |
|---|---|---|
| `SessionStart` | `claude-voice hook session-start` | Notes which terminal the conversation is in — a tmux pane, or the pty it was started on — before it has said anything |
| `UserPromptSubmit` | `claude-voice hook user-prompt-submit` | Injects the TTS instruction, plays an acknowledgement, starts the heartbeat |
| `MessageDisplay` | `claude-voice hook message-display` | Speaks progress between tool calls (optional) |
| `Stop` | `claude-voice hook stop` | Speaks the `<!-- TTS: -->` line, stops the heartbeat |

Drop the `MessageDisplay` entry if you only want the final line spoken. The other three are the voice.

### Why `SessionStart` is worth its entry

A window that has not exchanged anything yet still carries the default `Claude Code` title and has no transcript to match it against. Without the binding, the very first dictated line of a conversation has no session to be filed under and never reaches the history panel.

It costs nothing and says nothing; it writes one small file.

## What a turn actually does

1. **`SessionStart`** binds the terminal to the conversation.
2. **`UserPromptSubmit`** checks four things — a HUD is open, the switch is on, this session is not muted, and the [focus](../sessions.md#focus-one-session-at-a-time) allows it. If all four pass it spawns the acknowledgement and the heartbeat detached, marks the session as thinking, and returns the instruction as additional context. **If the voice is off it returns nothing at all**, which is what makes the switch cost no tokens.
3. **`MessageDisplay`** cleans a prose block and enqueues it, up to `narrate.max_per_turn`, never repeating one it already said, and skipping any block that carries the marker.
4. **`Stop`** kills the heartbeat first, then extracts the last `<!-- TTS: -->` from the message, synthesizes it, and enqueues it with a flush so the answer does not arrive behind three narration lines.

Every one of them catches broadly and exits zero. A hook that raises is a hook that breaks somebody's editor.

## Installing by hand

The snippet goes in `~/.claude/settings.json`. `claude-voice hooks` prints exactly what to paste, and that is the authoritative form — the shape below is illustrative:

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [{ "type": "command", "command": "claude-voice hook stop" }] }
    ]
  }
}
```

For a project's own `.claude/settings.json`, pasting is the only path — `--install` only ever touches your home settings unless you pass `--settings`.

## What `--install` does to your settings file

It **merges**, it does not paste:

- The four entries are added; everything already in the file stays.
- The copy it replaced is kept next to it with a timestamp.
- An event already hooked to us is left exactly as it is, so running it again after an upgrade adds only what is new.
- A settings file it cannot parse is **refused rather than repaired** — it prints the snippet instead and changes nothing.

## Old installs

The commands carry no paths, so reinstalling or upgrading does not break them.

Older installs wrote an interpreter and a script path into each hook. Those still work, and `claude-voice doctor` points them out rather than waiting for the day a moved checkout makes them go quiet:

```text
[ FAIL ] hook Stop — points at a missing file: /old/path/speak.py
         fix: claude-voice hooks   (the checkout moved — replace the old line)
```

`--install` counts a legacy entry as installed and leaves it alone — adding ours beside it would run the hook twice, which for `Stop` means saying the same line twice. So a legacy entry has to be taken out by hand.
