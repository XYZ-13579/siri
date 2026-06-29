import threading
import time
import socket
import sys
import subprocess
import os
import requests
import keyboard
import speech_recognition as sr

ASSISTANT_EXE   = "assistant.exe"
ASSISTANT_URL   = "http://127.0.0.1:5678/show"
WAKE_PHRASE     = "wake up"

# assistant.exe の多重起動防止
OPEN_INTERVAL   = 2        # 秒: /show を叩く最小間隔
LAUNCH_TIMEOUT  = 15       # 秒: assistant 起動後、/show が応答するまで待つ上限
POLL_INTERVAL   = 0.5      # 秒: 起動待ちポーリング間隔

last_open_time  = 0

# -----------------------------------------------------------------------
# シングルインスタンスロック（listener.exe 自身の二重起動防止）
# -----------------------------------------------------------------------
LOCK_PORT = 47823

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

# -----------------------------------------------------------------------
# assistant.exe のプロセス検出
# -----------------------------------------------------------------------
def is_assistant_running() -> bool:
    """tasklist で assistant.exe が動いているか確認する。"""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {ASSISTANT_EXE}", "/NH"],
            creationflags=0x08000000,   # CREATE_NO_WINDOW
            stderr=subprocess.DEVNULL,
        )
        return ASSISTANT_EXE.lower() in out.decode(errors="ignore").lower()
    except Exception:
        return False

def is_assistant_http_ready() -> bool:
    """HTTP /show エンドポイントが応答するか確認する。"""
    try:
        # HEAD は /show に定義されていないので POST で確認
        r = requests.post(ASSISTANT_URL, timeout=1)
        return r.status_code < 500
    except Exception:
        return False

def launch_assistant():
    """assistant.exe をバックグラウンドで起動する。"""
    print(f"[Listener] Launching {ASSISTANT_EXE}...")
    try:
        subprocess.Popen(
            [ASSISTANT_EXE],
            creationflags=0x08000000,   # CREATE_NO_WINDOW
            close_fds=True,
        )
    except FileNotFoundError:
        print(f"[Listener] ERROR: {ASSISTANT_EXE} not found.")
    except Exception as e:
        print(f"[Listener] ERROR launching {ASSISTANT_EXE}: {e}")

# -----------------------------------------------------------------------
# メインのトリガー処理
# -----------------------------------------------------------------------
def open_page():
    """
    1. assistant.exe が動いていなければ起動する。
    2. HTTP /show を叩いてウィンドウを表示させる。
    """
    global last_open_time
    now = time.time()
    if now - last_open_time < OPEN_INTERVAL:
        return
    last_open_time = now

    # --- assistant が動いていなければ起動 ---
    if not is_assistant_running():
        launch_assistant()

        # HTTP サーバーが Ready になるまで待つ
        print("[Listener] Waiting for assistant HTTP server...")
        deadline = time.time() + LAUNCH_TIMEOUT
        while time.time() < deadline:
            if is_assistant_http_ready():
                print("[Listener] Assistant is ready.")
                break
            time.sleep(POLL_INTERVAL)
        else:
            print("[Listener] Assistant did not respond in time. Giving up.")
            return

    # --- /show を叩く ---
    print(f"[Listener] Triggering {ASSISTANT_URL} ...")
    try:
        r = requests.post(ASSISTANT_URL, timeout=2)
        print(f"[Listener] Response: {r.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"[Listener] Request failed: {e}")

# -----------------------------------------------------------------------
# スペースキー長押し監視（1 秒で発動）
# -----------------------------------------------------------------------
def space_listener():
    hold_start = None

    while True:
        if keyboard.is_pressed("space"):
            if hold_start is None:
                hold_start = time.time()
            elif time.time() - hold_start >= 1.0:
                print("[Listener] SPACE held for 1 second.")
                open_page()

                # キーが離されるまで待機
                while keyboard.is_pressed("space"):
                    time.sleep(0.05)
                hold_start = None
        else:
            hold_start = None

        time.sleep(0.05)

# -----------------------------------------------------------------------
# 音声ウェイクワード検出
# -----------------------------------------------------------------------
def listen_loop():
    recognizer = sr.Recognizer()
    mic = sr.Microphone()

    print("[Listener] Calibrating microphone for ambient noise (1 second)...")
    with mic as source:
        recognizer.adjust_for_ambient_noise(source, duration=1)

    print(f'[Listener] Listening for wake phrase: "{WAKE_PHRASE}"')
    print("[Listener] Hold SPACE for 1 second to show Assistant.")
    print("[Listener] Running in background — assistant will be launched if needed.\n")

    while True:
        try:
            with mic as source:
                audio = recognizer.listen(source, timeout=5, phrase_time_limit=4)

            text = recognizer.recognize_google(audio, language="en-US").lower()
            print(f"[Listener] Heard: {text}")

            if WAKE_PHRASE in text:
                print("[Listener] Wake phrase detected!")
                open_page()

        except sr.WaitTimeoutError:
            pass
        except sr.UnknownValueError:
            pass
        except sr.RequestError as e:
            print(f"[Listener] Google Speech Recognition error: {e}")
        except KeyboardInterrupt:
            print("\n[Listener] Stopped.")
            break

# -----------------------------------------------------------------------
# エントリポイント
# -----------------------------------------------------------------------
if __name__ == "__main__":
    if not acquire_single_instance_lock():
        print("[Listener] Another instance is already running. Exiting.")
        sys.exit(0)

    print("[Listener] Starting background listener...")
    threading.Thread(target=space_listener, daemon=True).start()
    listen_loop()
