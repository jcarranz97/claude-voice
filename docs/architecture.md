---
title: Architecture
---

# Architecture

How the pieces fit, for anyone reading the source or debugging something the [troubleshooting](troubleshooting.md) page does not cover.

## The shape of it

Nothing here is a service. There are three kinds of process, they have wildly different lifetimes, and they find each other through files in `~/.config/claude-voice/`.

```mermaid
flowchart LR
    CC["Claude Code"] -->|four hooks| HK["the hooks<br/><i>milliseconds each</i>"]
    HK -->|spawn, detached| DM["the daemons<br/><i>ack · heartbeat<br/>player · the ear</i>"]
    HK <--> ST[("the state directory")]
    DM <--> ST
    HUD["the HUD<br/><i>one window</i>"] <--> ST
    HUD -.->|"its pidfile is the licence:<br/>no window, nothing runs"| HK
```

They coordinate through that one directory rather than through a bus, because the coordinating processes come and go faster than a connection could be established, and because the state has to survive all of them dying at once.

| | Lifetime | Started by |
|---|---|---|
| **The hooks** | milliseconds | Claude Code, one per event. They read the config fresh each time, which is why a config change needs no restart |
| **The HUD** | hours | you, or the first session. Its existence is what licenses everything else to run |
| **The daemons** | minutes to hours | something short-lived, detached. Each stops on its own when the window goes |

What each of them writes, and where, is in [Keys and files](reference/keys-and-files.md).

## A turn, end to end

```mermaid
sequenceDiagram
    autonumber
    actor You
    participant CC as Claude Code
    participant Hook as hooks
    participant Ack as ack
    participant Tick as heartbeat
    participant Q as queue + player
    participant HUD

    You->>CC: hit enter
    CC->>Hook: UserPromptSubmit
    activate Hook
    Note over Hook: gate: window open?<br/>switch on? not muted?<br/>focus allows?
    Hook->>Ack: spawn, detached
    Hook->>Tick: spawn, detached
    Hook->>HUD: mark session "thinking"
    Hook-->>CC: the TTS instruction
    deactivate Hook

    Ack->>Q: one short line
    Q-->>You: 🔊 "Checking the suite."

    loop while the turn runs
        Tick->>Q: tick, unless the queue is busy
        Q-->>You: 🔊 ·
    end

    loop between tool calls
        CC->>Hook: MessageDisplay
        Hook->>Q: cleaned prose
        Q-->>You: 🔊 progress
    end

    CC->>Hook: Stop
    activate Hook
    Hook->>Tick: kill
    Note over Hook: extract the LAST<br/>TTS marker
    Hook->>Q: synthesized line,<br/>flushing this session's backlog
    Hook->>HUD: mark session "ready"
    deactivate Hook
    Q-->>You: 🔊 "Two failures, both in the parser."
```

Two details in that sequence are the whole design.

**The gate in step 2.** Fail it and nothing is spawned, nothing is injected, and the turn costs exactly what it would have cost without this program installed:

```mermaid
flowchart LR
    P([prompt submitted]) --> A{a HUD<br/>is open?}
    A -->|no| X([return nothing])
    A -->|yes| B{switch<br/>is on?}
    B -->|no| X
    B -->|yes| C{this session<br/>muted?}
    C -->|yes| X
    C -->|no| D{focus allows<br/>this terminal?}
    D -->|no| X
    D -->|yes| E([spawn ack + heartbeat<br/>inject the instruction])
```

**The flush in the last step.** The final line drops the *same session's* pending items on its way into the queue, so the answer does not arrive behind three narration lines about how it was reached. Other sessions' audio is untouched.

## Text to speech

```mermaid
flowchart TB
    M[assistant message] --> E["extract: the LAST TTS marker"]
    E -->|no marker, or SILENT| N([nothing is spoken])
    E -->|a line| P1["phonemize the WHOLE line<br/>in tts.primary_voice"]
    P1 --> W{per word}
    W -->|in pronunciation.overrides| O[use that IPA verbatim]
    W -->|in foreign_terms| F["re-phonemize in<br/>tts.foreign_voice"]
    W -->|neither| K[keep the primary phonemes]
    O --> S
    F --> S
    K --> S
    S[Piper, CPU, at length_scale] --> WAV[16-bit mono wav]
    WAV --> ENQ[enqueue: seq under a lock]
```

The whole line is phonemized first, and only then are individual words swapped, because that is what keeps the prosody and the word boundaries right — a per-word approach gets every term correct and the sentence wrong.

**Last, not first**, on the marker. A response that quotes the marker while explaining it — this page, for instance — would otherwise get its example spoken instead of its actual summary.

Then one player, holding one lock:

```mermaid
sequenceDiagram
    autonumber
    participant Any as any producer
    participant Q as queue/
    participant P as player (flock)
    participant A as aplay
    participant HUD

    Any->>Q: seq + wav + text
    Any-->>Any: return immediately
    Note right of Any: a hook that waited<br/>for audio would stall<br/>your session

    P->>Q: take the lowest seq
    P->>P: measure the envelope
    P->>A: spawn
    P->>HUD: state.json — envelope + t0
    Note over P,HUD: written AFTER the spawn,<br/>so t0 is the moment<br/>sound actually starts
    P->>Q: append to the spoken log
```

## How the reactor knows how loud

Two directions, two mechanisms, because only one of them is hard.

```mermaid
flowchart LR
    subgraph mouth["The mouth — known in advance"]
        W[finished wav] --> EN[measure once:<br/>one envelope]
        EN --> PUB[publish envelope + t0]
        PUB --> R1[every window interpolates<br/>off the wall clock]
    end

    subgraph ear["The ear — no such luxury"]
        MIC[pw-record frames] --> LV[publish a bare float<br/>~25 times a second]
        LV --> R2[fast attack, slow decay<br/>stale after 0.5s = silence]
    end
```

| | Mouth | Ear |
|---|---|---|
| Known in advance | yes — it is a finished file | no |
| Published as | one envelope + the start time | a bare float, ~25×/s |
| Read as | interpolated off the wall clock | the last value, stale after half a second |
| Can drift | no | not applicable |

A window opened mid-sentence catches up on the right syllable, because it computes the same function of the same clock every other window does. The ear cannot work that way, so it is smoothed instead — the way an ear behaves rather than the way a graph does.

Both are advisory. A window that cannot read either still animates, blind.

## The wrapper

There is no way into a session that is already running, so the text has to come from something that was present at launch.

```mermaid
flowchart TB
    CV["claude-voice"] --> HUDQ{a HUD<br/>is open?}
    HUDQ -->|no| OPEN[spawn one, wait up to 6s]
    HUDQ -->|yes| FORK
    OPEN --> FORK
    FORK["openpty → fork → setsid → TIOCSCTTY"] --> CHILD["execvp: claude"]
    FORK --> REG[["registry file:<br/>pid, pty path, cwd, socket"]]
    FORK --> SOCK[["AF_UNIX socket, mode 0600<br/>in XDG_RUNTIME_DIR"]]
    FORK --> LOOP{{"select() on<br/>[master, socket, stdin]"}}
    LOOP -->|stdin → master| CHILD
    LOOP -->|master → stdout| SCREEN([what you see])
    LOOP -->|socket → master| CHILD
```

The delay before the carriage return is so the TUI has taken the text before the newline arrives.

The **pty path is the join key**: the wrapper knows the pty it started the session on, and Claude Code lists every live session under `~/.claude/sessions/`, so a wrapped session is matched to its conversation exactly, from the first moment — including for the dictated line that opens the conversation, which is precisely the one a title lookup could never match. That is also why the fork is hand-rolled rather than `pty.fork()`: it needs the slave's name.

### Dictation, end to end

```mermaid
sequenceDiagram
    autonumber
    actor You
    participant HUD
    participant Rec as arecord
    participant W as faster-whisper
    participant D as dictate
    participant S as socket
    participant Wrap as wrapper
    participant CC as claude

    You->>HUD: press d
    HUD->>Rec: start, on stt.device
    You->>Rec: speak
    You->>HUD: press d
    HUD->>Rec: stop
    Rec->>W: the wav
    W-->>D: text (glossary as initial prompt)
    D->>D: drop known hallucinations
    alt target is running claude
        D->>S: text, no newlines allowed
        S->>Wrap: bytes
        Wrap->>CC: into the pty master
        Wrap->>CC: \r, 150 ms later
        D->>HUD: append to the spoken log
    else target is a shell, or gone
        D-->>HUD: refuse, and say why
        Note over D,HUD: in a shell, a bad transcription<br/>would execute as a command
    end
```

## Conversation mode

```mermaid
flowchart TB
    START([press c]) --> CHK{a window open<br/>AND a session<br/>to deliver to?}
    CHK -->|no| REF([refuse, and say which])
    CHK -->|yes| CAP[pw-record, 32 ms frames]

    CAP --> LVL[publish the level]
    LVL --> GATE{are WE<br/>speaking?}
    GATE -->|yes| CAP
    GATE -->|no| VAD["Silero VAD<br/>0.60 on / 0.35 off<br/>500 ms preroll"]

    VAD -->|speech| CAP
    VAD -->|"silence ≥ floor_ms"| ST["ask smart-turn<br/>every 200 ms"]

    ST -->|"p ≥ complete"| SEND
    ST -->|"past ceil_ms"| SEND
    ST -->|"not finished"| CAP

    SEND[transcribe and deliver] --> ALIVE{session still<br/>there?}
    ALIVE -->|yes| CAP
    ALIVE -->|no| HOLD["HOLD: keep showing the level,<br/>transcribe nothing"]
    HOLD -->|re-checked every 3s| ALIVE
```

The hold is the part worth noticing. Voice activity is still detected and still drawn — so speaking into a dead setup looks different from speaking into a live one — but nothing is transcribed, because the result has nowhere to go. Open a session again and it resumes from the next sentence, with no key pressed.

Every three seconds it re-checks, which is what makes that work.

### Why three layers and not a timeout

A fixed silence threshold forces a choice between cutting people off and being slow.

```mermaid
flowchart LR
    A["600 ms silence"] --> A1["21.7% of turns<br/>cut mid-sentence"]
    B["1600 ms silence"] --> B1["5% cut —<br/>and 1.6s of dead air<br/>after every sentence"]
    C["smart-turn:<br/>does it SOUND finished?"] --> C1["5% cut at ~543 ms"]
```

Figures from LiveKit's open turn-taking benchmark. Silero runs in about 0.07 ms per frame and smart-turn in about 25 ms, both on the CPU, so the cost of asking is not what makes the decision.

## The HUD, in two surfaces

```mermaid
flowchart TB
    subgraph core["hudcore — state, labels, actions, refusals"]
        HC[" "]
    end

    core --> WEBM[hudweb]
    core --> TUIM[hud, curses]

    WEBM --> SRV["ThreadingHTTPServer<br/>127.0.0.1:random"]
    SRV --> PROD["one producer thread, 4 Hz"]
    SRV --> SSE["/events — SSE snapshots<br/>+ named level events at 20 Hz"]
    SRV --> ACT["POST /act"]
    SRV --> SHELL{"hudshell.open_window"}

    SHELL -->|preferred| WV["WebKitGTK, frameless"]
    SHELL -->|fallback| CH["Chromium --app, own profile"]
    SHELL -->|none| URL["print the address"]

    TUIM --> CUR["curses, 20 fps"]

    ACT --> ACTF["hudcore.act(name)"]
    CUR --> ACTF
    ACTF --> core
```

Both surfaces call one function for every key. There is one implementation of "turn the voice off", and one implementation of the reason it might refuse — so the two can disagree about how a thing is drawn, and cannot disagree about whether the microphone is open.

**The connection is the window.** The page holds the event stream open; when the last stream drops for more than a few seconds, the server exits, and on the way out — if it was the last window — it stops conversation mode, silences the queue and sweeps orphaned captures.

```mermaid
sequenceDiagram
    autonumber
    participant Page as the page
    participant Srv as the server
    participant Rest as mic, tick, queue

    Page->>Srv: GET /events (held open)
    Note over Page,Srv: while this stream is up,<br/>the hooks may speak
    Page-xSrv: window closed — stream drops
    Srv->>Srv: wait out the grace period
    alt this was the last window
        Srv->>Rest: stop conversation mode
        Srv->>Rest: silence the queue
        Srv->>Rest: sweep orphaned captures
    end
    Srv->>Srv: exit
```

## Presence, focus and sessions

Four questions, four separate files, deliberately not one:

```mermaid
flowchart TB
    Q1{"Is anyone<br/>watching?"} -->|live pidfiles,<br/>one per window| G1[the machine]
    Q2{"Is the voice<br/>on?"} -->|a marker file| G2[the machine]
    Q3{"May THIS session<br/>speak?"} -->|focus.json +<br/>per-session mute| G3["the TERMINAL —<br/>tmux pane or pty"]
    Q4{"What is this<br/>session doing?"} -->|one state file<br/>per session| G4[the session]
```

The grain in the third row is the one that took a rewrite to get right. Focus is filed under the terminal rather than the session id, so closing a conversation and starting another one in the same window keeps the voice where you put it — a session id would fall off on the restart, which is the exact moment every other window starts talking again.

## What leaves the machine

```mermaid
flowchart LR
    subgraph local["Local, always — no exceptions"]
        TTS[Piper synthesis]
        STT[faster-whisper]
        VADL[Silero VAD]
        TURN[smart-turn]
    end

    subgraph net["Network, both switchable"]
        A["the acknowledgement:<br/>the prompt + ack.context turns<br/>of the spoken log"] --> API[Anthropic API]
        R["the repo panel:<br/>gh pr view for this branch"] --> GH[GitHub]
    end
```

**No audio leaves the machine, ever.** There is no code path that sends a recording anywhere. `ack.contextual = false` removes the first call; `plugins.github.network = false` removes the second. [Security](security.md) has the detail.

## Models

| | | Where from |
|---|---|---|
| TTS | Piper | `~/.local/share/piper-voices/`, fetched from Hugging Face |
| STT | faster-whisper, `small` by default | its own cache |
| VAD | Silero v6 | read out of faster-whisper's assets — no torch |
| Turn-taking | smart-turn v3 | one ONNX file from Hugging Face, with features computed by faster-whisper's own extractor to avoid a `transformers` dependency |
