---
title: Pronunciation
---

# Pronunciation

When a word comes out wrong, fix it by ear.

```bash
claude-voice pron say "I merged into main"    # hear it
claude-voice pron diag main merge queue       # see both languages, get a fix
claude-voice pron list                        # what is currently overridden
```

!!! warning "An automated phoneme diff cannot do this for you"

    In the Spanish voice, "main" comes out as *MA-in* — two syllables — instead of *MEIN*. Both renderings are five phonemes, so a comparison that diffs phoneme counts sees nothing wrong. It only shows up by ear.

    That case is why the workbench prints a table for you to read rather than a verdict, and why the shipped term lists were curated by listening.

## The problem it solves

espeak takes one language per utterance. In Spanish, "merge" comes out MER-je and "queue" becomes KE-u-e — which is not an accent, it is a different word.

So synthesis runs in two passes:

1. **The primary language phonemizes the whole line.** That gets the prosody and the word boundaries right, which is the half that matters most and the half a per-word approach destroys.
2. **The configured foreign terms are re-phonemized and spliced in**, word by word, using the second espeak voice.

Piper voices share one IPA alphabet, so the splice works. The acoustic model never trained on those phonemes, so they come out accented — which is exactly how a bilingual developer actually says them.

## The two tiers

`claude-voice pron diag <word>` prints the exact TOML to paste, and tells you which tier the word belongs in.

=== "Tier 2 — foreign_terms"

    For words the *second* language says correctly. Just list them:

    ```toml
    [pronunciation]
    foreign_terms = ["main", "merge", "queue", "cache", "null", "pipeline"]
    ```

    This is the tier almost every fix belongs in. `foreign_voice` in `[tts]` decides which voice is spliced in; a blank `foreign_voice` disables the pass entirely, which is what the English pack does — the technical vocabulary is already English.

=== "Tier 1 — overrides"

    For words *neither* language gets right — product names, acronyms — where you write the IPA by hand:

    ```toml
    [pronunciation.overrides]
    kubectl = "kjuːb kəntɹˈoʊl"     # "kube-control", not "ku-BEKTL"
    nginx = "ˈɛndʒɪn ˈɛks"          # "engine-X"
    sudo = "sˈuðo"
    ```

    The string is raw espeak IPA and is used verbatim. It wins over everything, including `foreign_terms`.

Plurals fall back to the singular, so `hooks` is covered by an entry for `hook`.

## Do not overcorrect

The shipped Spanish pack deliberately leaves out *commit*, *deploy*, *refactor*, *endpoint*, *server*, *debug*, *package*, *install*, *token*, *script*, *test*, *lint*, *fix*, *bug*, *tag*, *fork*, *push*, *stash*, *checkout* and *log*.

Spanish already renders those the way a developer actually says them. Forcing English on them sounds affected, and a list that grows by reflex ends up making the voice worse.

The rule is: add a term because you heard it wrong, not because it is an English word.

## Checking your work

```bash
claude-voice pron say "I merged into main and the queue is empty"
```

Put the word in a realistic phrase rather than on its own — a word said alone gets a different stress pattern, and half of what sounds wrong is stress rather than phonemes. `pron say` also reports the duration and the peak level, and warns when the peak is low enough that you are about to blame the phonemes for a volume problem.

Then listen. That is the whole verification step, and there is no substitute for it.

## The pronunciation skill

The repository ships a Claude Code skill at `skills/pronunciation/` that walks this process: identify the word (ask, do not guess), diagnose it, pick the **lowest** tier that fixes it, verify in a realistic phrase, and ask you to confirm by ear.

Point Claude Code at it when a word sounds wrong and you would rather describe the problem than write IPA.

## Where the tables live

In the [language pack](languages.md), which is where they belong: a term list is a fact about a language, not about you. Your own additions go in `config.toml` — or, better, in a copy of the pack under `~/.config/claude-voice/presets/` keeping the same name, which survives upgrades and travels with the language rather than sitting on top of every language at once.
