"""Auto-update / version rollback via GitHub Releases — standalone module.

Works for a PyInstaller --onefile Windows EXE.  Update flow:
  1. download selected release asset into  <app dir>\temp_update\
  2. write a detached .bat updater into %TEMP% and spawn it
  3. exit the app; the .bat waits for the EXE lock to clear, swaps files,
     restarts the app and deletes itself
  4. on next launch the app calls cleanup_after_update() to remove leftovers
"""
import json, os, queue, re, shutil, subprocess, sys, tempfile, threading, time, zipfile
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import urllib.request, urllib.error

REPO          = 'drydream/translate_tool'
API_URL       = f'https://api.github.com/repos/{REPO}/releases'
USER_AGENT    = 'translate-tool-updater'
TEMP_DIR_NAME = 'temp_update'
BAT_NAME      = '_translate_tool_update.bat'
TIMEOUT       = 15
RETRIES       = 3


class UpdateError(Exception):
    pass


# ─── Paths (PyInstaller-aware) ───────────────────────────────────────────────

def is_frozen():
    return getattr(sys, 'frozen', False)


def app_dir():
    """Real folder the app lives in — NOT the PyInstaller _MEIPASS temp dir."""
    if is_frozen():
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def can_write_app_dir():
    test = os.path.join(app_dir(), '.write_test')
    try:
        with open(test, 'w') as f:
            f.write('x')
        os.remove(test)
        return True
    except OSError:
        return False


def cleanup_after_update():
    """Call once at app startup — removes temp_update/ and the updater .bat."""
    try:
        tmp = os.path.join(app_dir(), TEMP_DIR_NAME)
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        bat = os.path.join(tempfile.gettempdir(), BAT_NAME)
        if os.path.isfile(bat):
            try:
                os.remove(bat)
            except OSError:
                pass  # still running right after a swap — next launch gets it
    except Exception:
        pass


# ─── Version compare ─────────────────────────────────────────────────────────

def _parse_ver(v):
    try:
        from packaging.version import Version
        return Version(str(v).lstrip('vV'))
    except Exception:
        nums = re.findall(r'\d+', str(v))
        return tuple(int(n) for n in nums) or (0,)


def is_newer(tag, current):
    try:
        return _parse_ver(tag) > _parse_ver(current)
    except Exception:
        return False


# ─── GitHub API ──────────────────────────────────────────────────────────────

def fetch_releases():
    """Return [{tag, name, body, prerelease, assets:[{name,url,size}]}] newest first."""
    req = urllib.request.Request(API_URL, headers={
        'Accept': 'application/vnd.github+json', 'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise UpdateError('GitHub API rate limit reached — try again in a few minutes.')
        raise UpdateError(f'GitHub API error: HTTP {e.code}')
    except Exception as e:
        raise UpdateError(f'Network error: {e}')
    rels = []
    for r in data:
        if r.get('draft'):
            continue
        rels.append({
            'tag': r['tag_name'],
            'name': r.get('name') or r['tag_name'],
            'body': r.get('body') or '(no release notes)',
            'prerelease': r.get('prerelease', False),
            'assets': [{'name': a['name'],
                        'url': a['browser_download_url'],
                        'size': a.get('size', 0)} for a in r.get('assets', [])],
        })
    return rels


def latest_release_tag():
    rels = [r for r in fetch_releases() if not r['prerelease']]
    return rels[0]['tag'] if rels else None


def pick_asset(release):
    """Prefer asset matching our exe name, then any .exe, then any .zip."""
    assets = release['assets']
    if is_frozen():
        me = os.path.basename(sys.executable).lower()
        for a in assets:
            if a['name'].lower() == me:
                return a
    for a in assets:
        if a['name'].lower().endswith('.exe'):
            return a
    for a in assets:
        if a['name'].lower().endswith('.zip'):
            return a
    return None


# ─── Download (retry + progress + partial cleanup) ───────────────────────────

def download(url, dest, progress_cb=None, cancel=None):
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(dest, 'wb') as f:
                total = int(r.headers.get('Content-Length') or 0)
                got = 0
                while True:
                    if cancel is not None and cancel.is_set():
                        raise UpdateError('Cancelled.')
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if progress_cb:
                        progress_cb(got, total)
            return
        except UpdateError:
            _silent_remove(dest)
            raise
        except Exception as e:
            _silent_remove(dest)
            last_err = e
            if attempt < RETRIES:
                time.sleep(2)
    raise UpdateError(f'Download failed after {RETRIES} attempts: {last_err}')


def _silent_remove(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def prepare_new_exe(release, progress_cb=None, cancel=None):
    """Download asset into temp_update/; if zip, extract and locate the exe.
    Returns full path of the new exe ready to swap in."""
    asset = pick_asset(release)
    if asset is None:
        raise UpdateError(f"Release {release['tag']} has no .exe or .zip asset.")
    tmp = os.path.join(app_dir(), TEMP_DIR_NAME)
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    dest = os.path.join(tmp, asset['name'])
    try:
        download(asset['url'], dest, progress_cb, cancel)
        if dest.lower().endswith('.zip'):
            with zipfile.ZipFile(dest) as zf:
                zf.extractall(tmp)
            os.remove(dest)
            for root, _dirs, files in os.walk(tmp):
                for fn in files:
                    if fn.lower().endswith('.exe'):
                        return os.path.join(root, fn)
            raise UpdateError('Downloaded zip contains no .exe file.')
        return dest
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


# ─── Detached swap ───────────────────────────────────────────────────────────

def apply_update_and_exit(new_exe):
    """Spawn the detached .bat updater, then hard-exit the app.
    Only valid when frozen (running as a packaged .exe)."""
    if not is_frozen():
        raise UpdateError('Version switching only works in the packaged .exe build.')
    old_exe = sys.executable
    tmp_dir = os.path.join(app_dir(), TEMP_DIR_NAME)
    bat = os.path.join(tempfile.gettempdir(), BAT_NAME)
    # ping -n used as delay: `timeout` breaks in a detached (console-less) process
    script = f"""@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
set /a tries=0
:wait
ping -n 2 127.0.0.1 >nul
del "{old_exe}" >nul 2>&1
if exist "{old_exe}" (
  set /a tries+=1
  if !tries! lss 30 goto wait
  exit /b 1
)
move /y "{new_exe}" "{old_exe}" >nul
start "" "{old_exe}"
rd /s /q "{tmp_dir}" >nul 2>&1
endlocal
(goto) 2>nul & del "%~f0"
"""
    with open(bat, 'w', encoding='utf-8') as f:
        f.write(script)
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(['cmd', '/c', bat],
                     creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                     close_fds=True, cwd=tempfile.gettempdir())
    os._exit(0)


# ─── UI ──────────────────────────────────────────────────────────────────────

class UpdateDialog(tk.Toplevel):
    def __init__(self, parent, current_version):
        super().__init__(parent)
        self.title('Updates & Versions')
        self.geometry('540x460')
        self.resizable(False, False)
        self.transient(parent)
        self._current  = current_version
        self._releases = []
        self._busy     = False
        self._cancel   = threading.Event()
        self._q        = queue.Queue()
        self._build()
        self._poll()
        self._load_releases()
        self.protocol('WM_DELETE_WINDOW', self._on_close)

    # ── layout ──
    def _build(self):
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(1, weight=1)

        ttk.Label(outer, text=f'Current version:  v{self._current}') \
            .grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 8))

        ttk.Label(outer, text='Available versions:').grid(row=1, column=0, sticky='w')
        self._combo = ttk.Combobox(outer, state='disabled', values=[])
        self._combo.grid(row=1, column=1, sticky='ew', padx=(6, 0))
        self._combo.bind('<<ComboboxSelected>>', self._on_select)

        nf = ttk.LabelFrame(outer, text='Release notes', padding=6)
        nf.grid(row=2, column=0, columnspan=2, sticky='nsew', pady=8)
        outer.rowconfigure(2, weight=1)
        self._notes = scrolledtext.ScrolledText(nf, height=10, wrap='word', state='disabled')
        self._notes.pack(fill='both', expand=True)

        self._bar = ttk.Progressbar(outer, maximum=100)
        self._bar.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(0, 6))

        self._status = ttk.Label(outer, text='Loading releases…', foreground='#666666')
        self._status.grid(row=4, column=0, columnspan=2, sticky='w')

        self._apply_btn = ttk.Button(outer, text='Apply Version',
                                     command=self._do_apply, state='disabled')
        self._apply_btn.grid(row=5, column=0, columnspan=2, sticky='e', pady=(8, 0))

    # ── thread → UI plumbing (same pattern as main app) ──
    def _poll(self):
        try:
            while True:
                self._q.get_nowait()()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(50, self._poll)

    def _ui(self, fn):
        self._q.put(fn)

    def _set_status(self, msg, color='#666666'):
        self._ui(lambda: self._status.configure(text=msg, foreground=color))

    # ── load release list ──
    def _load_releases(self):
        def work():
            try:
                rels = fetch_releases()
            except UpdateError as e:
                self._set_status(str(e), '#b00000')
                return
            self._ui(lambda: self._fill(rels))
        threading.Thread(target=work, daemon=True).start()

    def _fill(self, rels):
        self._releases = rels
        if not rels:
            self._set_status('No releases found on GitHub yet.', '#b05000')
            return
        labels = []
        for i, r in enumerate(rels):
            extra = '  (latest)' if i == 0 else ''
            if r['tag'].lstrip('vV') == self._current.lstrip('vV'):
                extra += '  (current)'
            labels.append(r['tag'] + extra)
        self._combo.configure(values=labels, state='readonly')
        self._combo.current(0)
        self._apply_btn.configure(state='normal')
        self._on_select()
        if is_newer(rels[0]['tag'], self._current):
            self._set_status(f"Update available: {rels[0]['tag']}", '#b05000')
        else:
            self._set_status('You are on the latest version.', '#1a7a1a')

    def _selected_release(self):
        i = self._combo.current()
        return self._releases[i] if 0 <= i < len(self._releases) else None

    def _on_select(self, _event=None):
        rel = self._selected_release()
        if rel is None:
            return
        self._notes.configure(state='normal')
        self._notes.delete('1.0', 'end')
        self._notes.insert('end', rel['body'])
        self._notes.configure(state='disabled')

    # ── apply ──
    def _do_apply(self):
        if self._busy:
            return
        rel = self._selected_release()
        if rel is None:
            return
        if not is_frozen():
            messagebox.showinfo('Not packaged',
                'Version switching only works in the packaged .exe build.\n'
                'You are running from Python source — update with git instead.',
                parent=self)
            return
        if not can_write_app_dir():
            messagebox.showerror('No write permission',
                'Cannot write to the application folder:\n'
                f'{app_dir()}\n\n'
                'Move the app to a normal folder (e.g. Desktop or D:\\), or\n'
                'run it as administrator (right-click → Run as administrator).',
                parent=self)
            return
        if not messagebox.askyesno('Apply version',
                f"Download and switch to {rel['tag']}?\n"
                'The app will restart automatically.', parent=self):
            return
        self._busy = True
        self._apply_btn.configure(state='disabled')
        self._combo.configure(state='disabled')

        def progress(got, total):
            if total:
                pct = got * 100 / total
                self._ui(lambda: self._bar.configure(value=pct))
                self._set_status(f'Downloading…  {got // 1024:,} / {total // 1024:,} KB')
            else:
                self._set_status(f'Downloading…  {got // 1024:,} KB')

        def work():
            try:
                new_exe = prepare_new_exe(rel, progress, self._cancel)
            except UpdateError as e:
                self._set_status(str(e), '#b00000')
                self._ui(self._unlock)
                return
            self._set_status('Restarting…', '#1a7a1a')
            self._ui(lambda: apply_update_and_exit(new_exe))  # never returns
        threading.Thread(target=work, daemon=True).start()

    def _unlock(self):
        self._busy = False
        self._bar.configure(value=0)
        self._apply_btn.configure(state='normal')
        self._combo.configure(state='readonly')

    def _on_close(self):
        self._cancel.set()
        self.destroy()
