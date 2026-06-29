import threading
import time
import socket
import sys
import os
import requests
import keyboard
import speech_recognition as sr

TARGET_URL = "http://127.0.0.1:5678/show"
WAKE_PHRASE = "wake up"

# Prevent spamming the API
last_open_time = 0
OPEN_INTERVAL = 2  # seconds

# -----------------------------------------------------------------------
# 二重起動防止: ソケットロックを使ってシングルインスタンスを保証する
# -----------------------------------------------------------------------
LOCK_PORT = 47823  # listener 専用のロックポート

def acquire_single_instance_lock():
    """
    ソケットを使ってシングルインスタンスを保証する。
    既に別の listener が動いている場合は False を返す。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
        # バインド成功 = 自分が唯一のインスタンス
        # ソケットは GC されないようにグローバルに保持する
        globals()["_lock_socket"] = sock
        return True
    except OSError:
        # 既に別のインスタンスが動いている
        sock.close()
        return False


def open_page():
    global last_open_time
    now = time.time()

    if now - last_open_time < OPEN_INTERVAL:
        return

    last_open_time = now

    print(f"[Listener] Triggering {TARGET_URL} ...")
    try:
        response = requests.post(TARGET_URL, timeout=2)
        print(f"[Listener] Response: {response.status_code}")
    except requests.exceptions.RequestException as e:
        # assistant.exe がまだ起動していない場合は正常（待機中）
        print(f"[Listener] assistant is not ready yet: {e}")


def space_listener():
    """
    SPACE を 1 秒間押し続けるとアシスタントを呼び出す。
    """
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


def listen_loop():
    recognizer = sr.Recognizer()
    mic = sr.Microphone()

    print("[Listener] Calibrating microphone for ambient noise (1 second)...")
    with mic as source:
        recognizer.adjust_for_ambient_noise(source, duration=1)

    print(f'[Listener] Listening for wake phrase: "{WAKE_PHRASE}"')
    print("[Listener] Hold SPACE for 1 second to show Assistant.")
    print("[Listener] Running in background — assistant can be called anytime.\n")

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


if __name__ == "__main__":
    # シングルインスタンスチェック
    if not acquire_single_instance_lock():
        print("[Listener] Another instance of listener is already running. Exiting.")
        sys.exit(0)

    print("[Listener] Starting background listener...")

    threading.Thread(target=space_listener, daemon=True).start()
    listen_loop()