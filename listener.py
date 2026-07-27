"""
listener.py (最終修正版 v2)
-----------
- `--noconsole` ビルド時のコンソールハンドル不在によるライブラリクラッシュを完全に防ぐ防弾仕様
- [FIX] llama-server.exe / model のリソース探索を強化（多階層 + _MEIPASS + 環境変数オーバーライド + 詳細ログ）
- [FIX] tasklist 呼び出しの stdin 未リダイレクトによる WinError 6 を修正
- [FIX] Rust(manager.rs) との二重起動レースを緩和
"""

import sys
import os
import ctypes
USE_CMD_WRAPPER = True
# =======================================================================
# 【超重要】すべてのインポートの前に、標準ストリームを完全にダミー化する
# =======================================================================
class DummyStream:
    def write(self, x): pass
    def flush(self): pass
    def reconfigure(self, *args, **kwargs): pass

if getattr(sys, "frozen", False):
    # .exe 化されている場合、ログファイルに逃がすか、完全に無効化する
    try:
        exe_dir = os.path.dirname(sys.executable)
        # 完全に動作を確認する用：同じフォルダにログを出します
        _log_file = open(os.path.join(exe_dir, "listener_debug.log"), "w", encoding="utf-8", buffering=1)
        sys.stdout = _log_file
        sys.stderr = _log_file
    except Exception:
        # 万が一ログファイルが開けない場合も、ダミーを割り当てて絶対に落ちないようにする
        sys.stdout = DummyStream()
        sys.stderr = DummyStream()

    # 窓なしの時に stdin が None だと落ちるライブラリ対策
    if sys.stdin is None:
        sys.stdin = DummyStream()

# -----------------------------------------------------------------------
# 通常のインポート（環境初期化の後に実行）
# -----------------------------------------------------------------------
import threading
import time
import random
import socket
import subprocess
import json
import queue
import array
import math
from collections import deque

# [FIX] Windows のプロキシ自動検出(WPAD)が原因で requests がハングするのを防ぐ。
# --noconsole ビルドでは例外も出ずに無期限ブロックすることがあるため、
# 環境のプロキシ設定を一切信用しない Session を明示的に作る。
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

import requests
import keyboard
import sounddevice as sd  # ダミー化の後に読み込むことで安全を確保
from vosk import Model, KaldiRecognizer

def _safe_utf8_stdout():
    try:
        if sys.platform == "win32":
            os.system("chcp 65001 > nul")
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

_safe_utf8_stdout()

# [FIX] すべての HTTP 通信をこの Session 経由に統一する。
# trust_env=False により、環境変数・Windowsレジストリ・WPAD PACファイル取得など
# 「環境由来のプロキシ設定」を一切参照しなくなり、WPAD解決によるハングを根本的に防ぐ。
HTTP_SESSION = requests.Session()
HTTP_SESSION.trust_env = False
HTTP_SESSION.proxies = {"http": None, "https": None}

# -----------------------------------------------------------------------
# 設定項目
# -----------------------------------------------------------------------
ASSISTANT_EXE   = "assistant.exe"
ASSISTANT_URL   = "http://127.0.0.1:5678/show"
WAKE_WORDS      = ["wake up", "wakeup", "wake-up"]

# --- Wake word 誤検知対策パラメータ ---
# Vosk はグラモリ(文法)制約下だと、無音やノイズすら「候補の中で一番近いもの」に
# 強制的に割り当てがちなため、以下の複数条件を"すべて"満たした場合のみ本物の
# wake word として採用する（多層フィルタ方式）。
WAKE_CONF_AVG_THRESHOLD      = 0.75   # 認識された各単語の信頼度(conf)の平均下限
WAKE_CONF_MIN_THRESHOLD      = 0.55   # 認識された各単語の信頼度(conf)の最小値下限（1語でも自信が無ければ棄却）
WAKE_MIN_PHRASE_DURATION_SEC = 0.35   # 発話区間の最短長。短すぎる打撃音等のノイズを弾く
WAKE_MIN_RMS                 = 40.0   # int16 PCMの実効値(RMS)下限。無音/暗騒音のみでの誤爆を防ぐ
                                       # ログに出る rms 値を見ながら環境に合わせて調整すること
WAKE_REQUIRE_DOUBLE_CONFIRM  = False  # True にすると、下記の時間窓内に2回連続で検出されない限り発火しない
                                       # （さらに厳格化したい場合の追加オプション。誤検知はほぼゼロになるが
                                       #   毎回2回言う必要があり体感の反応は遅くなる）
WAKE_DOUBLE_CONFIRM_WINDOW_SEC = 4.0

OPEN_INTERVAL   = 2
LAUNCH_TIMEOUT  = 15
POLL_INTERVAL   = 0.5
SAMPLE_RATE     = 16000
BLOCK_SIZE      = 4000
AUDIO_CALLBACK_TIMEOUT_SECS = 4.0
MIC_RETRY_INTERVAL_SECS = 5
LOCK_PORT       = 47823

last_open_time  = 0

# [FIX] --noconsole ビルドで tasklist 等の子プロセスを呼ぶ際に必須。
# stdin/stdout/stderr を「必ず」全て明示的にリダイレクトしないと、
# GUIサブシステムの exe には有効な標準ハンドルが無いため
# CreateProcess が WinError 6 (invalid handle) で失敗する。
def _run_hidden_capture(args):
    """tasklist 等、出力を読みたい子プロセスを安全に実行する共通ヘルパー"""
    return subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=0x08000000,  # CREATE_NO_WINDOW
        timeout=5,
    )

def _resolve_model_path() -> str:
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        meipass = getattr(sys, "_MEIPASS", "")

        # 探索パターン
        p1 = os.path.join(meipass, "vosk")
        if meipass and os.path.exists(p1): return p1
        p2 = os.path.join(exe_dir, "vosk-model")
        if os.path.exists(p2): return p2
        p3 = os.path.join(exe_dir, "vosk")
        if os.path.exists(p3): return p3
        return p2
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "vosk-model")

MODEL_PATH = _resolve_model_path()

def _hidden_subprocess_kwargs() -> dict:
    return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW

LLAMA_SERVER_EXE      = "llama-server.exe"
LLAMA_MODEL_FILENAME  = "LFM2.5-1.2B-Instruct-Q8_0.gguf"
LLAMA_PORT            = 8000
LLAMA_READY_URL       = f"http://127.0.0.1:{LLAMA_PORT}/v1/models"
LLAMA_MARKER_DIR      = "llama"
LLAMA_SEARCH_DEPTH    = 4
LLAMA_READY_POLL_SECS     = 0.5
LLAMA_READY_MAX_ATTEMPTS  = 120

# [FIX] 単一の親フォルダだけでなく、複数階層の祖先も探索ルートに加える。
# ビルド後のフォルダ構造（Tauriのresourcesディレクトリ配下にネストされる等）は
# 開発時のフラットな配置と異なることが多く、1階層上だけでは足りないケースがある。
ANCESTOR_SEARCH_LEVELS = 4

# [FIX] 環境変数で明示的にベースディレクトリを指定できるようにする。
# manager.rs 側からリソース解決済みのパスを渡してもらえば、探索ロジックの
# 差異による food-fight を根本的に回避できる。
LLAMA_BASE_DIR_ENV = "LLAMA_BASE_DIR"

def acquire_single_instance_lock() -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
        globals()["_lock_socket"] = sock
        return True
    except OSError:
        sock.close()
        return False

def _listener_own_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def _resolve_assistant_exe_path() -> str:
    return os.path.join(_listener_own_dir(), ASSISTANT_EXE)

def is_assistant_running() -> bool:
    try:
        # [FIX] stdin を明示的に DEVNULL にリダイレクト（旧コードは未指定でWinError 6の原因になっていた）
        out = _run_hidden_capture(["tasklist", "/FI", f"IMAGENAME eq {ASSISTANT_EXE}", "/NH"])
        return ASSISTANT_EXE.lower() in out.stdout.decode(errors="ignore").lower()
    except Exception as e:
        print(f"[Listener] tasklist check failed: {e}")
        return False

def is_assistant_http_ready() -> bool:
    try:
        r = HTTP_SESSION.post(ASSISTANT_URL, timeout=1)
        return r.status_code < 500
    except Exception:
        return False

def launch_assistant():
    exe_path = _resolve_assistant_exe_path()
    exe_dir = os.path.dirname(exe_path)
    # 環境変数をクリーンにし、GUIアプリとして完全に独立して起動させる
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    print(f"[Listener] Launching {exe_path}...")
    try:
        subprocess.Popen(
            [exe_path],
            cwd=exe_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            startupinfo=startupinfo,
            creationflags=0x08000000
        )
    except Exception as e:
        print(f"[Listener] ERROR launching {exe_path}: {e}")

def _find_dir_containing(root: str, target_name: str, max_depth: int):
    q = deque([(root, 0)])
    while q:
        d, depth = q.popleft()
        if not os.path.isdir(d): continue
        if os.path.exists(os.path.join(d, target_name)): return d
        if depth >= max_depth: continue
        try: entries = os.listdir(d)
        except Exception: continue
        for name in entries:
            p = os.path.join(d, name)
            if os.path.isdir(p): q.append((p, depth + 1))
    return None

def _collect_llama_search_roots():
    """[FIX] own_dir から複数階層上の祖先まで含めた探索ルート一覧を作る。
    さらに frozen(onefile)時は _MEIPASS も候補に加える。"""
    own_dir = _listener_own_dir()
    roots = [own_dir]

    cur = own_dir
    for _ in range(ANCESTOR_SEARCH_LEVELS):
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        roots.append(parent)
        cur = parent

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            roots.append(meipass)

    seen = set()
    unique_roots = []
    for r in roots:
        norm = os.path.normcase(os.path.normpath(r))
        if norm not in seen:
            seen.add(norm)
            unique_roots.append(r)
    return unique_roots

def _resolve_llama_base_dir():
    # [FIX] 環境変数で明示指定されていれば最優先で使う（manager.rs から渡す想定）
    override = os.environ.get(LLAMA_BASE_DIR_ENV)
    if override and os.path.exists(os.path.join(override, LLAMA_MARKER_DIR)):
        print(f"[Listener] Using LLAMA_BASE_DIR override: {override}")
        return override

    roots = _collect_llama_search_roots()
    print(f"[Listener] Searching for '{LLAMA_MARKER_DIR}' marker under {len(roots)} root(s):")
    for r in roots:
        print(f"[Listener]   Root candidate: {r}")

    for root in roots:
        found = _find_dir_containing(root, LLAMA_MARKER_DIR, LLAMA_SEARCH_DEPTH)
        if found:
            print(f"[Listener] Found base dir: {found} (from root {root})")
            return found

    print(f"[Listener] ERROR: could not locate '{LLAMA_MARKER_DIR}' marker in any search root.")
    return None

def is_port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5): return True
    except OSError: return False

def is_llama_server_ready() -> bool:
    if not is_port_in_use(LLAMA_PORT): return False
    try:
        r = HTTP_SESSION.get(LLAMA_READY_URL, timeout=0.5)
        return r.status_code < 500
    except Exception: return False

def launch_llama_server():
    base = _resolve_llama_base_dir()
    if not base:
        print("[Listener] ERROR: llama-server resource folder not found.")
        return False

    exe_path = os.path.join(base, "llama", LLAMA_SERVER_EXE)
    model_path = os.path.join(base, "model", LLAMA_MODEL_FILENAME)
    exe_dir = os.path.dirname(exe_path)

    # 1. 環境変数のクリーンアップ
    # PyInstaller が注入した環境変数を排除した新しい環境辞書を作成
    new_env = os.environ.copy()
    keys_to_remove = ["PYTHONHOME", "PYTHONPATH", "_MEIPASS2"]
    for key in keys_to_remove:
        new_env.pop(key, None)

    # PATH をシステム標準と exe_dir のみに制限
    # これにより PyInstaller の DLL フォルダ等が優先されるのを防ぐ
    system_path = os.environ.get("Path", os.environ.get("PATH", ""))
   # 修正前：f-string 内で \\ を使っていたためエラー
    # new_env["PATH"] = f"{exe_dir};{os.path.dirname(sys.executable)};{os.environ.get('SystemRoot', 'C:\\Windows')}\\System32"

    # 修正後：バックスラッシュを含むパスを f-string 外で処理
    sys_root = os.environ.get('SystemRoot', r'C:\Windows')
    # 現在: new_env["PATH"] = f"{exe_dir};{os.path.dirname(sys.executable)};{sys_root}\\System32"
    # 修正後: 最低限のパスのみにする
    new_env["PATH"] = f"{exe_dir};{sys_root}\\System32;{sys_root}"
    
    # 念のため、現在プロセスの環境変数から「llama-server.exe に不要なもの」をさらに削除
    for key in list(new_env.keys()):
        if key.startswith("PY") or key.startswith("PYTHON"):
            new_env.pop(key, None)

    # ログ出力
    print(f"[Listener] --- Debug Info ---")
    print(f"[Listener] EXE: {exe_path}")
    print(f"[Listener] Model: {model_path}")
    print(f"[Listener] CWD: {exe_dir}")
    print(f"[Listener] PATH (Cleaned): {new_env['PATH']}")
    print(f"[Listener] DLL Envs: { {k:v for k,v in new_env.items() if 'DLL' in k.upper()} }")

    # 2. DLL 検索パスの分離（親プロセスの影響を最小化）
    # Python の DLL 検索順序をリセットするために SetDefaultDllDirectories を呼ぶ
    # LOAD_LIBRARY_SEARCH_DEFAULT_DIRS はシステム標準のパスを優先させる
    if hasattr(ctypes.windll.kernel32, "SetDefaultDllDirectories"):
        LOAD_LIBRARY_SEARCH_DEFAULT_DIRS = 0x00001000
        ctypes.windll.kernel32.SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS)

    log_path = os.path.join(exe_dir, "llama_server_stdout.log")
    try:
        llama_log = open(log_path, "w", encoding="utf-8", errors="replace")
    except Exception:
        llama_log = subprocess.DEVNULL

    try:
        # 起動引数の構成
        args = [exe_path, "-m", model_path, "--port", str(LLAMA_PORT)]
        if USE_CMD_WRAPPER:
            args = ["cmd", "/c"] + args

        proc = subprocess.Popen(
            args,
            cwd=exe_dir,
            env=new_env,
            stdin=subprocess.DEVNULL,
            stdout=llama_log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **_hidden_subprocess_kwargs(),
        )
        print(f"[Listener] llama-server spawned (PID={proc.pid})")
        
        # 起動直後の生存確認
        time.sleep(2.0)
        if proc.poll() is not None:
            print(f"[Listener] ERROR: llama-server exited with code {proc.returncode}")
            return False

        globals()["_llama_proc"] = proc
        return True
    except Exception as e:
        print(f"[Listener] CRITICAL: Failed to launch: {e}")
        # GetLastError を取得
        err = ctypes.GetLastError()
        print(f"[Listener] Windows LastError: {err}")
        return False

def ensure_llama_server_running():
    if is_llama_server_ready(): return

    # [FIX] manager.rs (Tauri側) が同時に起動を試みるため、
    # わずかにジッターを入れて先に manager.rs 側の起動を優先させ、
    # 二重起動レースを緩和する。
    time.sleep(random.uniform(0.5, 1.5))
    if is_llama_server_ready(): return

    if not launch_llama_server(): return
    attempts = 0
    while True:
        # [FIX] ポーリング中にプロセス自体が死んでいたら即座に検知してログに残す
        proc = globals().get("_llama_proc")
        if proc is not None and proc.poll() is not None:
            print(f"[Listener] ERROR: llama-server process died while waiting for readiness "
                  f"(exit code {proc.returncode}). llama_server_stdout.log を確認してください。")
            return
        if is_port_in_use(LLAMA_PORT):
            try:
                r = HTTP_SESSION.get(LLAMA_READY_URL, timeout=2)
                if r.status_code < 500:
                    print("[Listener] llama-server is ready.")
                    break
            except Exception: pass
        attempts += 1
        if attempts > LLAMA_READY_MAX_ATTEMPTS:
            print("[Listener] ERROR: llama-server did not become ready within timeout.")
            break
        time.sleep(LLAMA_READY_POLL_SECS)

def open_page():
    global last_open_time
    now = time.time()
    if now - last_open_time < OPEN_INTERVAL: return
    last_open_time = now
    if not is_assistant_running():
        launch_assistant()
        deadline = time.time() + LAUNCH_TIMEOUT
        while time.time() < deadline:
            if is_assistant_http_ready(): break
            time.sleep(POLL_INTERVAL)
        else: return
    try: HTTP_SESSION.post(ASSISTANT_URL, timeout=2)
    except Exception: pass

def space_listener():
    hold_start = None
    while True:
        if keyboard.is_pressed("space"):
            if hold_start is None: hold_start = time.time()
            elif time.time() - hold_start >= 1.0:
                open_page()
                while keyboard.is_pressed("space"): time.sleep(0.05)
                hold_start = None
        else: hold_start = None
        time.sleep(0.05)

def _normalize_wake_text(t: str) -> str:
    """wake word比較用に表記揺れ（ハイフン・連続空白）を正規化する"""
    return " ".join(t.replace("-", " ").split())

_WAKE_WORDS_NORMALIZED = {_normalize_wake_text(w) for w in WAKE_WORDS}

def _compute_pcm_rms(pcm_bytes: bytes) -> float:
    """int16 PCMバイト列の実効値(RMS)を計算する。
    無音・環境ノイズのみの区間が文法制約によりwake wordへ強制割り当てされるのを
    弾くために使用する。"""
    if not pcm_bytes:
        return 0.0
    usable_len = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if usable_len <= 0:
        return 0.0
    try:
        samples = array.array("h")
        samples.frombytes(pcm_bytes[:usable_len])
    except Exception:
        return 0.0
    if not samples:
        return 0.0
    sum_sq = sum(s * s for s in samples)
    return math.sqrt(sum_sq / len(samples))

def _evaluate_wake_candidate(result_dict: dict, utterance_pcm: bytes):
    """
    Voskの確定結果(Result())を多層フィルタで検証し、
    「本物のwake word発話」と判断してよいかを判定する。

    以下をすべて満たした場合のみ True:
      1. [unk]を除いたテキストが、正規化後にwake wordと完全一致する
         （部分一致 `w in text` は誤検知の温床のため使わない）
      2. Vosk が返す単語ごとの信頼度(conf)が、平均・最小の両方で閾値を超える
      3. 発話区間の長さが、短すぎるノイズ的な誤爆でないこと
      4. 発話区間の音声エネルギー(RMS)が、無音/暗騒音のみでの強制割り当てでないこと

    戻り値: (採用してよいか: bool, ログ用の詳細説明: str)
    """
    text = result_dict.get("text", "").strip().lower()
    if not text or text == "[unk]":
        return False, "テキストなし/[unk]のみ"

    meaningful_tokens = [w for w in text.split() if w != "[unk]"]
    candidate_norm = _normalize_wake_text(" ".join(meaningful_tokens))
    if candidate_norm not in _WAKE_WORDS_NORMALIZED:
        return False, f"wake wordと完全一致せず: '{candidate_norm}'"

    words_info = [w for w in result_dict.get("result", []) if w.get("word", "") != "[unk]"]
    if not words_info:
        return False, "単語信頼度情報が取得できず（SetWords未設定の可能性）"

    confs = [float(w.get("conf", 0.0)) for w in words_info]
    avg_conf = sum(confs) / len(confs)
    min_conf = min(confs)
    if avg_conf < WAKE_CONF_AVG_THRESHOLD or min_conf < WAKE_CONF_MIN_THRESHOLD:
        return False, f"信頼度不足 (avg={avg_conf:.2f} < {WAKE_CONF_AVG_THRESHOLD} または min={min_conf:.2f} < {WAKE_CONF_MIN_THRESHOLD})"

    try:
        duration = float(words_info[-1].get("end", 0.0)) - float(words_info[0].get("start", 0.0))
    except Exception:
        duration = 0.0
    if duration < WAKE_MIN_PHRASE_DURATION_SEC:
        return False, f"発話長不足 (duration={duration:.2f}s < {WAKE_MIN_PHRASE_DURATION_SEC}s)"

    rms = _compute_pcm_rms(utterance_pcm)
    if rms < WAKE_MIN_RMS:
        return False, f"音声エネルギー不足 (rms={rms:.1f} < {WAKE_MIN_RMS})"

    return True, f"OK (avg_conf={avg_conf:.2f}, min_conf={min_conf:.2f}, duration={duration:.2f}s, rms={rms:.1f})"

def listen_loop():
    if not os.path.exists(MODEL_PATH):
        print(f"[Listener] ERROR: Vosk model not found at '{MODEL_PATH}'")
        sys.exit(1)

    # [FIX] exe化した際に「マイクが表示されない」原因を切り分けるため、
    # PortAudio が実際に認識しているデバイス一覧とホストAPI、
    # デフォルト入力デバイスを必ずログに残す。
    try:
        print("[Listener] --- Audio device diagnostics ---")
        print(f"[Listener] sounddevice version: {sd.__version__}")
        try:
            print(f"[Listener] PortAudio hostapis: {sd.query_hostapis()}")
        except Exception as e:
            print(f"[Listener] query_hostapis() failed: {e}")
        try:
            devices = sd.query_devices()
            print(f"[Listener] Found {len(devices)} audio device(s):")
            for i, d in enumerate(devices):
                print(f"[Listener]   [{i}] {d.get('name')} "
                      f"(max_input_channels={d.get('max_input_channels')}, "
                      f"hostapi={d.get('hostapi')})")
        except Exception as e:
            print(f"[Listener] query_devices() failed: {e}")
        try:
            print(f"[Listener] Default input device: {sd.default.device}")
        except Exception as e:
            print(f"[Listener] default.device lookup failed: {e}")
        print("[Listener] --- End audio device diagnostics ---")
    except Exception as e:
        print(f"[Listener] Audio diagnostics block failed entirely: {e}")

    print(f"[Listener] Loading Vosk model from: {MODEL_PATH}")
    model = Model(MODEL_PATH)
    recognizer = KaldiRecognizer(model, SAMPLE_RATE, json.dumps(WAKE_WORDS + ["[unk]"]))
    # [FIX] 誤検知対策: 単語ごとの信頼度(conf)・開始/終了時刻をResult()に含めるため必須。
    # これが無いと _evaluate_wake_candidate() の信頼度・発話長チェックが機能しない。
    recognizer.SetWords(True)
    audio_queue = queue.Queue()
    last_callback_at = {"t": time.monotonic()}
    # 二重確認モード用: 直近の一次検出タイムスタンプを保持する
    pending_confirmations = deque()

    def audio_callback(indata, frames, time_info, status):
        last_callback_at["t"] = time.monotonic()
        audio_queue.put(bytes(indata))

    while True:
        stop_watchdog = threading.Event()
        def watchdog():
            while not stop_watchdog.is_set():
                time.sleep(1.0)
                if stop_watchdog.is_set(): return
                if time.monotonic() - last_callback_at["t"] > AUDIO_CALLBACK_TIMEOUT_SECS:
                    audio_queue.put(None)
                    return
        threading.Thread(target=watchdog, daemon=True).start()

        try:
            with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK_SIZE, dtype="int16", channels=1, callback=audio_callback):
                print("[Listener] Microphone stream opened successfully.")
                with audio_queue.mutex: audio_queue.queue.clear()
                recognizer.Reset()
                stop_watchdog.clear()
                # 現在の発話区間（無音区切りでリセットされるまで）に対応する生PCMバッファ。
                # RMS（音声エネルギー）チェックに使用する。
                utterance_pcm = bytearray()

                while True:
                    data = audio_queue.get()
                    if data is None: break
                    utterance_pcm.extend(data)

                    # [FIX] 誤検知対策の核心: 部分結果(PartialResult)ではwake word判定を行わない。
                    # 部分結果は認識途中の不安定な仮説であり、揺れ動く過程でたまたま
                    # wake wordの文字列を経由することがあるため、確定結果(AcceptWaveform=True)
                    # のみを判定対象にする。
                    if recognizer.AcceptWaveform(data):
                        result_dict = json.loads(recognizer.Result())
                        text = result_dict.get("text", "").strip().lower()
                        current_pcm = bytes(utterance_pcm)
                        utterance_pcm = bytearray()  # 次の発話区間のためにリセット

                        if not text or text == "[unk]":
                            continue

                        print(f"[Listener] Heard (final): {text}")
                        is_valid, detail = _evaluate_wake_candidate(result_dict, current_pcm)

                        if not is_valid:
                            print(f"[Listener] Wake word 候補を棄却: {detail}")
                            continue

                        now = time.time()
                        if WAKE_REQUIRE_DOUBLE_CONFIRM:
                            while pending_confirmations and now - pending_confirmations[0] > WAKE_DOUBLE_CONFIRM_WINDOW_SEC:
                                pending_confirmations.popleft()
                            pending_confirmations.append(now)
                            if len(pending_confirmations) < 2:
                                print(f"[Listener] Wake word 一次検出（二重確認待ち）: {detail}")
                                continue
                            pending_confirmations.clear()

                        print(f"[Listener] >>> Wake word 確定検出: '{text}' | {detail}")
                        open_page()
                        recognizer.Reset()
                    else:
                        # 部分結果はデバッグ用に表示するのみで、判定には使わない
                        partial = json.loads(recognizer.PartialResult()).get("partial", "").strip().lower()
                        if partial and partial != "[unk]":
                            print(f"[Listener] Heard (partial, 判定対象外): {partial}")
                stop_watchdog.set()
        except Exception as e:
            stop_watchdog.set()
            # [FIX] エラーメッセージだけでなく完全なトレースバックを出す
            # （PortAudioError の詳細やエラーコードが省略されないようにする）
            import traceback
            print(f"[Listener] Microphone access error: {e}")
            print("[Listener] " + traceback.format_exc().replace("\n", "\n[Listener] "))
            time.sleep(MIC_RETRY_INTERVAL_SECS)

if __name__ == "__main__":
    if not acquire_single_instance_lock(): sys.exit(0)
    print("[Listener] Starting background listener...")
    ensure_llama_server_running()
    threading.Thread(target=space_listener, daemon=True).start()
    try: listen_loop()
    except KeyboardInterrupt: pass