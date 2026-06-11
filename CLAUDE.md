# translate_tool — CLAUDE.md

## What it does

GUI app (tkinter) that translates a CSV's `english` column → `thai` column using
LM Studio's local API. Target: adult VN / RenPy games. Target model: Qwen3-14B-Instruct GGUF.

Current version: **2.1.2** (`APP_VERSION` in `translate.py`).

---

## Architecture

### Pipeline (v2.1, scene-aware + token freezing)

```
CSV rows (pending: thai == '')
  → detect_scene_groups()        # group by scene, split into batches
  → ThreadPoolExecutor           # workers submit batches in parallel
      → _translate_batch()       # freeze → prompt → POST → parse → clean → thaw → validate
          → freeze_tokens()      # {tag}/[var]/%(x)s → ⟦N⟧ placeholders (pre-send)
          → clean_thai_output()  # strip artifacts (CJK leakage, annotations)
          → thaw_tokens()        # ⟦N⟧ → original tokens (post-clean)
          → _validate_line()     # Thai presence + token set equality
  → df_lock write thai column
  → _safe_save() every N batches
```

### Token freezing (v2.1 core mechanism)

Ren'Py markup like `{color=[KoGa3Color2]}[mc_name]{/color}` gets mangled by the
model AND must survive cleanup. `freeze_tokens()` replaces each token with `⟦N⟧`
before sending; `thaw_tokens()` restores them after cleaning. Speaker-label
lines (`[Anna]`) are NOT frozen — the name must be transliteratable.
Leftover `⟦` after thaw = model invented/dropped a placeholder → line invalid.

### Key classes

| Class | Purpose |
|---|---|
| `CharacterMemory` | Runtime character profiles (name, gender, role, personality, pronouns). Seeded from world_setting text pre-run. Updated when speaker labels detected during translation. |
| `TerminologyDB` | Persistent JSON (`terminology.json`) of approved term translations. Top 60 injected into system prompt. Pass `TerminologyDB('')` to disable (no file load). |

### Key functions

| Function | Notes |
|---|---|
| `detect_scene_groups(pending, batch_size)` | Index gap >5 OR chapter/scene/act regex = new scene boundary. Returns list of batches. |
| `freeze_tokens(en)` / `thaw_tokens(th, tokens)` | Replace/restore Ren'Py tokens with `⟦N⟧` placeholders. Skipped for speaker-label lines. |
| `_translate_batch(...)` | 3-attempt retry loop; each retry re-sends ONLY lines that haven't validated yet, with temperature nudged +0.15/attempt. On `finish=length` + parse fail: auto-splits batch in half, halves `context_lines` per level. On `finish=length` + parse OK: distrusts the last line (may be cut mid-sentence) and retries it. Lines that never validate keep original English (safe fallback — broken tags would crash Ren'Py). |
| `_parse_batch_response(raw, expected)` | Strips `<think>` blocks, tries numbered-line regex first, then line-count fallback with preamble filter + `N.` prefix strip. Returns `None` on mismatch → triggers retry. |
| `_validate_line(en, th)` | Thai char check + variable/tag token set equality. Skips `[...]` check for standalone speaker labels (`[Anna]` → `[แอนนา]` is valid). |
| `_learn_speaker(term_db, en, th)` | When a speaker-label line validates, records `Anna → แอนนา` in TerminologyDB so transliteration stays consistent across batches. |
| `_build_chat_messages(...)` | Assembles system + user messages. Strips empty `**Label:**` fields from world_setting before injection. |
| `detect_speaker(text)` | Matches `Anna:` / `[Anna]` / `<Anna>` patterns. Used by CharacterMemory, freeze_tokens, _validate_line. |

---

## API

- Endpoint: `/v1/chat/completions` (NOT `/v1/completions` — that returns 400; `_load_cfg` auto-migrates old configs)
- **`"enable_thinking": false` in the payload is IGNORED by LM Studio** (verified via `usage.completion_tokens_details.reasoning_tokens`). The working method is the Qwen3 soft switch: append `/no_think` to the user message (done in `_build_chat_messages`). Without it, thinking varies per batch and can consume the entire `max_tokens` budget → `finish=length, got ~0`.
- `max_tokens` = output-only budget (input prompt tokens don't count against it), but thinking tokens DO count against it

---

## Defaults

```python
DEFAULT_API_URL       = 'http://127.0.0.1:1234/v1/chat/completions'
DEFAULT_MODEL         = 'qwen3-14b-instruct'
DEFAULT_MAX_TOKENS    = 1500   # ~150 tokens/line × 10 lines
DEFAULT_WORKERS       = 2
DEFAULT_BATCH_SIZE    = 10     # use 5 for emoji-heavy / social media CSVs
DEFAULT_CONTEXT_LINES = 20
DEFAULT_TEMPERATURE   = 0.2
DEFAULT_TOP_P         = 0.9
DEFAULT_MIN_P         = 0.05
```

---

## Token budget tuning

| Content type | Batch size | max_tokens |
|---|---|---|
| Standard VN dialogue | 10 | 1500 |
| Emoji-heavy / social media | 5 | 1500 |
| Very long monologues | 5 | 2000 |

Rule: `finish=length` errors → reduce batch size first, not context lines.
Context lines are input tokens and don't cause output truncation.

If `finish=length` persists: check LM Studio context window setting.
Total budget = input tokens + max_tokens must fit in LM Studio's context window.
At context=2048, effective output = 2048 − ~700 input = ~1350, not 1500.
Set LM Studio context to 4096+.

---

## Known issues / invariants

- **Parallelism-safe context**: `df['english']` is read-only during translation; only `df['thai']` is written under `df_lock`. Context blocks read from `df['english']` without locking.
- **`[...]` validation collision**: `[player_name]` (Ren'Py interpolation, must preserve) vs `[Anna]` (speaker label, valid to transliterate). Resolved: skip `[...]` token check when `detect_speaker(en)` returns non-None; freeze_tokens also skips speaker-label lines.
- **Cleaner history (v2.1 fix)**: the old `clean_thai_output` whitelist stripped `{}[]`, emoji, and Latin letters — it destroyed correct model output (e.g. `{color=...}[mc_name]` → `32_`). v2.1 strips CJK leakage specifically instead; token freezing keeps Ren'Py markup out of the cleaner's reach entirely.
- **Empty world setting fields**: `_build_chat_messages` strips lines matching `^\s*-?\s*\*\*[^*]+:\*\*\s*$` (e.g. `- **World Tone:** `) before injection.
- **translate_config.json**: saved next to exe/script. Not committed (contains game-specific paths). `terminology.json` is committed as empty `{}` — gets auto-populated with learned speaker names at runtime.

## Tests

`python test_v210.py` — offline suite: freeze/thaw round-trip, cleaner behavior,
speaker learning, fake-session integration (retry-only-failed, English fallback,
length truncation handling). No LM Studio needed.

---

## Files

| File | Purpose |
|---|---|
| `translate.py` | Main app (single file) |
| `app_updater.py` | Auto-updater, checks GitHub releases |
| `terminology.json` | Persistent term DB (manually editable JSON `{"English": "Thai"}`) |
| `lmstudio_thai_translate_preset.json` | LM Studio preset: temp 0.2, top_p 0.9, min_p 0.05 |
| `translate.spec` | PyInstaller build spec |
| `translate_config.json` | Runtime config (gitignored) |

## Build

```
pip install pandas requests pyinstaller
pyinstaller translate.spec
```
Output: `dist\translate.exe`
