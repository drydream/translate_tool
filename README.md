# Auto-Translate Tool (LM Studio)

Standalone Windows app that auto-translates CSV files (English→Thai) using a
local LM Studio API. Companion app to
[renpy_extract](https://github.com/drydream/renpy_extract) — translate the CSV
it produces, then apply it back to the game.

**[⬇ Download latest version](https://github.com/drydream/translate_tool/releases/latest)**

## Features

- Translates the `thai` column of a CSV row-by-row through LM Studio's completions API
- Parallel workers with live progress bar and stop button
- Master prompt template with world/character/pronoun context (editable in-app)
- Settings saved automatically next to the exe (`translate_config.json`)
- Built-in auto-updater with version rollback

## How to use

1. Start [LM Studio](https://lmstudio.ai/), load a model, start the local server
2. Open `translate.exe`, pick the source CSV (from renpy_extract)
3. Adjust API URL / model / workers if needed
4. Click **Start Translating**

A ready-made LM Studio preset is included: `lmstudio_thai_translate_preset.json`

## Build from source

```
pip install pandas requests pyinstaller
pyinstaller --onefile --noconsole --name translate translate.py
```

Output: `dist\translate.exe`
