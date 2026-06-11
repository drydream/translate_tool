# translate_tool — CLAUDE.md

## What it does

GUI app (tkinter) that translates a CSV's `english` column → `thai` column using
LM Studio's local API. Target: adult VN / RenPy games. Target model: Qwen3-14B-Instruct GGUF.

Current version: **2.0.1** (`APP_VERSION` in `translate.py`).

---

## Architecture

### Pipeline (v2.0, scene-aware)

```
CSV rows (pending: thai == '')
  → detect_scene_groups()        # group by scene, split into batches
  → ThreadPoolExecutor           # workers submit batches in parallel
      → _translate_batch()       # build prompt → POST → parse → validate → retry
          → clean_thai_output()  # strip artifacts
          → _validate_line()     # Thai presence + token set equality
  → df_lock write thai column
  → _safe_save() every N batches
```

### Key classes

| Class | Purpose |
|---|---|
| `CharacterMemory` | Runtime character profiles (name, gender, role, personality, pronouns). Seeded from world_setting text pre-run. Updated when speaker labels detected during translation. |
| `TerminologyDB` | Persistent JSON (`terminology.json`) of approved term translations. Top 60 injected into system prompt. Pass `TerminologyDB('')` to disable (no file load). |

### Key functions

| Function | Notes |
|---|---|
| `detect_scene_groups(pending, batch_size)` | Index gap >5 OR chapter/scene/act regex = new scene boundary. Returns list of batches. |
| `_translate_batch(...)` | 3-attempt retry loop. On `finish=length`: auto-splits batch in half, halves `context_lines` per level (10@ctx20 → 5@ctx10 → 2+3@ctx5 → 1@ctx2). Single-row fallback returns original English if all else fails. |
| `_parse_batch_response(raw, expected)` | Strips `<think>` blocks, tries numbered-line regex first, then line-count fallback with preamble filter. Returns `None` on mismatch → triggers retry. |
| `_validate_line(en, th)` | Thai char check + variable/tag token set equality. Skips `[...]` check for standalone speaker labels (`[Anna]` → `[แอนนา]` is valid). |
| `_build_chat_messages(...)` | Assembles system + user messages. Strips empty `**Label:**` fields from world_setting before injection. |
| `detect_speaker(text)` | Matches `Anna:` / `[Anna]` / `<Anna>` patterns. Used by CharacterMemory and _validate_line. |

---

## API

- Endpoint: `/v1/chat/completions` (NOT `/v1/completions` — that returns 400)
- Always send `"enable_thinking": false` — prevents Qwen3 thinking tokens from consuming output budget; thinking goes to separate `reasoning_content` field
- `max_tokens` = output-only budget (input prompt tokens don't count against it)

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
- **`[...]` validation collision**: `[player_name]` (Ren'Py interpolation, must preserve) vs `[Anna]` (speaker label, valid to transliterate). Resolved: skip `[...]` token check when `detect_speaker(en)` returns non-None.
- **Empty world setting fields**: `_build_chat_messages` strips lines matching `^\s*-?\s*\*\*[^*]+:\*\*\s*$` (e.g. `- **World Tone:** `) before injection.
- **translate_config.json**: saved next to exe/script. Not committed (contains game-specific paths). `terminology.json` is committed as empty `{}`.

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
