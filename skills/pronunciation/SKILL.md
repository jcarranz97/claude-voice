---
name: pronunciation
description: Fix how the claude-voice TTS pronounces a word. Use whenever the user says a word sounded wrong, was mispronounced, "that sounded off", "it should sound English", or asks to change how something is said aloud. Also for auditing or listing current pronunciation rules.
---

# Fixing a pronunciation

The user heard a word that came out wrong. Your job is to diagnose it, apply the
right level of fix, and **verify it by ear with them** — not declare victory
because the phonemes look correct.

Everything is driven from the config, not from code. Rules live in
`~/.config/claude-voice/config.toml`, which overrides the language pack. Your
own language packs live beside it in `~/.config/claude-voice/presets/`;
nothing that is edited by hand lives inside the install.

## 1. Identify the word

If the user did not name it exactly, ask before touching anything. Do not guess
from the last spoken line — there are usually several candidates.

## 2. Diagnose

```bash
claude-voice pron diag <word>
```

It shows the phonemes in both configured languages and whether a rule already
applies. It also prints the exact TOML to paste.

## 3. Pick the right level

Three levels, in order of precedence. Choose the **lowest** one that fixes it:

| Level | When | Where |
|---|---|---|
| **Leave it alone** | The primary language already says it the way the user would | — |
| **`foreign_terms`** | The foreign column of the diagnosis sounds right | `[pronunciation]` list |
| **`overrides`** | **Neither** sounds right | `[pronunciation.overrides]`, IPA by hand |

**The most common mistake is overcorrecting.** In Spanish, words like `commit`,
`deploy`, `refactor`, `endpoint`, `server`, `debug`, `package`, `install`,
`token`, `script`, `log` already sound the way a developer says them, and
forcing them into English sounds affected. They are left out of the shipped
list on purpose — do not add them back.

For `overrides`, write espeak IPA. A reliable trick is to spell the word the
way it should be pronounced and extract the phonemes from that:

```bash
claude-voice pron diag kiubectl
```

`diag` takes anything, real word or not, so spelling it as it should sound and
reading the phonemes back is the short way to write an override without
guessing.

## 4. Verify in context, not in isolation

A word alone sounds different from the same word inside a sentence — Spanish
voices consonants between vowels, and stress shifts. Always test with a
realistic phrase:

```bash
claude-voice pron say "I merged into main and kubectl is still pending."
```

It reports duration and peak level, and warns if the output is near-silent —
which means the phoneme stream did not resolve, not that your ears are wrong.

## 5. Ask the user

Say what you changed and ask them to confirm by ear. **Only they can judge
this.** A real case: `main` sounded wrong (`mˈaɪn`, "MA-in" in two syllables,
instead of `mˈeɪn`, "MEIN") and an automated comparison across 56 terms missed
it, because both renderings are five phonemes and start the same. It only shows
up out loud.

If they are still not convinced, go up a level: from nothing to
`foreign_terms`, or from `foreign_terms` to a hand-tuned `override`.

## Notes

- Plurals resolve themselves (`logs`, `hooks`, `branches` fall back to the singular).
- `claude-voice pron list` shows every active rule.
- If the user asks to **remove** a rule, confirm before deleting it.
- Changes take effect on the next spoken line; nothing needs restarting.
