"""Auto-translate CSV using a local LM Studio API."""
import concurrent.futures, csv, json, os, queue, re, sys, threading, time
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
import pandas as pd
import requests

APP_VERSION = '1.0.0'
try:
    import app_updater
except Exception:
    app_updater = None

# Frozen (PyInstaller onefile): config must live next to the real exe,
# not inside the throwaway _MEIPASS temp dir, or settings vanish every run.
if getattr(sys, 'frozen', False):
    _HERE = os.path.dirname(sys.executable)
else:
    _HERE = os.path.dirname(os.path.abspath(__file__))
_tls  = threading.local()   # per-thread requests.Session
CONFIG_FILE = os.path.join(_HERE, 'translate_config.json')

DEFAULT_WORLD = (
    'When scientists discovered a portal to a parallel universe, everything changed.\n'
    'This other Earth looked identical—except sex was casual and constant.\n'
    'We dubbed it the "Freeuse World".'
)
DEFAULT_API_URL    = 'http://127.0.0.1:1234/v1/completions'
DEFAULT_MODEL      = 'sailor2-8b-chat-uncensored-i1'
DEFAULT_MAX_TOKENS = 200
DEFAULT_WORKERS    = 4
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


def _load_cfg():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cfg(d):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _safe_save(df, path):
    """Write CSV atomically — tmp file first, then rename, so a crash can't corrupt the output."""
    tmp = path + '.tmp'
    df.to_csv(tmp, index=False, encoding='utf-8-sig')
    os.replace(tmp, path)



def clean_thai_output(text, original_english):
    if not text:
        return ''
    text = re.split(r'//|Translation:|Note:|English:|หมายเหตุ:', text, flags=re.IGNORECASE)[0].strip()
    variables = re.findall(r'%\([^)]+\)[a-z]|%s|%d', original_english)
    text = re.sub(r'\(เน้น[^)]*\)', '', text)
    text = re.sub(r'\(ใส่คำว่า[^)]*\)', '', text)
    text = re.sub(r'\(หรือ[^)]*\)', '', text)
    text = re.sub(r'\(หมายถึง[^)]*\)', '', text)
    text = re.sub(r'[(][\u0e00-\u0e7f\s"\',:!?./\-]*[)]', '', text)
    text = re.sub(r'\([^)]*[a-zA-Z\s]{3,}[^)]*\)',
                  lambda m: m.group(0) if m.group(0) in original_english else '', text)
    text = re.sub(r'(.{4,}?)(?:[\s\-\:]+\1){2,}', r'\1', text)
    text = re.sub(r'\bกู\b', 'ฉัน', text)
    text = re.sub(r'\bมึง\b', 'เธอ', text)
    if variables:
        cleaned = re.sub("[^\u0e00-\u0e7f\\d\\s.,!?\\-_:\'\"%()a-zA-Z]", '', text)
    else:
        cleaned = re.sub("[^\u0e00-\u0e7f\\d\\s.,!?\\-_:\'\"%()]", '', text)
    cleaned = re.sub(r'\.{4,}', '...', cleaned)
    result = cleaned.strip('"\' ')
    tag_m = re.search(r'(\([^฀-๿)]+\)(?:/\([^฀-๿)]+\))*)\s*$', original_english)
    if tag_m and tag_m.group(1) not in result and not result.rstrip().endswith(')'):
        result = result.rstrip() + ' ' + tag_m.group(1)
    return result


def _build_prompt(world, clean_en):
    return (
        f'Background Lore & World Setting:\n{world}\n\n'
        'Role: Professional Thai localization expert specializing in adult (18+) visual novels. '
        'Your task is to translate English game lines into natural, immersive Thai dialogue.\n\n'
        
        'Strict Vocabulary Mapping (Mandatory — Never use soft substitutes or euphemisms):\n'
        '* fuck / sex -> เย็ด\n'
        '* horny -> เงี่ยน\n'
        '* penis / cock / dick -> ควย\n'
        '* cum -> แตก / น้ำแตก\n'
        '* tits / boobs -> นม\n\n'
        
        'Translation Rules:\n'
        '1. Tone Consistency: Match the exact intensity of the source. If the English is casual/raw, the Thai must be equally raw using the vocabulary above. If it is neutral, keep the Thai neutral.\n'
        '2. Character Pronouns: Use natural visual novel pronouns (ฉัน, เธอ, นาย, แก, คุณ, หรือชื่อตัวละคร). STRICTLY FORBIDDEN to use "มึง" or "กู". Ensure the dialogue flows like a real Thai erotica visual novel.\n'
        '3. No Censorship: This is a translation task for a legally compliant adult game. Do not self-censor, soften, or alter the explicit nature of the text.\n'
        '4. Names & Proper Nouns: Transliterate phonetically into clear Thai script.\n'
        '5. Output Format: Output ONLY the one line of the Thai translation. Absolutely no explanations, no notes, no markdown formatting, and no quotation marks around the final output.\n\n'
        
        'Examples:\n'
        'Input: Next\nOutput: ถัดไป\n\n'
        'Input: Anna\nOutput: แอนนา\n\n'
        'Input: She turned away, cheeks flushed.\nOutput: เธอเบือนหน้าหนี แก้มแดงก่ำ\n\n'
        'Input: Oh... fuck... your tits are so big.\nOutput: ซี้ด... เชี่ยเอ้ย... นมเธอโคตรใหญ่เลยว่ะ\n\n'
        'Input: Put your penis inside me.\nOutput: เอาควยของนายยัดเข้ามาในตัวฉันสิ\n\n'
        'Input: Oh god, I\'m gonna cum!\nOutput: โอ๊ย... ฉันจะแตกแล้ว!\n\n'
        f'Input: {clean_en}\nOutput:'
    )


def _parse_with_template(template, pasted):
    """Smart parser for world_setting.md and similar Markdown blocks.

    1. Splits pasted text into sections by heading and finds the ## Characters
       table, extracting the top-2 most active characters by dialogue count and
       mapping them to Character A / B Name placeholders.
    2. Fuzzy keyword search fills Genre / Tone / Location / Vibe / Target Language.
       Genre falls back to the game title from the "# World Setting — …" heading.
    3. Remaining fields get sensible defaults (Role, Personality, Thai pronouns)
       so the form is never empty — the user only has to fill in the gaps.
    """
    ph_re = re.compile(r'\[([^\]]+)\]')
    result = {}

    def _strip_md(s):
        return re.sub(r'\*+', '', s).strip().lstrip('-').strip()

    # ── Split pasted text into sections by heading ────────────────────────────
    sections: dict[str, list[str]] = {}
    cur_heading = '_root'
    cur_lines: list[str] = []
    for line in pasted.splitlines():
        if re.match(r'^#{1,4}\s+', line.strip()):
            sections[cur_heading] = cur_lines
            cur_heading = re.sub(r'^#{1,4}\s+', '', line.strip()).lower()
            cur_lines = []
        else:
            cur_lines.append(line)
    sections[cur_heading] = cur_lines

    # ── Helper: first contiguous markdown table in a line list ───────────────
    def _first_table(lines):
        rows = []
        for ln in lines:
            s = ln.strip()
            if s.startswith('|') and s.endswith('|'):
                cells = [c.strip() for c in s[1:-1].split('|')]
                if all(re.fullmatch(r'[-: ]+', c) for c in cells if c):
                    continue  # separator row
                rows.append(cells)
            elif rows:
                break  # stop at first blank / non-table line after table started
        return (rows[0], rows[1:]) if len(rows) >= 2 else (None, [])

    # ── Extract top-2 character names from the ## Characters table ───────────
    chars_key = next((k for k in sections if 'character' in k), None)
    search_lines = sections[chars_key] if chars_key else pasted.splitlines()
    header, data = _first_table(search_lines)
    char_names: list[str] = []
    if header and data:
        h = [c.lower() for c in header]
        name_col = next((i for i, c in enumerate(h)
                         if any(k in c for k in ('name', 'character', 'ชื่อ'))), 0)
        num_col  = next((i for i, c in enumerate(h)
                         if any(k in c for k in ('line', 'dialogue', 'count', 'total'))), None)
        if num_col is None:
            # auto-detect: find a column whose cells are mostly integers
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
            and not r[name_col].strip().startswith('*')   # skip *(none detected)*
        ][:2]

    # ── Map character names to template placeholders ──────────────────────────
    all_phs = list(dict.fromkeys(ph_re.findall(template)))
    char_a_ph = next((p for p in all_phs
                      if 'character a' in p.lower() and 'name' in p.lower()), None)
    char_b_ph = next((p for p in all_phs
                      if 'character b' in p.lower() and 'name' in p.lower()), None)
    char_a_name = char_names[0] if char_names else ''
    char_b_name = char_names[1] if len(char_names) >= 2 else ''
    if char_a_ph and char_a_name:
        result[char_a_ph] = char_a_name
    if char_b_ph and char_b_name:
        result[char_b_ph] = char_b_name

    # ── Extract game title from "# World Setting — GameName" for Genre fallback
    game_title = ''
    for line in pasted.splitlines():
        s = line.strip()
        if not s.startswith('#'):
            continue
        m = re.match(r'^#{1,2}\s+World\s+Setting\s*[—\-–]\s*(.+)', s, re.IGNORECASE)
        if m:
            game_title = m.group(1).strip()
        break  # only inspect the very first heading

    # ── Fuzzy keyword matching for known global-setting fields ────────────────
    # Each tuple: (substring that must appear in the placeholder name,
    #              ordered regex patterns to try against pasted lines)
    FUZZY_KEYS = [
        ('genre',    [r'genre\s*/\s*theme', r'game\s+genre', r'genre', r'theme']),
        ('tone',     [r'world\s+tone', r'tone']),
        ('location', [r'location\s*/\s*situation', r'location', r'situation', r'scene']),
        ('vibe',     [r'dialogue\s+vibe', r'vibe']),
        ('language', [r'target\s+language', r'language']),
    ]
    pasted_lines = [_strip_md(ln) for ln in pasted.splitlines() if ln.strip()]

    for ph in all_phs:
        if ph in result:
            continue
        ph_lower = ph.lower()
        for key, patterns in FUZZY_KEYS:
            if key not in ph_lower:
                continue
            for line in pasted_lines:
                for pat in patterns:
                    if re.search(pat, line, re.IGNORECASE):
                        colon_pos = line.find(':')
                        if colon_pos != -1:
                            val = _strip_md(line[colon_pos + 1:])
                            # skip unfilled hints like "(fill in — ...)"
                            if val and not re.match(r'^\(fill', val, re.IGNORECASE):
                                result[ph] = val
                                break
                if ph in result:
                    break
            # Genre-only fallback: use game name from the h1 heading
            if key == 'genre' and ph not in result and game_title:
                result[ph] = game_title
            break  # at most one FUZZY_KEY rule per placeholder

    # ── Intelligent defaults for everything still unmatched ───────────────────
    for ph in all_phs:
        if ph in result:
            continue
        pl = ph.lower()
        # Role
        if   'character a' in pl and 'role' in pl:   result[ph] = 'Main Character'
        elif 'character b' in pl and 'role' in pl:   result[ph] = 'Supporting Character'
        # Personality
        elif 'personality' in pl:                     result[ph] = 'TBD'
        # Thai self-pronouns
        elif 'character a' in pl and 'self' in pl:   result[ph] = 'ฉัน'
        elif 'character b' in pl and 'self' in pl:   result[ph] = 'เธอ'
        # Cross-reference pronouns — use the other character's name if known
        elif 'character a' in pl and 'to b' in pl:   result[ph] = char_b_name or 'เธอ'
        elif 'character b' in pl and 'to a' in pl:   result[ph] = char_a_name or 'นาย'

    return result


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f'Auto-Translate  (LM Studio)  v{APP_VERSION}')
        self.minsize(700, 820)
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

    # ── UI construction ───────────────────────────────────────────────────────

    def _build(self, cfg):
        self._master_template = cfg.get('master_template', DEFAULT_MASTER_TEMPLATE)

        outer = ttk.Frame(self, padding=14)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(5, weight=1)  # log row expands

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

        ttk.Label(af, text='Workers:').grid(row=3, column=0, sticky='w', pady=(4, 0))
        self._workers_var = tk.StringVar(value=str(cfg.get('workers', DEFAULT_WORKERS)))
        ttk.Entry(af, textvariable=self._workers_var, width=8).grid(row=3, column=1, sticky='w', padx=6, pady=(4, 0))
        ttk.Label(af, text='(parallel requests — raise if GPU < 80%)', foreground=GRAY).grid(
            row=3, column=2, columnspan=2, sticky='w', padx=6, pady=(4, 0))

        # ── World Setting ─────────────────────────────────────────────────────
        wf = ttk.LabelFrame(outer,
            text='World Setting  (game background lore used as AI translation context)', padding=8)
        wf.grid(row=2, column=0, sticky='ew', pady=(0, 8))
        wf.columnconfigure(0, weight=1)

        ttk.Label(wf,
            text='Edit the story background. The AI reads this before translating every line.',
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

        # ── Mode ──────────────────────────────────────────────────────────────
        mf = ttk.LabelFrame(outer, text='Translation Mode', padding=8)
        mf.grid(row=3, column=0, sticky='ew', pady=(0, 8))

        self._mode_var = tk.StringVar(value=cfg.get('mode', 'continue'))
        ttk.Radiobutton(mf,
            text='Continue from previous work  (sync new rows + resume untranslated)',
            variable=self._mode_var, value='continue').pack(anchor='w')
        ttk.Radiobutton(mf,
            text='Start fresh  (overwrite output CSV from scratch)',
            variable=self._mode_var, value='fresh').pack(anchor='w', pady=(4, 0))

        # ── Run ───────────────────────────────────────────────────────────────
        rf = ttk.LabelFrame(outer, text='Run', padding=8)
        rf.grid(row=4, column=0, sticky='ew', pady=(0, 8))
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
        lf.grid(row=5, column=0, sticky='nsew')
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self._log = scrolledtext.ScrolledText(lf, height=10, wrap='word')
        self._log.grid(row=0, column=0, sticky='nsew')
        self._log.bind('<Key>', lambda e: 'break' if not (e.state & 4) and e.keysym not in (
            'Up', 'Down', 'Left', 'Right', 'Prior', 'Next', 'Home', 'End') else None)

        log_menu = tk.Menu(self._log, tearoff=0)
        log_menu.add_command(label='Copy',
            command=lambda: self._log.event_generate('<<Copy>>'))
        log_menu.add_command(label='Select All',
            command=lambda: self._log.tag_add('sel', '1.0', 'end'))
        self._log.bind('<Button-3>',
            lambda e: log_menu.tk_popup(e.x_root, e.y_root))

        # ── Version / updates bar ─────────────────────────────────────────────
        bar = ttk.Frame(outer)
        bar.grid(row=6, column=0, sticky='ew', pady=(8, 0))
        self._upd_lbl = ttk.Label(bar, text=f'v{APP_VERSION}', foreground=GRAY)
        self._upd_lbl.pack(side='left')
        ttk.Button(bar, text='Check for Updates…',
                   command=self._open_updater).pack(side='right')

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
                        text=f'v{APP_VERSION}  —  Update available: {tag}',
                        foreground=ORANGE))
            except Exception:
                pass  # offline / rate-limited — run normally
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
        # Exclude ALL-CAPS structural markers like [GLOBAL SETTING] — they are
        # section headers in the template, not user-fillable fields.
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

        # ── Toolbar ───────────────────────────────────────────────────────────
        toolbar = ttk.Frame(win, padding=(14, 10, 14, 0))
        toolbar.pack(fill='x')
        ttk.Button(toolbar, text='Paste Markdown…',
                   command=lambda: _open_paste_window()).pack(side='left')

        # ── Fields ────────────────────────────────────────────────────────────
        form = ttk.Frame(win, padding=14)
        form.pack(fill='both', expand=True)

        # Placeholders whose names match these strings get a multi-line text area.
        _MULTILINE = {'overview'}

        entries     = {}   # ph -> StringVar   (single-line Entry widgets)
        txt_entries = {}   # ph -> Text widget (multi-line ScrolledText widgets)

        for i, ph in enumerate(placeholders):
            ttk.Label(form, text=ph + ':').grid(
                row=i, column=0, sticky='nw', pady=4, padx=(0, 10))
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
            if ph in entries:
                entries[ph].set(val)
            elif ph in txt_entries:
                txt_entries[ph].delete('1.0', 'end')
                txt_entries[ph].insert('1.0', val)

        # Auto-fill from whatever is currently in the World Setting text area.
        _world = self._world_txt.get('1.0', 'end').strip()
        if _world:
            for ph, val in _parse_with_template(self._master_template, _world).items():
                _set_entry(ph, val)

        # ── Paste Markdown sub-window ─────────────────────────────────────────
        def _open_paste_window():
            sub = tk.Toplevel(win)
            sub.title('Paste Markdown')
            sub.geometry('640x420')
            sub.grab_set()

            ttk.Label(sub,
                text='Paste the full Markdown block below, then click "Parse & Fill":',
                foreground=GRAY).pack(anchor='w', padx=10, pady=(10, 4))

            def _parse_and_fill():
                for ph, val in _parse_with_template(
                        self._master_template, txt.get('1.0', 'end')).items():
                    _set_entry(ph, val)
                sub.destroy()

            sub_btns = ttk.Frame(sub)
            sub_btns.pack(fill='x', padx=10, pady=(0, 10), side='bottom')
            ttk.Button(sub_btns, text='Cancel', command=sub.destroy).pack(side='right')
            ttk.Button(sub_btns, text='Parse & Fill',
                       command=_parse_and_fill).pack(side='right', padx=(0, 6))

            txt = scrolledtext.ScrolledText(sub, wrap='word')
            txt.pack(fill='both', expand=True, padx=10, pady=(0, 8))
            self._attach_context_menu(txt)

        # ── Apply / Cancel ────────────────────────────────────────────────────
        def _apply():
            result = self._master_template
            for ph, var in entries.items():
                result = result.replace(f'[{ph}]', var.get())
            for ph, w in txt_entries.items():
                result = result.replace(f'[{ph}]', w.get('1.0', 'end').strip())
            self._world_txt.delete('1.0', 'end')
            self._world_txt.insert('1.0', result)
            win.destroy()

        btn_row = ttk.Frame(win, padding=(14, 0, 14, 14))
        btn_row.pack(fill='x')
        ttk.Button(btn_row, text='Cancel', command=win.destroy).pack(side='right')
        ttk.Button(btn_row, text='Apply', command=_apply).pack(side='right', padx=(0, 6))

    def _import_world_setting(self):
        p = filedialog.askopenfilename(
            title='Import world_setting.md',
            initialfile='world_setting.md',
            filetypes=[('Markdown files', '*.md'), ('All files', '*.*')])
        if not p:
            return
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

        out        = os.path.splitext(src)[0] + '_th.csv'
        world      = self._world_txt.get('1.0', 'end').strip()
        api_url    = self._url_var.get().strip()
        model      = self._model_var.get().strip()
        mode       = self._mode_var.get()
        try:
            max_tokens = int(self._maxtok_var.get().strip())
        except ValueError:
            max_tokens = DEFAULT_MAX_TOKENS
        try:
            workers = max(1, int(self._workers_var.get().strip()))
        except ValueError:
            workers = DEFAULT_WORKERS

        _save_cfg({'source': src, 'api_url': api_url,
                   'model': model, 'world_setting': world, 'mode': mode,
                   'max_tokens': max_tokens, 'workers': workers,
                   'master_template': self._master_template})

        self._busy = True
        self._stop.clear()
        self._start_btn.configure(state='disabled')
        self._stop_btn.configure(state='normal')
        self._status_lbl.configure(text='Running…', foreground=ORANGE)
        self._write_log('\n── Starting translation ──')

        threading.Thread(
            target=self._worker,
            args=(src, out, world, api_url, model, mode, max_tokens, workers),
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
                    'model': model, 'prompt': 'Hi', 'max_tokens': 1, 'stream': False,
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

    def _worker(self, src, out, world, api_url, model, mode, max_tokens, workers):
        # Load source
        try:
            df_src = pd.read_csv(src, dtype=str).fillna('')
            df_src['english'] = df_src['english'].str.strip()
        except Exception as e:
            self._log_line(f'ERROR reading source CSV: {e}')
            self._ui(lambda: self._finish(f'Error: {e}', False))
            return

        # Build working dataframe
        if mode == 'continue' and os.path.isfile(out):
            self._log_line('Syncing with previous work…')
            try:
                df_prev = pd.read_csv(out, dtype=str).fillna('')
                df_prev['english'] = df_prev['english'].str.strip()
                existing = set(df_prev['english'].tolist())
                new_rows = df_src[~df_src['english'].isin(existing)].copy()
                if len(new_rows):
                    if 'thai' not in new_rows.columns:
                        new_rows['thai'] = ''
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

        pending = df[(df['english'] != '') & (df['thai'].str.strip() == '')].index.tolist()
        total = len(pending)
        self._log_line(f'Total rows: {len(df)}  |  Pending: {total}  |  Workers: {workers}')
        self._set_progress(0, total, f'0 / {total}')

        if total == 0:
            self._log_line('All rows already translated.')
            _safe_save(df, out)
            self._ui(lambda: self._finish('Nothing to translate — all done!', True))
            return

        cache      = {}
        in_flight  = {}   # text -> Event; prevents duplicate API calls for same segment
        cache_lock = threading.Lock()
        df_lock    = threading.Lock()
        counters   = [0]  # [done]
        save_every = max(1, workers * 2)
        t_start    = time.time()

        def translate_row(index):
            if self._stop.is_set():
                return

            english_text = df.at[index, 'english'].strip()
            if not english_text or english_text == 'nan':
                return

            _tp = r'\{[^}]+\}'

            # Peel off leading / trailing tag clusters
            _lm   = re.match(r'^(?:' + _tp + r')+', english_text)
            lead  = _lm.group(0) if _lm else ''
            _mid  = english_text[len(lead):]
            _tm   = re.search(r'(?:' + _tp + r')+$', _mid)
            trail = _tm.group(0) if _tm else ''
            body_raw = _mid[:-len(trail)] if trail else _mid

            # Split body on tags — translate each text piece, keep tags verbatim
            parts = re.split(f'({_tp})', body_raw)

            def _translate_segment(seg):
                text = seg.strip()
                if not text:
                    return seg
                has_par = text.startswith('(') and text.endswith(')')
                inner = text[1:-1].strip() if has_par else text

                # Atomically check cache and register as fetcher (or waiter)
                with cache_lock:
                    cached = cache.get(inner)
                    if cached is not None:
                        return f'({cached})' if cached and has_par else cached
                    if inner in in_flight:
                        ev = in_flight[inner]
                        is_fetcher = False
                    else:
                        ev = threading.Event()
                        in_flight[inner] = ev
                        is_fetcher = True

                # Non-fetcher: wait for the fetcher's result
                if not is_fetcher:
                    ev.wait()
                    with cache_lock:
                        result = cache.get(inner, '')
                    return f'({result})' if result and has_par else result or text

                # Fetcher: call the API, then wake up any waiters
                if not hasattr(_tls, 'session'):
                    _tls.session = requests.Session()
                prompt = _build_prompt(world, inner)
                payload = {
                    'model': model, 'prompt': prompt,
                    'temperature': 0.1, 'max_tokens': max_tokens,
                    'stop': ['\n', 'English:', 'Thai:'], 'stream': False,
                }
                try:
                    for _ in range(2):
                        if self._stop.is_set():
                            return text
                        try:
                            resp = _tls.session.post(api_url, json=payload, timeout=45)
                            if resp.status_code == 200:
                                raw    = resp.json()['choices'][0]['text'].strip()
                                result = clean_thai_output(raw.split('\n')[0].strip(), inner)
                                with cache_lock:
                                    cache[inner] = result
                                return f'({result})' if result and has_par else result
                            else:
                                self._log_line(f'  Row {index+1}: server error {resp.status_code}')
                        except Exception as e:
                            self._log_line(f'  Row {index+1}: {e}, retrying…')
                            time.sleep(1)
                finally:
                    with cache_lock:
                        in_flight.pop(inner, None)
                    ev.set()
                return text  # untranslated fallback

            out_parts = []
            for part in parts:
                if self._stop.is_set():
                    return
                if re.fullmatch(_tp, part):
                    out_parts.append(part)
                else:
                    out_parts.append(_translate_segment(part))

            thai = lead + ''.join(out_parts) + trail

            with df_lock:
                df.at[index, 'thai'] = thai
                counters[0] += 1
                d = counters[0]
                snap = df.copy() if d % save_every == 0 else None

            if snap is not None:
                _safe_save(snap, out)

            elapsed = time.time() - t_start
            rpm_str = f'  {d/elapsed*60:.0f} r/min' if elapsed > 2 else ''
            self._log_line(f'[{d}/{total}] {english_text[:60]} ➔ {thai}{rpm_str}')
            self._set_progress(d, total, f'{d} / {total}{rpm_str}')

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(translate_row, idx) for idx in pending]
            for future in concurrent.futures.as_completed(futures):
                if self._stop.is_set():
                    for f in futures: f.cancel()
                    self._log_line('Stopped by user.')
                    break

        _safe_save(df, out)
        d = counters[0]
        msg = f'Done — {d} rows translated  →  {out}'
        self._log_line(f'\n{msg}')
        self._ui(lambda m=msg: self._finish(m, True))


if __name__ == '__main__':
    App().mainloop()
