---
title: Design decisions
---

# Design decisions

Most "make the LLM talk" setups read the response aloud and get abandoned in a week. Markdown, diffs and file paths are unlistenable, and nobody wants to hear "slash home slash user slash repos".

These are the decisions that make this one liveable. Each is a thing that was got wrong first.

## It speaks the model's own summary, never the response

While the voice is on, a hook injects an instruction telling the model to end each response with `<!-- TTS: one short spoken sentence -->`. That marker is what gets spoken. No marker, no sound.

The model writes for the ear on purpose — result, not procedure; "the config file", not the path. That is a different piece of writing from the response, produced by the thing that already knows what the response means.

## A summary of an answer is not an answer

Writing for the ear pulls towards the gist, and the gist is wrong when the question had an exact answer in it.

Ask which services failed to start and the screen lists three names while the voice says "three, one more than yesterday" — true, and useless to somebody who asked *which*.

So the instruction overrides its own word limit for a concrete answer: a number, a name, a short list, a yes or no gets said out loud, at whatever length that takes. Past six items it says how many, names the first few, and hands the rest to the screen — the one thing a screen is better at.

## The switch controls both ends, which is what makes it cheap

While the voice is off the hook injects nothing, so the model never writes the marker and you spend no tokens on spoken summaries nobody will hear. Turning it on is what makes the instruction appear.

A voice feature that costs tokens while switched off is a feature people uninstall.

## The window is the application, not a viewer

While no HUD is open, nothing of ours runs at all: nothing spoken, no acknowledgement, no heartbeat, no microphone held open, no instruction added to your prompts.

This is the surprising one, and it is deliberate. The alternative — hooks that speak whether or not anyone is watching — means a machine that installed this once is spending tokens and holding a microphone for nobody. Making the window the gate turns "is this thing running" into a question you answer by looking.

Closing does not turn the voice **off**, it suspends it. Open a window again and it picks up where the switch left it.

## One audio queue, one player

Acknowledgement, narration, final answer and tick all enqueue and return immediately — a slow hook stalls the session. A single locked player process plays them in order, one at a time.

The final line flushes the same session's pending items on the way in, so the answer does not arrive behind three narration lines about how it was reached.

## The acknowledgement is shown the last few turns, not just the prompt

The line spoken the instant you hit enter comes from its own small model call, and that call used to see one thing: the sentence just submitted.

Handed six words it can only hand six back, so "try it again with the flag" came back as "Retrying with the flag" — a sentence with no content in it. Worse, a word dictation got wrong was repeated with total confidence, because there was nothing to notice it against.

It now reads the last few turns of the spoken log first, which is enough to name the actual work and to quietly read "bump" where the microphone heard "pump".

## And it decides whether to speak at all

Say hello and the answer arrives in about the time an acknowledgement of it takes to play, so you were told twice that nothing was happening.

The same call now answers `SILENT` for anything it could simply answer — a greeting, a yes or no, a question with no work behind it — and nothing plays: not the line, not the cached phrase. What still gets an acknowledgement is the turn that would otherwise open with a minute of silence, which is the turn it was written for.

The test is how long the *answer* takes, not how short the request was: "run the tests" is three words and several minutes. A failed call is not a decline — if the model times out the cached phrase plays as before.

## Per session, except what is genuinely shared

You will have several sessions open, and a machine can be running a bot on the same hooks.

What a *session* is doing — thinking, done — is one file each, and so are the heartbeat's pidfiles; otherwise the first window to finish writes "ready" over everyone and its `Stop` hook kills the tick of a window that is still working.

What the *speaker* is doing stays global, because there is one pair of them. The HUD reads the session it is pointed at and lays the speaker over it.

## Focus is filed under the terminal, not the session

Closing a conversation and starting another one in the same window keeps the voice where you put it. A session id would not: a restarted session is a new one, and the focus would quietly fall off it — which is the moment every other window starts talking again.

## Foreign technical terms get their own phonemizer

espeak takes one language per utterance, so in Spanish "merge" comes out MER-je and "queue" becomes KE-u-e.

The primary language phonemizes the whole line — correct prosody, correct word boundaries — and then the configured foreign terms are re-phonemized and spliced in. Piper voices share one IPA alphabet, so it works; the acoustic model never trained on those phonemes, so they come out accented, which is exactly how a bilingual developer actually says them.

## Silence detection is not enough for turn-taking

A fixed silence threshold forces a choice between cutting people off and being slow. At 600 ms, LiveKit's open benchmark measures 21.7% mid-sentence cuts, and you need 1600 ms to reach 5% — a second and a half of dead air after every sentence.

Conversation mode runs Silero VAD per 32 ms frame, and when it hears silence, asks a small model whether the phrase *sounds finished*.

## Disabled beats silently useless

With no session that can receive text, dictation and conversation mode are switched off rather than left running into a void: the microphone is not opened, the footer says why, and pressing the key flashes the same reason.

Conversation mode holds rather than stopping when the session disappears under it — voice activity still shown, nothing transcribed — and resumes on its own when a session comes back.

Otherwise a dead setup and an unheard sentence look identical: silence.

## The reactor carries the state, and only the state

The instrument panel around it never changes colour, because a window whose chrome dims when nothing is happening reads as a window that is broken.

And the microphone badge has a colour of its own, because the ear being open is not a state of Claude's — confusing the two is how you end up talking to a window that stopped listening ten minutes ago.

## Two surfaces, one implementation

The frameless window and the terminal HUD read the same module for everything they know and route every key through the same function — including every refusal. They can disagree about how a thing is drawn; they cannot disagree about whether the microphone is open.

## It fails silent, always

A broken voice must never break coding. Every hook catches broadly and exits zero, a missing voice model falls back to another of the same language, and a hook that cannot do its job writes a line to a log and gets out of the way.

Which is why the first question after silence is *what does the log say*, and never *what broke*.
