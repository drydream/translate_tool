"""Auto-translate CSV using LM Studio (scene-aware VN localization)."""
import concurrent.futures, json, os, queue, re, sys, threading, time
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
import pandas as pd
import requests

APP_VERSION = '2.1.1'
try:
    import app_updater
except Exception:
    app_updater = None

if getattr(sys, 'frozen', False):
    _HERE = os.path.dirname(sys.executable)
else:
    _HERE = os.path.dirname(os.path.abspath(__file__))
_tls            = threading.local()
CONFIG_FILE      = os.path.join(_HERE, 'translate_config.json')
TERMINOLOGY_FILE = os.path.join(_HERE, 'terminology.json')

DEFAULT_WORLD = (
    'When scientists discovered a portal to a parallel universe, everything changed.\n'
    'This other Earth looked identical—except sex was casual and constant.\n'
    'We dubbed it the "Freeuse World".'
)
DEFAULT_API_URL       = 'http://127.0.0.1:1234/v1/chat/completions'
DEFAULT_MODEL         = 'qwen3-14b-instruct'
DEFAULT_MAX_TOKENS    = 1500
DEFAULT_WORKERS       = 2
DEFAULT_BATCH_SIZE    = 10
DEFAULT_CONTEXT_LINES = 20
DEFAULT_TEMPERATURE   = 0.2
DEFAULT_TOP_P         = 0.9
DEFAULT_MIN_P         = 0.05
DEFAULT_MASTER_TEMPLATE = (
    '### [GLOBAL SETTING]\n'
    '- **Overview:** [Overview]\n'
    '- **Game Genre/Theme:** [Genre]\n'
    '- **World Tone:** [Tone]\n\n'
    '### [CURRENT SCENE CONTEXT]\n'
    '- **Location/Situation:** [Location]\n'
    '- **Active Characters:**\n'
    '  1. [Character A Name] | Role: [Character A Role] | Personality: [Character A Personality]\n'
    '  2. [Character B Name] | Role: [Character B Role] | Personality: [Character B Personality]\n\n'
    '### [PRONOUNS & TONE SPECIFICATION]\n'
    '- **[Character A Name] refers to self as:** [Character A Self Pronoun]\n'
    '- **[Character A Name] refers to [Character B Name] as:** [Character A to B Pronoun]\n'
    '- **[Character B Name] refers to self as:** [Character B Self Pronoun]\n'
    '- **[Character B Name] refers to [Character A Name] as:** [Character B to A Pronoun]\n'
    '- **Dialogue Vibe:** [Dialogue Vibe]'
)

GREEN  = '#1a7a1a'
RED    = '#b00000'
GRAY   = '#666666'
ORANGE = '#b05000'


# ─── Config I/O ───────────────────────────────────────────────────────────────

def _load_cfg():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception:
        return {}
    # Migrate pre-2.0 endpoint: /v1/completions returns 400 on LM Studio
    url = cfg.get('api_url', '')
    if url.rstrip('/').endswith('/v1/completions'):
        cfg['api_url'] = url.replace('/v1/completions', '/v1/chat/completions')
    return cfg


def _save_cfg(d):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _safe_save(df, path):
    tmp = path + '.tmp'
    df.to_csv(tmp, index=False, encoding='utf-8-sig')
    os.replace(tmp, path)


# ─── Output cleanup ───────────────────────────────────────────────────────────

# CJK leakage from the model (Chinese/Japanese/Korean) — never valid in Thai output
_CJK_RE = re.compile(r'[぀-ヿ㐀-䶿一-鿿가-힯]+')


def clean_thai_output(text, original_english):
    if not text:
        return ''
    text = re.split(r'//|Translation:|Note:|English:|หมายเหตุ:', text, flags=re.IGNORECASE)[0].strip()
    text = re.sub(r'\(เน้น[^)]*\)', '', text)
    text = re.sub(r'\(ใส่คำว่า[^)]*\)', '', text)
    text = re.sub(r'\(หรือ[^)]*\)', '', text)
    text = re.sub(r'\(หมายถึง[^)]*\)', '', text)
    text = re.sub(r'[(][฀-๿\s"\',:!?./\-]*[)]', '', text)
    text = re.sub(r'\([^)]*[a-zA-Z\s]{3,}[^)]*\)',
                  lambda m: m.group(0) if m.group(0) in original_english else '', text)
    text = _CJK_RE.sub('', text)
    text = re.sub(r'(.{4,}?)(?:[\s\-\:]+\1){2,}', r'\1', text)
    text = re.sub(r'\bกู\b', 'ฉัน', text)
    text = re.sub(r'\bมึง\b', 'เธอ', text)
    if '**' not in original_english:
        text = text.replace('**', '')
    text = re.sub(r'\.{4,}', '...', text)
    result = text.strip('"\' ')
    tag_m = re.search(r'(\([^฀-๿)]+\)(?:/\([^฀-๿)]+\))*)\s*$', original_english)
    if tag_m and tag_m.group(1) not in result and not result.rstrip().endswith(')'):
        result = result.rstrip() + ' ' + tag_m.group(1)
    return result


# ─── Speaker detection ────────────────────────────────────────────────────────

_SPEAKER_RE = re.compile(
    r'^(?:'
    r'([A-Za-z][A-Za-z0-9 _\'\-]{0,30})\s*:\s*$'
    r'|\[([A-Za-z][A-Za-z0-9 _\'\-]{0,30})\]\s*$'
    r'|<([A-Za-z][A-Za-z0-9 _\'\-]{0,30})>\s*$'
    r')'
)


def detect_speaker(text: str):
    m = _SPEAKER_RE.match(text.strip())
    if m:
        return next(g for g in m.groups() if g is not None)
    return None


# ─── Token freezing ───────────────────────────────────────────────────────────
# Ren'Py variables/tags ({color=[c]}, [mc_name], %(name)s …) get mangled by the
# model AND must survive cleanup untouched. Replace them with ⟦N⟧ placeholders
# before sending; restore after. Speaker-label lines ([Anna]) stay raw so the
# name can be transliterated.

_FREEZE_RE = re.compile(r'%\([^)]+\)[a-z]|%[sd]|\{[^{}]*\}|\[[^\[\]]*\]')


def freeze_tokens(en: str):
    if detect_speaker(en):
        return en, []
    tokens = []

    def _repl(m):
        tokens.append(m.group(0))
        return f'⟦{len(tokens)}⟧'

    return _FREEZE_RE.sub(_repl, en), tokens


def thaw_tokens(th: str, tokens: list) -> str:
    for i, tok in enumerate(tokens, 1):
        th = th.replace(f'⟦{i}⟧', tok)
    return th


# ─── Character memory ─────────────────────────────────────────────────────────

class CharacterMemory:
    def __init__(self):
        self._chars = {}
        self._lock = threading.Lock()

    def seed_from_world(self, world_text: str):
        current_name = None
        skip_starts = ('note', 'rule', 'warning', 'the ', 'a ', 'an ',
                       'when ', 'this ', 'we ', 'in ', 'after ', 'every ')
        for line in world_text.splitlines():
            line = line.strip()
            m = re.match(r'^([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)?)\s*:?\s*$', line)
            if m and not any(line.lower().startswith(w) for w in skip_starts):
                current_name = m.group(1)
                with self._lock:
                    self._chars.setdefault(current_name, {})
                continue
            if current_name and line and line[0] in '-•*':
                prop = line.lstrip('-•* ').lower()
                props = {}
                if any(w in prop for w in ('female', 'woman', 'girl', 'she/her')):
                    props['gender'] = 'female'
                elif any(w in prop for w in ('male', 'man', 'boy', 'he/him')):
                    props['gender'] = 'male'
                for p in ('shy', 'confident', 'aggressive', 'romantic', 'playful', 'serious', 'cheerful'):
                    if p in prop:
                        props['personality'] = p
                        break
                m2 = re.search(r'uses?\s+"([^"\']+)"', prop)
                if not m2:
                    m2 = re.search(r'uses?\s+([ฉผเธคนา]{1,4})', prop)
                if m2:
                    props['self_pronoun'] = m2.group(1)
                if props:
                    with self._lock:
                        self._chars[current_name].update(props)
            elif line and line[0] not in '-•*#':
                current_name = None

    def update(self, name: str, **kwargs):
        with self._lock:
            self._chars.setdefault(name, {}).update(kwargs)

    def all_names(self):
        with self._lock:
            return list(self._chars.keys())

    def count(self):
        with self._lock:
            return len(self._chars)

    def to_prompt_block(self) -> str:
        with self._lock:
            if not self._chars:
                return ''
            lines = ['CHARACTER PROFILES:']
            for name, props in self._chars.items():
                parts = [f'• {name}']
                for key in ('gender', 'role', 'personality'):
                    if key in props:
                        parts.append(props[key])
                if 'self_pronoun' in props:
                    parts.append(f'self="{props["self_pronoun"]}"')
                if 'speech_style' in props:
                    parts.append(props['speech_style'])
                lines.append(' | '.join(parts))
            return '\n'.join(lines)


# ─── Terminology DB ───────────────────────────────────────────────────────────

class TerminologyDB:
    def __init__(self, path: str = ''):
        self._path = path
        self._terms = {}
        self._lock = threading.Lock()
        if path:
            self._load()

    def _load(self):
        try:
            with open(self._path, 'r', encoding='utf-8') as f:
                self._terms = json.load(f)
        except Exception:
            self._terms = {}

    def save(self):
        if not self._path:
            return
        try:
            tmp = self._path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._terms, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            pass

    def add(self, en: str, th: str):
        with self._lock:
            self._terms[en] = th

    def count(self) -> int:
        with self._lock:
            return len(self._terms)

    def to_prompt_block(self) -> str:
        with self._lock:
            if not self._terms:
                return ''
            lines = ['APPROVED TERMINOLOGY (always use these exact translations):']
            for en, th in list(self._terms.items())[:60]:
                lines.append(f'  {en} → {th}')
            return '\n'.join(lines)


# ─── Scene / batch grouping ───────────────────────────────────────────────────

_CHAPTER_RE = re.compile(
    r'^\s*(?:chapter|scene|act|part|section|prologue|epilogue|interlude|day\s+\d|night\s+\d)\b',
    re.IGNORECASE,
)


def detect_scene_groups(pending_rows: list, batch_size: int) -> list:
    """Group (index, text) pairs into scenes then split into batches of batch_size."""
    if not pending_rows:
        return []
    scenes, cur, prev_idx = [], [], None
    for idx, text in pending_rows:
        new_scene = False
        if prev_idx is not None and idx - prev_idx > 5:
            new_scene = True
        elif _CHAPTER_RE.match(text):
            new_scene = True
        if new_scene and cur:
            scenes.append(cur)
            cur = []
        cur.append((idx, text))
        prev_idx = idx
    if cur:
        scenes.append(cur)
    batches = []
    for scene in scenes:
        for i in range(0, len(scene), batch_size):
            batches.append(scene[i:i + batch_size])
    return batches


# ─── Chat prompt builders ─────────────────────────────────────────────────────

def _strip_think(text: str) -> str:
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE).strip()


def _build_context_block(df, first_idx: int, context_lines: int) -> str:
    if context_lines <= 0 or first_idx <= 0:
        return ''
    before = df[df.index < first_idx].tail(context_lines)
    parts = [str(row.get('english', '')).strip()
             for _, row in before.iterrows()
             if str(row.get('english', '')).strip()]
    return '\n'.join(parts)


def _build_chat_messages(world: str, context_block: str,
                          char_memory: CharacterMemory, term_db: TerminologyDB,
                          batch_lines: list) -> list:
    sys_parts = [
        'You are a professional Thai visual novel localizer specializing in adult (18+) games. '
        'Translate each numbered English line into natural, immersive Thai. '
        'Output ONLY the numbered translations — no explanations, no notes, no markdown.',
    ]
    if world:
        # Drop empty template fields like "- **Genre:** " before injecting
        world_clean = '\n'.join(
            l for l in world.splitlines()
            if not re.match(r'^\s*-?\s*\*\*[^*]+:\*\*\s*$', l)
        ).strip()
        if world_clean:
            sys_parts.append(f'WORLD SETTING:\n{world_clean}')
    char_block = char_memory.to_prompt_block()
    if char_block:
        sys_parts.append(char_block)
    term_block = term_db.to_prompt_block()
    if term_block:
        sys_parts.append(term_block)
    sys_parts.append(
        'TRANSLATION RULES:\n'
        '1. Codes like ⟦1⟧ ⟦2⟧ are protected placeholders — copy each one into the Thai line '
        'at the matching position. Never translate, renumber, merge, or drop them\n'
        '2. Preserve any other variables exactly: %s  %d  %(name)s\n'
        '3. Pronouns: ฉัน/ผม (self), เธอ/คุณ/นาย (you) — NEVER use กู/มึง\n'
        '4. Match emotional intensity exactly — raw English = raw Thai, romantic = romantic\n'
        '5. Transliterate proper names phonetically into Thai script\n'
        '6. Adult vocabulary (mandatory, no euphemisms): fuck/sex=เย็ด, penis/cock=ควย, '
        'pussy/vagina=หี, cum/orgasm=แตก, tits/boobs=นม, horny=เงี่ยน, ass/butt=ตูด\n'
        '7. Speaker labels like "Anna:" — transliterate the name, keep the colon\n'
        '8. Output format: "N. <thai>" — one numbered line per input line, nothing else'
    )
    system_msg = '\n\n'.join(sys_parts)

    user_parts = []
    if context_block:
        user_parts.append(
            f'RECENT CONTEXT (reference only — do NOT re-translate):\n{context_block}'
        )
    numbered = '\n'.join(f'{i + 1}. {text}' for i, (_, text) in enumerate(batch_lines))
    user_parts.append(f'TRANSLATE THESE {len(batch_lines)} LINE(S):\n{numbered}')
    user_msg = '\n\n'.join(user_parts)

    return [
        {'role': 'system', 'content': system_msg},
        {'role': 'user',   'content': user_msg},
    ]


# ─── Batch response parsing & validation ─────────────────────────────────────

def _parse_batch_response(raw: str, expected: int):
    raw = _strip_think(raw).strip()
    matches = re.findall(r'^\s*(\d+)[.):\-]\s*(.+)', raw, re.MULTILINE)
    if matches:
        numbered = {int(n): t.strip() for n, t in matches}
        if all(i + 1 in numbered for i in range(expected)):
            return [numbered[i + 1] for i in range(expected)]
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    lines = [l for l in lines if not re.match(
        r'^(?:Note|Translation|English|Thai|หมายเหตุ|Here|Below|Sure|Certainly|Of course|Got it|Understood)[\s:,]+',
        l, re.IGNORECASE)]
    # Strip any leftover "N." prefixes; empty remainders stay (fail validation → retried)
    lines = [re.sub(r'^\d+[.):\-]\s*', '', l) for l in lines]
    if len(lines) == expected:
        return lines
    return None


def _validate_line(en: str, th: str) -> bool:
    if not th:
        return False
    # English leakage check (allow short lines / proper names)
    if not re.search(r'[฀-๿]', th):
        en_words = len(re.findall(r'[a-zA-Z]+', en))
        if en_words > 2:
            return False
    # Variable / tag preservation — skip [..] check for standalone speaker labels
    # because [Anna] → [แอนนา] is valid but token sets won't match
    if detect_speaker(en):
        token_re = r'%\([^)]+\)[a-z]|%[sd]|\{[^}]+\}'
    else:
        token_re = r'%\([^)]+\)[a-z]|%[sd]|\{[^}]+\}|\[[^\]]+\]'
    return set(re.findall(token_re, en)) == set(re.findall(token_re, th))


# ─── Core batch translator ────────────────────────────────────────────────────

def _learn_speaker(term_db: TerminologyDB, en: str, th: str):
    """Record name transliterations ([Anna] → [แอนนา]) so every batch spells them the same."""
    name_en = detect_speaker(en)
    if not name_en:
        return
    m = re.match(r'^(?:\[([^\]]+)\]|<([^>]+)>|(.+?)\s*:)\s*$', th.strip())
    if not m:
        return
    name_th = next((g for g in m.groups() if g), '').strip()
    if name_th and re.search(r'[฀-๿]', name_th):
        term_db.add(name_en, name_th)


def _translate_batch(session, api_url: str, model: str, world: str,
                     df, batch_rows: list,
                     char_memory: CharacterMemory, term_db: TerminologyDB,
                     max_tokens: int, temperature: float, top_p: float, min_p: float,
                     context_lines: int, stop_event, log_fn, batch_id: int) -> list:
    if not batch_rows:
        return []
    frozen = [freeze_tokens(en) for _, en in batch_rows]   # [(frozen_en, tokens), …]
    best = [None] * len(batch_rows)   # per-line: first translation that passed validation
    context_block = _build_context_block(df, batch_rows[0][0], context_lines)

    for attempt in range(3):
        if stop_event.is_set():
            break
        # Only re-send lines that haven't validated yet
        todo = [i for i, b in enumerate(best) if b is None]
        send_rows = [(batch_rows[i][0], frozen[i][0]) for i in todo]
        messages = _build_chat_messages(world, context_block, char_memory, term_db, send_rows)
        payload = {
            'model': model, 'messages': messages,
            # retries at temp 0.2 are near-deterministic — nudge so output can change
            'temperature': min(1.0, temperature + 0.15 * attempt),
            'top_p': top_p, 'min_p': min_p,
            'max_tokens': max_tokens, 'stream': False,
            'enable_thinking': False,
        }
        try:
            resp = session.post(api_url, json=payload, timeout=120)
            if resp.status_code != 200:
                log_fn(f'  Batch {batch_id}: HTTP {resp.status_code}, attempt {attempt + 1}')
                time.sleep(2)
                continue
            data = resp.json()['choices'][0]
            raw = data['message']['content']
            finish = data.get('finish_reason', '?')
            parsed = _parse_batch_response(raw, len(send_rows))
            if parsed is None:
                got = len([l for l in raw.strip().splitlines() if l.strip()])
                log_fn(f'  Batch {batch_id}: parse error – expected {len(send_rows)},'
                       f' got ~{got}, finish={finish}')
                if finish == 'length' and len(batch_rows) > 1:
                    # Output truncated — split batch in half, also halve context to shed overhead
                    mid = len(batch_rows) // 2
                    sub_ctx = max(0, context_lines // 2)
                    log_fn(f'  Batch {batch_id}: splitting {len(batch_rows)} → {mid}+{len(batch_rows)-mid}'
                           f' (context {context_lines}→{sub_ctx})')
                    a = _translate_batch(session, api_url, model, world, df,
                                        batch_rows[:mid], char_memory, term_db,
                                        max_tokens, temperature, top_p, min_p,
                                        sub_ctx, stop_event, log_fn, batch_id)
                    b = _translate_batch(session, api_url, model, world, df,
                                        batch_rows[mid:], char_memory, term_db,
                                        max_tokens, temperature, top_p, min_p,
                                        sub_ctx, stop_event, log_fn, batch_id)
                    return a + b
                time.sleep(1)
                continue
            for j, i in enumerate(todo):
                if finish == 'length' and j == len(todo) - 1:
                    continue   # last line may be cut mid-sentence — retry it
                en = batch_rows[i][1]
                th = thaw_tokens(clean_thai_output(parsed[j], frozen[i][0]), frozen[i][1])
                if '⟦' not in th and _validate_line(en, th):
                    best[i] = th
                    _learn_speaker(term_db, en, th)
                else:
                    log_fn(f'  Batch {batch_id}: val fail – en={en[:50]!r} th={th[:50]!r}')
            if all(b is not None for b in best):
                return best
            log_fn(f'  Batch {batch_id}: validation issues, attempt {attempt + 1}')
            time.sleep(1)
        except Exception as e:
            log_fn(f'  Batch {batch_id}: {e}, attempt {attempt + 1}')
            time.sleep(2)
    # Lines that never validated: keep original English (broken tags/vars
    # would crash Ren'Py; English is the safe fallback)
    out = []
    for b, row in zip(best, batch_rows):
        if b is None:
            log_fn(f'  Batch {batch_id}: keeping English for: {row[1][:60]!r}')
            out.append(row[1])
        else:
            out.append(b)
    return out


# ─── World-setting template parser (unchanged) ────────────────────────────────

def _parse_with_template(template, pasted):
    ph_re = re.compile(r'\[([^\]]+)\]')
    result = {}

    def _strip_md(s):
        return re.sub(r'\*+', '', s).strip().lstrip('-').strip()

    sections: dict = {}
    cur_heading = '_root'
    cur_lines: list = []
    for line in pasted.splitlines():
        if re.match(r'^#{1,4}\s+', line.strip()):
            sections[cur_heading] = cur_lines
            cur_heading = re.sub(r'^#{1,4}\s+', '', line.strip()).lower()
            cur_lines = []
        else:
            cur_lines.append(line)
    sections[cur_heading] = cur_lines

    def _first_table(lines):
        rows = []
        for ln in lines:
            s = ln.strip()
            if s.startswith('|') and s.endswith('|'):
                cells = [c.strip() for c in s[1:-1].split('|')]
                if all(re.fullmatch(r'[-: ]+', c) for c in cells if c):
                    continue
                rows.append(cells)
            elif rows:
                break
        return (rows[0], rows[1:]) if len(rows) >= 2 else (None, [])

    chars_key = next((k for k in sections if 'character' in k), None)
    search_lines = sections[chars_key] if chars_key else pasted.splitlines()
    header, data = _first_table(search_lines)
    char_names: list = []
    if header and data:
        h = [c.lower() for c in header]
        name_col = next((i for i, c in enumerate(h)
                         if any(k in c for k in ('name', 'character', 'ชื่อ'))), 0)
        num_col = next((i for i, c in enumerate(h)
                        if any(k in c for k in ('line', 'dialogue', 'count', 'total'))), None)
        if num_col is None:
            for col_i in range(len(h)):
                if col_i == name_col:
                    continue
                hits = sum(1 for r in data if len(r) > col_i
                           and re.fullmatch(r'\d+', re.sub(r'[,\s]', '', r[col_i])))
                if hits >= max(1, len(data) // 2):
                    num_col = col_i
                    break
        if num_col is not None:
            def _ck(row, nc=num_col):
                try:    return -int(re.sub(r'[^\d]', '', row[nc]))
                except: return 0
            data = sorted(data, key=_ck)
        char_names = [
            r[name_col] for r in data
            if len(r) > name_col
            and r[name_col]
            and re.search(r'[a-zA-Z฀-๿]', r[name_col])
            and not r[name_col].strip().startswith('*')
        ][:2]

    all_phs = list(dict.fromkeys(ph_re.findall(template)))
    char_a_ph = next((p for p in all_phs if 'character a' in p.lower() and 'name' in p.lower()), None)
    char_b_ph = next((p for p in all_phs if 'character b' in p.lower() and 'name' in p.lower()), None)
    char_a_name = char_names[0] if char_names else ''
    char_b_name = char_names[1] if len(char_names) >= 2 else ''
    if char_a_ph and char_a_name: result[char_a_ph] = char_a_name
    if char_b_ph and char_b_name: result[char_b_ph] = char_b_name

    game_title = ''
    for line in pasted.splitlines():
        s = line.strip()
        if not s.startswith('#'): continue
        m = re.match(r'^#{1,2}\s+World\s+Setting\s*[—\-–]\s*(.+)', s, re.IGNORECASE)
        if m: game_title = m.group(1).strip()
        break

    FUZZY_KEYS = [
        ('genre',    [r'genre\s*/\s*theme', r'game\s+genre', r'genre', r'theme']),
        ('tone',     [r'world\s+tone', r'tone']),
        ('location', [r'location\s*/\s*situation', r'location', r'situation', r'scene']),
        ('vibe',     [r'dialogue\s+vibe', r'vibe']),
        ('language', [r'target\s+language', r'language']),
    ]
    pasted_lines = [_strip_md(ln) for ln in pasted.splitlines() if ln.strip()]
    for ph in all_phs:
        if ph in result: continue
        ph_lower = ph.lower()
        for key, patterns in FUZZY_KEYS:
            if key not in ph_lower: continue
            for line in pasted_lines:
                for pat in patterns:
                    if re.search(pat, line, re.IGNORECASE):
                        colon_pos = line.find(':')
                        if colon_pos != -1:
                            val = _strip_md(line[colon_pos + 1:])
                            if val and not re.match(r'^\(fill', val, re.IGNORECASE):
                                result[ph] = val
                                break
                if ph in result: break
            if key == 'genre' and ph not in result and game_title:
                result[ph] = game_title
            break

    for ph in all_phs:
        if ph in result: continue
        pl = ph.lower()
        if   'character a' in pl and 'role' in pl:   result[ph] = 'Main Character'
        elif 'character b' in pl and 'role' in pl:   result[ph] = 'Supporting Character'
        elif 'personality' in pl:                     result[ph] = 'TBD'
        elif 'character a' in pl and 'self' in pl:   result[ph] = 'ฉัน'
        elif 'character b' in pl and 'self' in pl:   result[ph] = 'เธอ'
        elif 'character a' in pl and 'to b' in pl:   result[ph] = char_b_name or 'เธอ'
        elif 'character b' in pl and 'to a' in pl:   result[ph] = char_a_name or 'นาย'
    return result


# ─── GUI ─────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f'Auto-Translate  (LM Studio)  v{APP_VERSION}')
        self.minsize(700, 900)
        self.resizable(True, True)
        self._q    = queue.Queue()
        self._busy = False
        self._stop = threading.Event()
        cfg = _load_cfg()
        self._build(cfg)
        self._poll()
        if app_updater:
            app_updater.cleanup_after_update()
            self._startup_update_check()

    def _build(self, cfg):
        self._master_template = cfg.get('master_template', DEFAULT_MASTER_TEMPLATE)

        outer = ttk.Frame(self, padding=14)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(6, weight=1)  # log row

        # ── Files ─────────────────────────────────────────────────────────────
        ff = ttk.LabelFrame(outer, text='Files', padding=8)
        ff.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        ff.columnconfigure(1, weight=1)
        ttk.Label(ff, text='Source CSV:').grid(row=0, column=0, sticky='w')
        self._src_var = tk.StringVar(value=cfg.get('source', os.path.join(_HERE, 'translation.csv')))
        ttk.Entry(ff, textvariable=self._src_var).grid(row=0, column=1, sticky='ew', padx=6)
        ttk.Button(ff, text='Browse…',
                   command=lambda: self._pick_open(self._src_var)).grid(row=0, column=2)

        # ── API Settings ──────────────────────────────────────────────────────
        af = ttk.LabelFrame(outer, text='API Settings  (LM Studio)', padding=8)
        af.grid(row=1, column=0, sticky='ew', pady=(0, 8))
        af.columnconfigure(1, weight=1)

        ttk.Label(af, text='API URL:').grid(row=0, column=0, sticky='w')
        self._url_var = tk.StringVar(value=cfg.get('api_url', DEFAULT_API_URL))
        ttk.Entry(af, textvariable=self._url_var).grid(row=0, column=1, sticky='ew', padx=6)
        ttk.Button(af, text='Test', width=6,
                   command=self._do_test_connection).grid(row=0, column=2, padx=(0, 6))
        self._conn_lbl = ttk.Label(af, text='', width=14)
        self._conn_lbl.grid(row=0, column=3, sticky='w')

        ttk.Label(af, text='Model:').grid(row=1, column=0, sticky='w', pady=(4, 0))
        self._model_var = tk.StringVar(value=cfg.get('model', DEFAULT_MODEL))
        ttk.Entry(af, textvariable=self._model_var).grid(row=1, column=1, sticky='ew', padx=6, pady=(4, 0))
        ttk.Button(af, text='Detect', width=6,
                   command=self._do_detect_model).grid(row=1, column=2, padx=(0, 6), pady=(4, 0))
        self._detect_lbl = ttk.Label(af, text='', width=14)
        self._detect_lbl.grid(row=1, column=3, sticky='w', pady=(4, 0))

        ttk.Label(af, text='Max Tokens:').grid(row=2, column=0, sticky='w', pady=(4, 0))
        self._maxtok_var = tk.StringVar(value=str(cfg.get('max_tokens', DEFAULT_MAX_TOKENS)))
        ttk.Entry(af, textvariable=self._maxtok_var, width=8).grid(row=2, column=1, sticky='w', padx=6, pady=(4, 0))
        ttk.Label(af, text='(~80/line × batch size)', foreground=GRAY).grid(
            row=2, column=2, columnspan=2, sticky='w', padx=6, pady=(4, 0))

        ttk.Label(af, text='Workers:').grid(row=3, column=0, sticky='w', pady=(4, 0))
        self._workers_var = tk.StringVar(value=str(cfg.get('workers', DEFAULT_WORKERS)))
        ttk.Entry(af, textvariable=self._workers_var, width=8).grid(row=3, column=1, sticky='w', padx=6, pady=(4, 0))
        ttk.Label(af, text='(parallel batch requests — 1-2 recommended for batch mode)', foreground=GRAY).grid(
            row=3, column=2, columnspan=2, sticky='w', padx=6, pady=(4, 0))

        tmp_row = ttk.Frame(af)
        tmp_row.grid(row=4, column=0, columnspan=4, sticky='w', pady=(6, 0))
        ttk.Label(tmp_row, text='Temperature:').pack(side='left')
        self._temperature_var = tk.StringVar(value=str(cfg.get('temperature', DEFAULT_TEMPERATURE)))
        ttk.Entry(tmp_row, textvariable=self._temperature_var, width=6).pack(side='left', padx=(4, 12))
        ttk.Label(tmp_row, text='Top P:').pack(side='left')
        self._top_p_var = tk.StringVar(value=str(cfg.get('top_p', DEFAULT_TOP_P)))
        ttk.Entry(tmp_row, textvariable=self._top_p_var, width=6).pack(side='left', padx=(4, 12))
        ttk.Label(tmp_row, text='Min P:').pack(side='left')
        self._min_p_var = tk.StringVar(value=str(cfg.get('min_p', DEFAULT_MIN_P)))
        ttk.Entry(tmp_row, textvariable=self._min_p_var, width=6).pack(side='left', padx=(4, 6))
        ttk.Label(tmp_row, text='(Qwen3-14B defaults)', foreground=GRAY).pack(side='left')

        # ── World Setting ─────────────────────────────────────────────────────
        wf = ttk.LabelFrame(outer,
            text='World Setting  (game background lore used as AI translation context)', padding=8)
        wf.grid(row=2, column=0, sticky='ew', pady=(0, 8))
        wf.columnconfigure(0, weight=1)
        ttk.Label(wf,
            text='Edit the story background. The AI reads this before translating every batch.',
            foreground=GRAY).grid(row=0, column=0, sticky='w', pady=(0, 4))
        self._world_txt = scrolledtext.ScrolledText(wf, height=5, wrap='word')
        self._world_txt.grid(row=1, column=0, sticky='ew')
        self._world_txt.insert('1.0', cfg.get('world_setting', DEFAULT_WORLD))
        btn_bar = ttk.Frame(wf)
        btn_bar.grid(row=2, column=0, sticky='w', pady=(6, 0))
        ttk.Button(btn_bar, text='Edit Master Template',
                   command=self._open_edit_template).pack(side='left')
        ttk.Button(btn_bar, text='Fill Template Form',
                   command=self._open_fill_form).pack(side='left', padx=(8, 0))
        ttk.Button(btn_bar, text='Import world_setting.md',
                   command=self._import_world_setting).pack(side='left', padx=(8, 0))

        # ── Scene-Aware Settings ──────────────────────────────────────────────
        sf = ttk.LabelFrame(outer, text='Scene-Aware Settings', padding=8)
        sf.grid(row=3, column=0, sticky='ew', pady=(0, 8))

        chk_row = ttk.Frame(sf)
        chk_row.grid(row=0, column=0, sticky='w')
        self._scene_aware_var = tk.BooleanVar(value=cfg.get('scene_aware', True))
        self._char_mem_var    = tk.BooleanVar(value=cfg.get('use_char_mem', True))
        self._term_db_var     = tk.BooleanVar(value=cfg.get('use_term_db', True))
        self._ctx_mem_var     = tk.BooleanVar(value=cfg.get('use_context', True))
        ttk.Checkbutton(chk_row, text='Scene-Aware Mode',  variable=self._scene_aware_var).pack(side='left')
        ttk.Checkbutton(chk_row, text='Character Memory',  variable=self._char_mem_var).pack(side='left', padx=(12, 0))
        ttk.Checkbutton(chk_row, text='Terminology DB',    variable=self._term_db_var).pack(side='left', padx=(12, 0))
        ttk.Checkbutton(chk_row, text='Context Memory',    variable=self._ctx_mem_var).pack(side='left', padx=(12, 0))

        param_row = ttk.Frame(sf)
        param_row.grid(row=1, column=0, sticky='w', pady=(6, 0))
        ttk.Label(param_row, text='Batch Size:').pack(side='left')
        self._batch_size_var = tk.StringVar(value=str(cfg.get('batch_size', DEFAULT_BATCH_SIZE)))
        ttk.Spinbox(param_row, textvariable=self._batch_size_var,
                    from_=1, to=50, width=5).pack(side='left', padx=(4, 12))
        ttk.Label(param_row, text='Context Lines:').pack(side='left')
        self._ctx_lines_var = tk.StringVar(value=str(cfg.get('context_lines', DEFAULT_CONTEXT_LINES)))
        ttk.Spinbox(param_row, textvariable=self._ctx_lines_var,
                    from_=0, to=100, width=5).pack(side='left', padx=(4, 12))
        ttk.Label(param_row,
                  text='(context length = model load-time setting in LM Studio)',
                  foreground=GRAY).pack(side='left')

        status_row = ttk.Frame(sf)
        status_row.grid(row=2, column=0, sticky='w', pady=(6, 0))
        self._char_status_lbl  = ttk.Label(status_row, text='Characters: –', foreground=GRAY)
        self._char_status_lbl.pack(side='left')
        ttk.Label(status_row, text='  |  ', foreground=GRAY).pack(side='left')
        self._term_count_lbl   = ttk.Label(status_row, text='Terms: 0', foreground=GRAY)
        self._term_count_lbl.pack(side='left')
        ttk.Label(status_row, text='  |  ', foreground=GRAY).pack(side='left')
        self._scene_status_lbl = ttk.Label(status_row, text='Scene: –', foreground=GRAY)
        self._scene_status_lbl.pack(side='left')

        # ── Translation Mode ──────────────────────────────────────────────────
        mf = ttk.LabelFrame(outer, text='Translation Mode', padding=8)
        mf.grid(row=4, column=0, sticky='ew', pady=(0, 8))
        self._mode_var = tk.StringVar(value=cfg.get('mode', 'continue'))
        ttk.Radiobutton(mf,
            text='Continue from previous work  (sync new rows + resume untranslated)',
            variable=self._mode_var, value='continue').pack(anchor='w')
        ttk.Radiobutton(mf,
            text='Start fresh  (overwrite output CSV from scratch)',
            variable=self._mode_var, value='fresh').pack(anchor='w', pady=(4, 0))

        # ── Run ───────────────────────────────────────────────────────────────
        rf = ttk.LabelFrame(outer, text='Run', padding=8)
        rf.grid(row=5, column=0, sticky='ew', pady=(0, 8))
        rf.columnconfigure(0, weight=1)
        prog_row = ttk.Frame(rf)
        prog_row.grid(row=0, column=0, sticky='ew')
        prog_row.columnconfigure(1, weight=1)
        self._prog_lbl = ttk.Label(prog_row, text='Ready.', width=16, anchor='w')
        self._prog_lbl.grid(row=0, column=0, sticky='w')
        self._prog_bar = ttk.Progressbar(prog_row, orient='horizontal', mode='determinate')
        self._prog_bar.grid(row=0, column=1, sticky='ew', padx=(8, 0))
        btn_row = ttk.Frame(rf)
        btn_row.grid(row=1, column=0, sticky='w', pady=(8, 0))
        self._start_btn = ttk.Button(btn_row, text='Start Translating', command=self._do_start)
        self._start_btn.pack(side='left')
        self._stop_btn = ttk.Button(btn_row, text='Stop', command=self._do_stop, state='disabled')
        self._stop_btn.pack(side='left', padx=(8, 0))
        self._status_lbl = ttk.Label(rf, text='')
        self._status_lbl.grid(row=2, column=0, sticky='w', pady=(4, 0))

        # ── Log ───────────────────────────────────────────────────────────────
        lf = ttk.LabelFrame(outer, text='Log', padding=6)
        lf.grid(row=6, column=0, sticky='nsew')
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self._log = scrolledtext.ScrolledText(lf, height=10, wrap='word')
        self._log.grid(row=0, column=0, sticky='nsew')
        self._log.bind('<Key>', lambda e: 'break' if not (e.state & 4) and e.keysym not in (
            'Up', 'Down', 'Left', 'Right', 'Prior', 'Next', 'Home', 'End') else None)
        log_menu = tk.Menu(self._log, tearoff=0)
        log_menu.add_command(label='Copy',      command=lambda: self._log.event_generate('<<Copy>>'))
        log_menu.add_command(label='Select All', command=lambda: self._log.tag_add('sel', '1.0', 'end'))
        self._log.bind('<Button-3>', lambda e: log_menu.tk_popup(e.x_root, e.y_root))

        # ── Version / updates bar ─────────────────────────────────────────────
        bar = ttk.Frame(outer)
        bar.grid(row=7, column=0, sticky='ew', pady=(8, 0))
        self._upd_lbl = ttk.Label(bar, text=f'v{APP_VERSION}', foreground=GRAY)
        self._upd_lbl.pack(side='left')
        ttk.Button(bar, text='Check for Updates…', command=self._open_updater).pack(side='right')

    # ── Update / status helpers ───────────────────────────────────────────────

    def _update_memory_status(self, char_memory: CharacterMemory, term_db: TerminologyDB):
        names = char_memory.all_names()
        char_text = (', '.join(names[:4]) + ('…' if len(names) > 4 else '')) if names else '–'
        self._char_status_lbl.configure(text=f'Characters: {char_text}')
        self._term_count_lbl.configure(text=f'Terms: {term_db.count()}')

    def _update_scene_status(self, batch_id: int, total_batches: int):
        self._scene_status_lbl.configure(text=f'Scene: {batch_id + 1}/{total_batches}')

    # ── Updater ───────────────────────────────────────────────────────────────

    def _open_updater(self):
        if app_updater is None:
            messagebox.showerror('Error', 'app_updater.py not found next to the app.')
            return
        win = getattr(self, '_upd_win', None)
        if win is not None and win.winfo_exists():
            win.lift()
            return
        self._upd_win = app_updater.UpdateDialog(self, APP_VERSION)

    def _startup_update_check(self):
        def work():
            try:
                tag = app_updater.latest_release_tag()
                if tag and app_updater.is_newer(tag, APP_VERSION):
                    self._ui(lambda: self._upd_lbl.configure(
                        text=f'v{APP_VERSION}  —  Update available: {tag}', foreground=ORANGE))
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    # ── Queue / threading ─────────────────────────────────────────────────────

    def _poll(self):
        try:
            while True: self._q.get_nowait()()
        except queue.Empty: pass
        self.after(50, self._poll)

    def _ui(self, fn): self._q.put(fn)

    def _write_log(self, msg):
        at_bottom = self._log.yview()[1] >= 1.0
        self._log.insert('end', msg + '\n')
        if at_bottom:
            self._log.see('end')

    def _log_line(self, msg):
        self._ui(lambda m=msg: self._write_log(m))

    def _set_progress(self, val, total, label):
        self._ui(lambda: (
            self._prog_bar.configure(maximum=max(total, 1), value=val),
            self._prog_lbl.configure(text=label),
        ))

    def _finish(self, msg, ok):
        self._busy = False
        self._start_btn.configure(state='normal')
        self._stop_btn.configure(state='disabled')
        self._status_lbl.configure(text=msg, foreground=GREEN if ok else RED)
        self._scene_status_lbl.configure(text='Scene: –')

    # ── Browse helpers ────────────────────────────────────────────────────────

    def _pick_open(self, var):
        p = filedialog.askopenfilename(
            title='Select CSV file',
            filetypes=[('CSV files', '*.csv'), ('All files', '*.*')])
        if p: var.set(p)

    # ── Template management ───────────────────────────────────────────────────

    def _attach_context_menu(self, widget):
        is_text = isinstance(widget, (tk.Text, scrolledtext.ScrolledText))
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label='Cut',   command=lambda: widget.event_generate('<<Cut>>'))
        menu.add_command(label='Copy',  command=lambda: widget.event_generate('<<Copy>>'))
        menu.add_command(label='Paste', command=lambda: widget.event_generate('<<Paste>>'))
        menu.add_separator()
        if is_text:
            menu.add_command(label='Select All',
                             command=lambda: widget.tag_add('sel', '1.0', 'end'))
        else:
            menu.add_command(label='Select All',
                             command=lambda: widget.selection_range(0, 'end'))
        widget.bind('<Button-3>', lambda e: menu.tk_popup(e.x_root, e.y_root))

    def _open_edit_template(self):
        win = tk.Toplevel(self)
        win.title('Edit Master Template')
        win.geometry('700x500')
        win.grab_set()
        ttk.Label(win,
            text='Use [PlaceholderName] for dynamic fields — e.g. [Genre], [Location], [Character A Name].',
            foreground=GRAY).pack(anchor='w', padx=10, pady=(10, 4))
        txt = scrolledtext.ScrolledText(win, wrap='word')
        txt.pack(fill='both', expand=True, padx=10, pady=(0, 8))
        txt.insert('1.0', self._master_template)
        self._attach_context_menu(txt)
        def _save():
            self._master_template = txt.get('1.0', 'end').strip()
            cfg = _load_cfg()
            cfg['master_template'] = self._master_template
            _save_cfg(cfg)
            win.destroy()
        btn_row = ttk.Frame(win)
        btn_row.pack(fill='x', padx=10, pady=(0, 10))
        ttk.Button(btn_row, text='Cancel', command=win.destroy).pack(side='right')
        ttk.Button(btn_row, text='Save & Close', command=_save).pack(side='right', padx=(0, 6))

    def _open_fill_form(self):
        placeholders = list(dict.fromkeys(
            ph for ph in re.findall(r'\[([^\]]+)\]', self._master_template)
            if ph != ph.upper()
        ))
        if not placeholders:
            messagebox.showinfo('No Placeholders',
                'No [Placeholder] fields found in the Master Template.\n'
                'Click "Edit Master Template" to add some.')
            return
        win = tk.Toplevel(self)
        win.title('Fill Template Form')
        win.resizable(True, True)
        win.grab_set()
        toolbar = ttk.Frame(win, padding=(14, 10, 14, 0))
        toolbar.pack(fill='x')
        ttk.Button(toolbar, text='Paste Markdown…',
                   command=lambda: _open_paste_window()).pack(side='left')
        form = ttk.Frame(win, padding=14)
        form.pack(fill='both', expand=True)
        _MULTILINE = {'overview'}
        entries, txt_entries = {}, {}
        for i, ph in enumerate(placeholders):
            ttk.Label(form, text=ph + ':').grid(row=i, column=0, sticky='nw', pady=4, padx=(0, 10))
            if ph.lower() in _MULTILINE:
                w = scrolledtext.ScrolledText(form, height=4, wrap='word', width=44)
                w.grid(row=i, column=1, sticky='ew', pady=4)
                self._attach_context_menu(w)
                txt_entries[ph] = w
            else:
                var = tk.StringVar()
                ent = ttk.Entry(form, textvariable=var, width=44)
                ent.grid(row=i, column=1, sticky='ew', pady=4)
                self._attach_context_menu(ent)
                entries[ph] = var
        form.columnconfigure(1, weight=1)

        def _set_entry(ph, val):
            if ph in entries:        entries[ph].set(val)
            elif ph in txt_entries:
                txt_entries[ph].delete('1.0', 'end')
                txt_entries[ph].insert('1.0', val)

        _world = self._world_txt.get('1.0', 'end').strip()
        if _world:
            for ph, val in _parse_with_template(self._master_template, _world).items():
                _set_entry(ph, val)

        def _open_paste_window():
            sub = tk.Toplevel(win)
            sub.title('Paste Markdown')
            sub.geometry('640x420')
            sub.grab_set()
            ttk.Label(sub, text='Paste the full Markdown block below, then click "Parse & Fill":',
                foreground=GRAY).pack(anchor='w', padx=10, pady=(10, 4))
            def _parse_and_fill():
                for ph, val in _parse_with_template(
                        self._master_template, txt.get('1.0', 'end')).items():
                    _set_entry(ph, val)
                sub.destroy()
            sub_btns = ttk.Frame(sub)
            sub_btns.pack(fill='x', padx=10, pady=(0, 10), side='bottom')
            ttk.Button(sub_btns, text='Cancel', command=sub.destroy).pack(side='right')
            ttk.Button(sub_btns, text='Parse & Fill', command=_parse_and_fill).pack(side='right', padx=(0, 6))
            txt = scrolledtext.ScrolledText(sub, wrap='word')
            txt.pack(fill='both', expand=True, padx=10, pady=(0, 8))
            self._attach_context_menu(txt)

        def _apply():
            result = self._master_template
            for ph, var in entries.items():     result = result.replace(f'[{ph}]', var.get())
            for ph, w in txt_entries.items():   result = result.replace(f'[{ph}]', w.get('1.0', 'end').strip())
            self._world_txt.delete('1.0', 'end')
            self._world_txt.insert('1.0', result)
            win.destroy()

        btn_row = ttk.Frame(win, padding=(14, 0, 14, 14))
        btn_row.pack(fill='x')
        ttk.Button(btn_row, text='Cancel', command=win.destroy).pack(side='right')
        ttk.Button(btn_row, text='Apply', command=_apply).pack(side='right', padx=(0, 6))

    def _import_world_setting(self):
        p = filedialog.askopenfilename(
            title='Import world_setting.md', initialfile='world_setting.md',
            filetypes=[('Markdown files', '*.md'), ('All files', '*.*')])
        if not p: return
        try:
            with open(p, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            messagebox.showerror('Error', f'Could not read file:\n{e}')
            return
        self._world_txt.delete('1.0', 'end')
        self._world_txt.insert('1.0', content.strip())

    # ── Actions ───────────────────────────────────────────────────────────────

    def _do_start(self):
        if self._busy: return
        src = self._src_var.get().strip()
        if not src or not os.path.isfile(src):
            messagebox.showerror('Error', f'Source CSV not found:\n{src}')
            return

        out     = os.path.splitext(src)[0] + '_th.csv'
        world   = self._world_txt.get('1.0', 'end').strip()
        api_url = self._url_var.get().strip()
        model   = self._model_var.get().strip()
        mode    = self._mode_var.get()

        try:    max_tokens = int(self._maxtok_var.get())
        except: max_tokens = DEFAULT_MAX_TOKENS
        try:    workers = max(1, int(self._workers_var.get()))
        except: workers = DEFAULT_WORKERS
        try:    batch_size = max(1, int(self._batch_size_var.get()))
        except: batch_size = DEFAULT_BATCH_SIZE
        try:    context_lines = max(0, int(self._ctx_lines_var.get()))
        except: context_lines = DEFAULT_CONTEXT_LINES
        try:    temperature = float(self._temperature_var.get())
        except: temperature = DEFAULT_TEMPERATURE
        try:    top_p = float(self._top_p_var.get())
        except: top_p = DEFAULT_TOP_P
        try:    min_p = float(self._min_p_var.get())
        except: min_p = DEFAULT_MIN_P

        scene_aware  = self._scene_aware_var.get()
        use_char_mem = self._char_mem_var.get()
        use_term_db  = self._term_db_var.get()
        use_context  = self._ctx_mem_var.get()

        _save_cfg({
            'source': src, 'api_url': api_url, 'model': model,
            'world_setting': world, 'mode': mode,
            'max_tokens': max_tokens, 'workers': workers,
            'master_template': self._master_template,
            'batch_size': batch_size, 'context_lines': context_lines,
            'scene_aware': scene_aware, 'use_char_mem': use_char_mem,
            'use_term_db': use_term_db, 'use_context': use_context,
            'temperature': temperature, 'top_p': top_p, 'min_p': min_p,
        })

        self._busy = True
        self._stop.clear()
        self._start_btn.configure(state='disabled')
        self._stop_btn.configure(state='normal')
        self._status_lbl.configure(text='Running…', foreground=ORANGE)
        self._write_log('\n── Starting translation ──')

        threading.Thread(
            target=self._worker,
            args=(src, out, world, api_url, model, mode, max_tokens, workers,
                  scene_aware, use_char_mem, use_term_db, use_context,
                  batch_size, context_lines, temperature, top_p, min_p),
            daemon=True).start()

    def _do_stop(self):
        self._stop.set()
        self._ui(lambda: self._status_lbl.configure(text='Stopping…', foreground=ORANGE))

    def _do_test_connection(self):
        url   = self._url_var.get().strip()
        model = self._model_var.get().strip()
        self._conn_lbl.configure(text='Testing…', foreground=ORANGE)
        def test():
            try:
                resp = requests.post(url, json={
                    'model': model,
                    'messages': [{'role': 'user', 'content': 'Hi'}],
                    'max_tokens': 1, 'stream': False, 'enable_thinking': False,
                }, timeout=5)
                if resp.status_code == 200:
                    self._ui(lambda: self._conn_lbl.configure(text='✓ Connected', foreground=GREEN))
                else:
                    self._ui(lambda s=resp.status_code: self._conn_lbl.configure(
                        text=f'✗ Error {s}', foreground=RED))
            except Exception:
                self._ui(lambda: self._conn_lbl.configure(text='✗ Unreachable', foreground=RED))
        threading.Thread(target=test, daemon=True).start()

    def _do_detect_model(self):
        url = self._url_var.get().strip()
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        models_url = urlunparse(parsed._replace(path='/v1/models', query='', fragment=''))
        self._detect_lbl.configure(text='Detecting…', foreground=ORANGE)
        def detect():
            try:
                resp = requests.get(models_url, timeout=5)
                if resp.status_code == 200:
                    data = resp.json().get('data', [])
                    if data:
                        model_id = data[0]['id']
                        self._ui(lambda m=model_id: (
                            self._model_var.set(m),
                            self._detect_lbl.configure(text='✓ Detected', foreground=GREEN),
                        ))
                    else:
                        self._ui(lambda: self._detect_lbl.configure(text='✗ No model', foreground=RED))
                else:
                    self._ui(lambda: self._detect_lbl.configure(text='✗ No model', foreground=RED))
            except Exception:
                self._ui(lambda: self._detect_lbl.configure(text='✗ Unreachable', foreground=RED))
        threading.Thread(target=detect, daemon=True).start()

    # ── Background worker ─────────────────────────────────────────────────────

    def _worker(self, src, out, world, api_url, model, mode, max_tokens, workers,
                scene_aware, use_char_mem, use_term_db, use_context,
                batch_size, context_lines, temperature, top_p, min_p):

        # ── Load source ───────────────────────────────────────────────────────
        try:
            df_src = pd.read_csv(src, dtype=str).fillna('')
            df_src['english'] = df_src['english'].str.strip()
        except Exception as e:
            self._log_line(f'ERROR reading source CSV: {e}')
            self._ui(lambda: self._finish(f'Error: {e}', False))
            return

        # ── Build working dataframe ───────────────────────────────────────────
        if mode == 'continue' and os.path.isfile(out):
            self._log_line('Syncing with previous work…')
            try:
                df_prev = pd.read_csv(out, dtype=str).fillna('')
                df_prev['english'] = df_prev['english'].str.strip()
                existing = set(df_prev['english'].tolist())
                new_rows = df_src[~df_src['english'].isin(existing)].copy()
                if len(new_rows):
                    if 'thai' not in new_rows.columns: new_rows['thai'] = ''
                    df = pd.concat([df_prev, new_rows], ignore_index=True)
                    self._log_line(f'  {len(new_rows)} new rows merged.')
                else:
                    df = df_prev
                    self._log_line('  No new rows. Resuming from previous work.')
            except Exception as e:
                self._log_line(f'WARNING: could not load previous output ({e}), starting fresh.')
                df = df_src.copy()
                df['thai'] = ''
        else:
            self._log_line('Starting fresh…')
            df = df_src.copy()
            df['thai'] = ''

        if 'thai' not in df.columns:
            df['thai'] = ''

        pending_idx  = df[(df['english'] != '') & (df['thai'].str.strip() == '')].index.tolist()
        pending_rows = [(idx, df.at[idx, 'english']) for idx in pending_idx]
        total        = len(pending_rows)

        self._log_line(f'Total rows: {len(df)}  |  Pending: {total}  |  Workers: {workers}')
        self._set_progress(0, total, f'0 / {total}')

        if total == 0:
            self._log_line('All rows already translated.')
            _safe_save(df, out)
            self._ui(lambda: self._finish('Nothing to translate — all done!', True))
            return

        # ── Memory systems ────────────────────────────────────────────────────
        char_memory = CharacterMemory()
        if use_char_mem:
            char_memory.seed_from_world(world)
            names = char_memory.all_names()
            if names:
                self._log_line(f'Character memory seeded: {", ".join(names)}')

        term_db = TerminologyDB(TERMINOLOGY_FILE if use_term_db else '')
        self._ui(lambda: self._update_memory_status(char_memory, term_db))

        # ── Batch grouping ────────────────────────────────────────────────────
        eff_batch   = batch_size if scene_aware else 1
        eff_context = context_lines if (scene_aware and use_context) else 0
        batches     = detect_scene_groups(pending_rows, eff_batch)
        n_batches   = len(batches)
        self._log_line(f'Batches: {n_batches}  |  Batch size: {eff_batch}  |  Context: {eff_context} lines')

        counters = [0]
        df_lock  = threading.Lock()
        t_start  = time.time()
        save_every = max(1, workers)

        def process_batch(args):
            batch_id, batch = args
            if self._stop.is_set():
                return
            if not hasattr(_tls, 'session'):
                _tls.session = requests.Session()

            results = _translate_batch(
                _tls.session, api_url, model, world, df, batch,
                char_memory, term_db, max_tokens,
                temperature, top_p, min_p,
                eff_context, self._stop, self._log_line, batch_id,
            )

            with df_lock:
                for (idx, _), th in zip(batch, results):
                    df.at[idx, 'thai'] = th
                counters[0] += len(batch)
                d    = counters[0]
                snap = df.copy() if (batch_id + 1) % save_every == 0 else None

            if snap is not None:
                _safe_save(snap, out)

            if use_char_mem:
                for _, en in batch:
                    spk = detect_speaker(en)
                    if spk:
                        char_memory.update(spk, seen=True)
                self._ui(lambda: self._update_memory_status(char_memory, term_db))

            self._ui(lambda bi=batch_id: self._update_scene_status(bi, n_batches))

            elapsed = time.time() - t_start
            rpm_str = f'  {d / elapsed * 60:.0f} lines/min' if elapsed > 2 else ''
            first_en = batch[0][1][:55] if batch else ''
            first_th = results[0][:55] if results else ''
            self._log_line(f'[{d}/{total}] {first_en} ➔ {first_th}{rpm_str}')
            self._set_progress(d, total, f'{d} / {total}{rpm_str}')

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(process_batch, (i, batch))
                       for i, batch in enumerate(batches)]
            for future in concurrent.futures.as_completed(futures):
                if self._stop.is_set():
                    for f in futures: f.cancel()
                    self._log_line('Stopped by user.')
                    break

        if use_term_db:
            term_db.save()

        _safe_save(df, out)
        d   = counters[0]
        msg = f'Done — {d} rows translated  →  {out}'
        self._log_line(f'\n{msg}')
        self._ui(lambda m=msg: self._finish(m, True))


if __name__ == '__main__':
    App().mainloop()
