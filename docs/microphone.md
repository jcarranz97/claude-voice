---
title: The microphone watchdog
---

# The microphone watchdog

The HUD warns about a microphone left open, but only while the HUD is up — and the failure worth catching is exactly the one where nothing is up.

A session that dies without unwinding leaves its capture stream behind. The tray icon stays lit. Nothing that could explain it is still running. That can go unnoticed for hours.

## Seeing who holds it

```bash
claude-voice mic              # who holds it, and since when
claude-voice monitor          # the microphone and the speakers, anyone's
claude-voice monitor --watch  # ... live, until you quit
```

`monitor` answers from the machine's side:

```text
microphone
  ● pw-record        claude-voice · conversation mode            4m
  ● firefox          meet.google.com                            18m

speakers
  ● aplay            claude-voice · speaking                     0s

claude-voice
  ● hud.py           the window (1 open)                         9m
  ● listen.py        conversation mode · microphone open         4m
```

A browser tab left in a call holds the microphone exactly as much as we would, so it is listed too, by its own name. Ours are marked as ours.

Quit the HUD and watch the list empty: `pw-record` is the microphone itself, and it should be gone within about three seconds. That is how you check the claim that nothing of ours runs while no window is open, rather than believing it.

!!! note "Nothing here kills anything"

    It is a window, not a broom. `claude-voice mic --sweep` clears a capture of ours left behind, and other people's processes are theirs to close.

## The four kinds of holder

The distinction the whole feature turns on: **a capture stream that exists is not a microphone that is recording.**

| | |
|---|---|
| **ours** | a capture of ours with a live session behind it — conversation mode working normally |
| **orphan** | a capture of ours with nothing behind it. This is the one the watchdog exists for, and the one ++x++ and `--sweep` can clear |
| **recording** | another application actively recording. Said plainly; not ours to close |
| **parked** | another application holding a stream open, idle. Quiet — but this is what lights your desktop's tray icon |

The parked case is why the HUD says, quietly, `mic held open by claude (852955) — not recording`. Nothing here can close someone else's stream, and ++x++ deliberately will not try; quitting that application releases it. The line exists so a lit tray icon has an explanation instead of being a thing you learn to ignore.

## Installing the timer

```bash
claude-voice mic --install    # notify when anyone holds it too long
claude-voice mic --uninstall  # stop watching
claude-voice mic --sweep      # close a capture of ours that was left behind
claude-voice mic --once       # run one check now
```

`--install` writes a user unit and enables a systemd timer that checks every minute.

It is a **timer firing a oneshot**, not a daemon, because a daemon would need something watching *it* — and this exists precisely because the thing that was supposed to be watching had died.

What it says depends on what it finds:

| | |
|---|---|
| a capture of ours, no session behind it | urgent — and `--sweep` can clear it |
| another app recording | said plainly; not ours to close |
| another app holding a stream open, idle | quiet — but this is what lights the tray icon |

Conversation mode working normally is never reported.

Nothing is killed automatically. The watchdog names the holder and stops there, the same way ++x++ in the HUD only ever sweeps captures of ours.

## Thresholds

```toml
[mic.watch]
enabled = true
interval = 60      # seconds between checks
after = 300        # held this long before the first word
repeat = 1800      # seconds between reminders
ignore = []        # process names never worth announcing
```

`ignore` ships empty on purpose. An allow-list written in advance hides the one leak you did not predict, and a notice that fires constantly is one you stop reading — which is the same failure as not having it at all.

## Why holders are identified by pid *and* start time

A pid alone is reused. A recycled one would inherit the age of whoever held that number before, reporting two hours against a process born a minute ago — a false alarm shaped exactly like the real thing.

So a holder is `(pid, process start time)`, read out of `/proc`, and an age is only reported for a process that has genuinely been there that long.

## MICROPHONE OPEN, NO OWNER

That is the HUD's version of the orphan case, and it usually means an unclean exit from conversation mode. Press ++x++.

The warning reads the kernel's capture state, not our own bookkeeping — precisely so it still fires when our bookkeeping is what broke.

Two things count as open: a capture stream that is *running*, whoever owns it, and a `pw-record` of ours being alive whatever state its stream is in. That second one is the orphan the warning exists for, and the one ++x++ can clear.
