import json
import logging
import os
import threading
import time
import signal
import sys
import psutil
from datetime import datetime
from websocket import WebSocketApp
import requests
from pynput import keyboard
from PIL import Image
import pystray
import winreg
from urllib.parse import urlparse, urlunparse
from translation_logger import TranslationLogger
from tray_controller import TrayController

# グローバル変数の定義
is_running = True
current_translation_index = 0
is_muted = True
translation_profiles_lock = threading.Lock()
APP_NAME = "YncneoXSOBridge"  # デフォルトのアプリ名
reconnect_attempts = 0  # データ用WebSocketの再接続試行回数
reconnect_lock = threading.Lock()  # 再接続試行回数を保護するロック
xso_ws = None  # XSOverlayのWebSocketオブジェクトを格納するグローバル変数
data_ws = None  # Yukacone翻訳ログ用WebSocket
translation_logger = None
last_mute_status_ok = True

# 認識言語のデフォルト値を定義する新しいグローバル変数
DEFAULT_RECOGNITION_LANGUAGE = "ja"
last_recognition_language = DEFAULT_RECOGNITION_LANGUAGE  # 前回の認識言語を保持

# タスクトレイ用
tray_status = "Initializing..."
tray_controller = None
XSO_PORT = None
YUKACONE_HTTP_PORT = None
YUKACONE_WS_PORT = None
DEBUG_MODE = False

_cleanup_done = False
_cleanup_lock = threading.Lock()
xso_io_lock = threading.Lock()

xso_io_lock = threading.Lock()          # 既存（送信/close の競合防止）
xso_reconnect_lock = threading.Lock()   # ★追加（タイマー/ホットキーの競合防止）

# --- シグナルハンドラーとクリーンアップ ---
def signal_handler(sig, frame):
    """終了シグナルを検知し、プログラムを安全に終了させるためのハンドラー"""
    global is_running
    logging.info("終了シグナルを検知しました。プログラムを安全に終了します...")
    is_running = False
    cleanup()

def cleanup():
    """プログラム終了時に必要なクリーンアップ処理を行う"""
    global is_running, xso_ws, data_ws, _cleanup_done
    with _cleanup_lock:
        if _cleanup_done:
            return
        _cleanup_done = True

    logging.info("クリーンアップ処理を開始します...")
    is_running = False

    # --- WebSocket を明示的にクローズ ---
    # XSOverlay
    with xso_io_lock:
        if xso_ws is not None:
            try:
                logging.info("XSOverlay WebSocket をクローズします")
                xso_ws.close()
            except Exception as e:
                logging.error(f"XSOverlay WebSocket クローズ中にエラー: {e}")
        xso_ws = None
    
    # Yukacone 翻訳ログ WebSocket
    if data_ws is not None:
        try:
            logging.info("Yukacone WebSocket をクローズします")
            data_ws.close()
        except Exception as e:
            logging.error(f"Yukacone WebSocket クローズ中にエラー: {e}")
        data_ws = None

    # トレイアイコン停止
    if tray_controller is not None:
        try:
            tray_controller.stop()
        except Exception as e:
            logging.error(f"TrayController 停止中にエラー: {e}")

    # 翻訳ログの flush とスレッド停止
    if translation_logger is not None:
        try:
            translation_logger.stop()
        except Exception as e:
            logging.error(f"TranslationLogger 停止中にエラー: {e}")

    logging.info("プログラムを終了します...")
    sys.exit(0)

# --- 設定ファイル読み込み ---
def load_config():
    """config.jsonを読み込む"""
    try:
        # 実行中のプログラムのディレクトリパスを取得する
        # PyInstallerなどで実行ファイル化されている場合にも対応
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            
        config_path = os.path.join(base_dir, "config.json")
        
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("[ERROR] config.jsonが見つかりません。")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"[ERROR] config.jsonの形式が不正です: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] config.jsonの読み込みに失敗: {e}")
        sys.exit(1)
def extract_port_from_url(url: str):
    """URL文字列からポート番号(int)を取り出す。取れなければ None。"""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if parsed.port:
            return parsed.port
    except Exception as e:
        logging.error(f"URLからポート抽出に失敗: url={url}, err={e}")
    return None

# --- Yukarinette WebSocket,HTTP接続先をレジストリから読み込む ---
def get_registry_hive_from_name(name: str):
    n = name.upper()
    mapping = {
        "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
        "HKCU": winreg.HKEY_CURRENT_USER,
        "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
    }
    if n not in mapping:
        raise ValueError(f"未知のレジストリハイブ名: {name}")
    return mapping[n]

def read_yncneo_port(config: dict, value_key_name: str, desc: str) -> int:
    """
    config.json の設定を使って YukarinetteConnectorNeo のポートを取得する。

    - Hive     : config["Yncneo_Registry_Hive"]
    - Path     : config["Yncneo_Registry_Path"]  （例: "Software\\YukarinetteConnectorNeo"）
    - Value名  : config[value_key_name] （例: "HTTP", "WebSocket"）

    実際のレジストリ構造：
      [HKEY_CURRENT_USER\Software\YukarinetteConnectorNeo]
      "HTTP"=dword:...
      "WebSocket"=dword:...
    """
    hive_name = config.get("Yncneo_Registry_Hive")
    base_path = config.get("Yncneo_Registry_Path")
    value_name = config.get(value_key_name)

    if not hive_name or not base_path or not value_name:
        raise ValueError(
            f"{desc} のレジストリ設定が config.json に不足しています "
            f"(Hive={hive_name}, Path={base_path}, Value={value_name})"
        )

    hive = get_registry_hive_from_name(hive_name)

    try:
        with winreg.OpenKey(hive, base_path) as key:
            # ★ 値名 value_name ("HTTP" / "WebSocket") を読む
            value, reg_type = winreg.QueryValueEx(key, value_name)

            if not isinstance(value, int):
                # 一応、文字列になっていても int に変換を試みる
                try:
                    port = int(str(value))
                except Exception:
                    raise ValueError(f"{desc} のレジストリ値が整数ではありません: {value}")
            else:
                port = value

            logging.info(f"{desc} ポート値取得: {port} (Key={base_path}, Value={value_name})")
            return port

    except FileNotFoundError as e:
        # キー自体が無い場合
        raise RuntimeError(
            f"{desc} のレジストリキーが見つかりません: "
            f"{hive_name}\\{base_path} / {e}"
        )
    except OSError as e:
        # 値名が無い場合など
        raise RuntimeError(
            f"{desc} のレジストリ値 '{value_name}' の読み出しに失敗: "
            f"{hive_name}\\{base_path} / {e}"
        )

# --- 共通: PyInstaller 対応のリソースパス ---
def resource_path(relative_path: str) -> str:
    """PyInstaller の onefile 実行時でもリソースにアクセスできるパスを返す"""
    if hasattr(sys, "_MEIPASS"):
        base_path = sys._MEIPASS  # type: ignore[attr-defined]
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

def update_tray_status():
    """タスクトレイのタイトル（ホバー時のステータス表示）を更新する"""
    global tray_status, tray_controller
    global XSO_PORT, YUKACONE_HTTP_PORT, YUKACONE_WS_PORT, DEBUG_MODE
    
    status = "Unknown"
    if last_mute_status_ok:
        status = "Mute" if is_muted else "Online"
    
    debug_text = "ON" if DEBUG_MODE else "OFF"

    parts = [f"{APP_NAME} - {status}"]

    # ポート番号表示
    if XSO_PORT is not None:
        parts.append(f"XSO:{XSO_PORT}")
    if YUKACONE_HTTP_PORT is not None:
        parts.append(f"HTTP:{YUKACONE_HTTP_PORT}")
    if YUKACONE_WS_PORT is not None:
        parts.append(f"WS:{YUKACONE_WS_PORT}")

    # DEBUGモード表示
    parts.append(f"DEBUG:{debug_text}")

    tray_status = " | ".join(parts)

    # 実際のアイコンタイトル更新は TrayController に任せる
    if tray_controller is not None:
        tray_controller.update_tooltip(tray_status)


# --- ログの初期化 ---
def setup_logger(script_name, debug):
    """メインロガーを初期化する"""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    executable_path = os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__))
    log_dir = os.path.join(executable_path, 'log')

    if not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir)
            print(f"ディレクトリを作成しました: {log_dir}")
        except OSError as e:
            print(f"ディレクトリ作成中にエラーが発生しました: {e}")
            sys.exit(1)
    log_file = os.path.join(log_dir, f"{script_name}_{timestamp}.log")
    
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    
    return log_file

# --- 翻訳された文字列を取得するヘルパー関数 ---
def get_translated_text(data, source_lang):
    """認識言語以外の翻訳文字列を取得する"""
    text_list = data.get("textList", {})
    
    # 認識言語のキー以外のキーを探す
    for lang_code, text in text_list.items():
        if lang_code != source_lang:
            return text
            
    return None

# --- ゆかコネAPI mute-status ---
def get_mute_status(base_url: str) -> bool:
    """
    /mute-status を呼んで true/false を返す。
    失敗したら例外（起動時はそのまま終了させたい想定）。
    """
    ok, text = call_yukacone_api(base_url, "/mute-status", {})
    if not ok or text is None:
        raise RuntimeError("mute-status の取得に失敗しました")

    t = text.strip().lower()
    if t == "true":
        return True
    if t == "false":
        return False
    raise ValueError(f"mute-status 応答が想定外です: {text}")

# --- ゆかコネNEO Mute Status checker ---
def refresh_mute_status(config):
    """
    /mute-status を呼んで、取れたら is_muted を更新。
    取れなければ is_muted は変更せず、Unknown状態にする。
    """
    global is_muted, last_mute_status_ok

    try:
        actual = get_mute_status(config["yukacone_endpoint"])
        last_mute_status_ok = True
        is_muted = actual
        return True
    except Exception as e:
        last_mute_status_ok = False
        logging.warning(f"mute-status取得失敗（状態は維持）: {e}")
        return False

# --- ゆかコネAPI mute-status 同期処理、不要かもしれない... ---
def periodic_mute_sync(config: dict, ws):
    global is_muted
    interval_sec = 300  # 5分

    while is_running:
        try:
            time.sleep(interval_sec)
            if not is_running:
                break

            changed_before = is_muted
            ok = refresh_mute_status(config)

            if ok and is_muted != changed_before:
                logging.info(f"mute-status同期: {changed_before} -> {is_muted}")
                send_xso_status(ws, config, current_translation_index, is_muted)
            elif ok:
                logging.debug("mute-status同期: 変化なし")

            # ok / ng に関わらずトレイは更新（Unknown反映もここで）
            update_tray_status()

        except Exception as e:
            logging.warning(f"mute-status同期に失敗: {e}")

# --- ゆかコネのAPI呼び出し ---
def call_yukacone_api(base_url, path, params):
    """ゆかコネAPIを呼び出す。戻り値: (成功bool, response_text or None)"""
    try:
        url = f"{base_url}{path}"
        logging.info(f"{path} 実行: {params}")
        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()
        text = (response.text or "").strip()
        # "Stay" も成功としてログに出す
        logging.info(f"{path} 成功: {text}")
        return True, text
    except Exception as e:
        logging.error(f"{path} 失敗: {e}")
        return False, None

# --- 翻訳設定変更 ---
def update_translation(config, index):
    """翻訳プロファイルを更新する"""
    global current_translation_index, last_recognition_language
    with translation_profiles_lock:
        try:
            setting = config["translation_profiles"][index]
            base_url = config["yukacone_endpoint"]
            
            new_recognition_language = setting["recognition_language"]
            logging.debug(f"認識言語 現:新={last_recognition_language}:{new_recognition_language}")
            
            # 認識言語が前回と異なる場合のみAPIを呼び出す
            if new_recognition_language != last_recognition_language:
                logging.info(f"認識言語変更: language={new_recognition_language}")
                call_yukacone_api(base_url, "/setRecognitionParam", {"language": new_recognition_language})
                time.sleep(0.5)
                last_recognition_language = new_recognition_language  # 変更を記録
            else:
                logging.info(f"認識言語は変更ありません: language={new_recognition_language}")

            logging.info(f"翻訳設定変更: language={setting['translation_param']['language']}, engine={setting['translation_param']['engine']}")
            call_yukacone_api(base_url, "/setTranslationParam", {
                "slot": setting["translation_param"]["slot"],
                "language": setting["translation_param"]["language"],
                "engine": setting["translation_param"]["engine"]
            })
            current_translation_index = index
        except IndexError:
            logging.error(f"翻訳プロファイルのインデックスが無効です: {index}")
        except KeyError as e:
            logging.error(f"config.jsonの設定キーが不足しています: {e}")

# --- ホットキー ---
def to_pynput_hotkey(hotkey: str) -> str:
    # "alt+ctrl+v" -> "<alt>+<ctrl>+v"
    parts = [p.strip().lower() for p in hotkey.split("+") if p.strip()]
    mapped = []
    for p in parts:
        if p in ("ctrl", "control"):
            mapped.append("<ctrl>")
        elif p == "alt":
            mapped.append("<alt>")
        elif p == "shift":
            mapped.append("<shift>")
        elif p in ("win", "cmd", "super"):
            mapped.append("<cmd>")
        else:
            # 最後の通常キー想定: "v" など
            mapped.append(p)
    return "+".join(mapped)

def start_reconnect_hotkey(config: dict):
    hotkey_cfg = (config.get("XSO_RECONNECT_HOTKEY") or "").strip()
    if not hotkey_cfg:
        logging.info("XSO_RECONNECT_HOTKEY 未設定のためホットキーは無効")
        return None

    pynput_hotkey = to_pynput_hotkey(hotkey_cfg)
    logging.info(f"XSO再接続ホットキー: {hotkey_cfg} -> {pynput_hotkey}")

    def on_hotkey():
        # 他操作（Mute/翻訳切替/タイマー）と競合しないように同じ関数へ
        reconnect_xso(config, reason=f"hotkey:{hotkey_cfg}")

    gh = keyboard.GlobalHotKeys({pynput_hotkey: on_hotkey})
    t = threading.Thread(target=gh.run, daemon=True)
    t.start()
    return gh  # 停止したいなら保持

# --- XSOverlay Websocket再接続 ---
def reconnect_xso(config: dict, reason: str):
    global xso_ws

    # 既に再接続中なら、ホットキー連打やタイマー競合を抑止
    got = xso_reconnect_lock.acquire(blocking=False)
    if not got:
        logging.info(f"XSO再接続スキップ（既に再接続中）: {reason}")
        return False

    try:
        # 送信/更新と競合しないように I/O ロック
        with xso_io_lock:
            logging.info(f"XSO再接続開始: {reason}")

            # 切断
            if xso_ws is not None:
                try:
                    xso_ws.close()
                    logging.info("XSO切断しました")
                except Exception as e:
                    logging.warning(f"XSO切断に失敗: {e}")
                finally:
                    xso_ws = None

            # 再接続（あなたの既存関数を使う）
            ws = connect_to_xsoverlay(config)  # ※あなたの接続関数名に合わせて
            xso_ws = ws

            if xso_ws is None:
                logging.warning("XSO再接続失敗（ws=None）")
                return False

            logging.info("XSO再接続成功")
            return True

    finally:
        xso_reconnect_lock.release()

# --- XSOverlay表示更新 ---
def send_xso_status(ws, config, index, is_muted):
    """XSOverlayのメディア情報表示を更新する"""
    if ws is None:
        logging.warning("XSO未接続のため send_xso_status をスキップします")
        return
    with xso_io_lock:
        try:
            profile = config["translation_profiles"][index]
            data = {
                "sender": APP_NAME,
                "target": "xsoverlay",
                "command": "UpdateMediaPlayerInformation",
                "jsonData": json.dumps({
                    "artist": f'{profile["name"]} ({profile["translation_param"]["engine"]})',
                    "title": f"{'Mute' if is_muted else 'Online'}",
                    "album": APP_NAME,
                    "sourceApp": "ゆかコネ"
                })
            }
            ws.send(json.dumps(data))
        except Exception as e:
            logging.error(f"XSOverlayへの表示送信失敗: {e}")
        
def send_xso_notification(ws, config, content):
    """XSOverlayに通知を送信する"""
    if ws is None:
        logging.warning(f"XSO未接続のため send_xso_notification をスキップします")
        return
    with xso_io_lock:
        try:
            notification_payload = {
                "sender": APP_NAME,
                "target": "xsoverlay",
                "command": "SendNotification",
                "jsonData": json.dumps({
                    "type": 1,
                    "title": "ゆかコネ翻訳",
                    "opacity": 0.5,
                    "volume": 0,
                    "content": content
                })
            }
            ws.send(json.dumps(notification_payload))
        except Exception as e:
            logging.error(f"XSOverlayへの通知送信失敗: {e}")

# --- メディアキー検出スレッド ---
def media_key_listener(ws, config):
    """メディアキーの入力を監視するスレッド"""
    global current_translation_index, is_muted
    
    def on_press(key):
        global current_translation_index, is_muted
        try:
            if key == keyboard.Key.media_play_pause:
                target_muted = not is_muted
                if target_muted:
                    ok, text = call_yukacone_api(config["yukacone_endpoint"], "/mute-on", {})
                    logging.info(f"/mute-on result: ok={ok}, body={text}")
                else:
                    ok, text = call_yukacone_api(config["yukacone_endpoint"], "/mute-off", {})
                    logging.info(f"/mute-off result: ok={ok}, body={text}")
                time.sleep(0.3)
                actual = get_mute_status(config["yukacone_endpoint"])
                if actual != is_muted:
                   logging.info(f"mute-status confirms: {is_muted} -> {actual}")
                is_muted = actual

                refresh_mute_status(config)
                send_xso_status(ws, config, current_translation_index, is_muted)
                update_tray_status()
            elif key == keyboard.Key.media_next:
                with translation_profiles_lock:
                    current_translation_index = (current_translation_index + 1) % len(config["translation_profiles"])
                update_translation(config, current_translation_index)
                time.sleep(0.5)
                ok, text = call_yukacone_api(config["yukacone_endpoint"], "/mute-off", {})
                logging.info(f"/mute-off result: ok={ok}, body={text}")
                time.sleep(0.3)
                actual = get_mute_status(config["yukacone_endpoint"])
                if actual != is_muted:
                    logging.info(f"mute-status confirms: {is_muted} -> {actual}")
                    is_muted = actual

                refresh_mute_status(config)
                send_xso_status(ws, config, current_translation_index, is_muted)
                update_tray_status()
            elif key == keyboard.Key.media_previous:
                with translation_profiles_lock:
                    current_translation_index = (current_translation_index - 1) % len(config["translation_profiles"])
                update_translation(config, current_translation_index)
                time.sleep(0.5)
                ok, text = call_yukacone_api(config["yukacone_endpoint"], "/mute-off", {})
                logging.info(f"/mute-off result: ok={ok}, body={text}")
                time.sleep(0.3)
                actual = get_mute_status(config["yukacone_endpoint"])
                if actual != is_muted:
                    logging.info(f"mute-status confirms: {is_muted} -> {actual}")
                    is_muted = actual
                send_xso_status(ws, config, current_translation_index, is_muted)
                update_tray_status()
        except Exception as e:
            logging.error(f"キーイベント処理中エラー: {e}")

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()

# --- XSOverlay WebSocket接続 ---
def connect_to_xsoverlay(config):
    """XSOverlayに接続する"""
    ws = None
    while is_running:
        try:
            base = (config["xso_endpoint"] or "").rstrip("/")
            websocket_url = f"{base}/?client={APP_NAME}"
            ws = WebSocketApp(
                websocket_url,
                on_open=lambda ws: logging.info("XSOverlayに接続しました"),
                on_error=lambda ws, err: logging.error(f"XSOverlayエラー: {err}"),
                on_close=lambda ws, code, msg: logging.warning("XSOverlay切断"),
            )
            thread = threading.Thread(target=ws.run_forever)
            thread.daemon = True
            thread.start()
            return ws
        except Exception as e:
            logging.error(f"XSOverlayに接続できませんでした、5秒後に再接続します: {e}")
            time.sleep(3)
    return None

# --- XSOverlayに対して定期的にWebsocketを切断、接続を行う処理 ---
def periodic_xso_reconnect(config: dict):
    interval = int(config.get("XSO_RECONNECT_INTERVAL_SEC", 300))
    if interval <= 0:
        logging.info("XSO定期再接続は無効 (interval<=0)")
        return

    while is_running:
        time.sleep(interval)
        if not is_running:
            break
        reconnect_xso(config, reason=f"timer:{interval}s")

# --- データ用 WebSocket接続 ---
def connect_to_data_ws(config, xso_ws):
    global is_running, data_ws, translation_logger

    ws_url = config.get("yukacone_translationlog_ws")

    def on_open(ws):
        logging.info("Yukacone WebSocket connected")

    def on_message(ws, message):
        try:
            data = json.loads(message)
        except Exception as e:
            logging.error(f"JSON parse error: {e}")
            return

        if translation_logger:
            translation_logger.add_yukacone_message(data)

    def on_close(ws, code, msg):
        logging.warning("Yukacone WebSocket closed")

    def on_error(ws, err):
        logging.error(f"Yukacone WebSocket error: {err}")

    while is_running:
        ws = WebSocketApp(
            ws_url,
            on_open=on_open,
            on_message=on_message,
            on_close=on_close,
            on_error=on_error,
        )
        data_ws = ws
        ws.run_forever()

        if not is_running:
            break

        time.sleep(3)

# --- 初期化処理 ---
def initialize(config, ws):
    """プログラムの初期化処理を行う"""
    global is_muted
    global last_recognition_language
    logging.info("初期化処理を開始します。")

    # 認識言語の初期設定をupdate_translationに任せる
    # 翻訳プロファイルの設定
    update_translation(config, current_translation_index)
    
    time.sleep(3.0)
    # mute-status取得、XSOverlayに反映
    is_muted = get_mute_status(config["yukacone_endpoint"])
    send_xso_status(ws, config, current_translation_index, is_muted)
    update_tray_status()
    logging.info("初期化処理が完了しました。")

def is_process_running(process_name: str) -> bool:
    """psutil で指定プロセス名が存在するか判定"""
    if not process_name:
        return True  # 空なら監視しない扱い
    try:
        target = process_name.lower()
        for p in psutil.process_iter(["name"]):
            name = p.info.get("name")
            if name and name.lower() == target:
                return True
    except Exception as e:
        logging.warning(f"プロセス監視中に例外: {e}")
    return False

# --- ゆかコネNEOプロセス監視 ---
def process_monitor_thread(config: dict, interval_sec: int = 10):
    """
    config['TARGET_PROCESS'] を interval_sec 秒おきに監視し、
    見つからなければログを出して終了する。
    """
    global is_running
    target = (config.get("TARGET_PROCESS") or "").strip()

    # 監視対象が未設定なら監視しない（要件に合わせてここは厳格にしてもOK）
    if not target:
        logging.info("TARGET_PROCESS 未設定のためプロセス監視は行いません")
        return

    logging.info(f"プロセス監視開始: TARGET_PROCESS={target}, interval={interval_sec}s")

    while is_running:
        time.sleep(interval_sec)
        if not is_running:
            break

        if not is_process_running(target):
            logging.error(f"プロセス監視による終了: {target} が見つかりません")
            # 終了処理は既存の cleanup() に寄せる
            cleanup()
            break

# --- メイン処理 ---
def main():
    global APP_NAME, DEBUG_MODE
    global XSO_PORT, YUKACONE_HTTP_PORT, YUKACONE_WS_PORT
    global translation_logger
    global xso_ws

    config = load_config()

    APP_NAME = config.get("app_name", "YncneoXSOBridge")
    DEBUG_MODE = bool(config.get("debug", False))

    log_path = setup_logger(APP_NAME, DEBUG_MODE)
    logging.info(f"開始: {APP_NAME}")

    # --- PROGRAM_DIR 相当（実行ファイルのあるディレクトリ） ---
    if getattr(sys, 'frozen', False):
        program_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        program_dir = os.path.dirname(os.path.abspath(__file__))

    # --- TranslationLogger 初期化 ---
    stable_sec = config.get("PROCESS_STABLE_SEC", 10)
    flush_interval = config.get("FLUSH_INTERVAL_SEC", 5)

    translation_logger = TranslationLogger(
        base_dir=program_dir,
        stable_sec=stable_sec,
        flush_interval=flush_interval,
    )
    translation_logger.start()
    logging.info(
        f"TranslationLogger started (stable={stable_sec}s, flush={flush_interval}s, dir={os.path.join(program_dir, 'log')})"
    )
    
    # XSOはそのまま config から抜く
    try:
        XSO_PORT = urlparse(config.get("xso_endpoint")).port
    except Exception:
        XSO_PORT = None

    # ---- レジストリから Yukacone ポート読み込み ----
    try:
        YUKACONE_HTTP_PORT = read_yncneo_port(config,
                                              "Yncneo_Registry_Value_Http",
                                              "Yukacone HTTP")
        YUKACONE_WS_PORT   = read_yncneo_port(config,
                                              "Yncneo_Registry_Value_Websocket",
                                              "Yukacone WebSocket")
    except Exception as e:
        logging.error(f"ポート取得失敗。終了します: {e}")
        sys.exit(1)

    # ---- URLを固定で構築 ----
    config["yukacone_endpoint"] = f"http://127.0.0.1:{YUKACONE_HTTP_PORT}/api"
    config["yukacone_translationlog_ws"] = f"ws://127.0.0.1:{YUKACONE_WS_PORT}/text"

    logging.info(f"Yukacone HTTP Endpoint      : {config['yukacone_endpoint']}")
    logging.info(f"Yukacone WebSocket Endpoint : {config['yukacone_translationlog_ws']}")

    # --- トレイ起動 ---
    global tray_controller, tray_status

    # まず現在の状態からステータス文字列を組み立てる（この時点では tray_controller は None なので単に tray_status を作るだけ）
    update_tray_status()

    # TrayController を初期化してアイコンを表示
    tray_controller = TrayController(
        app_name=APP_NAME,
        on_exit_callback=cleanup,   # Exit メニューから cleanup() を呼ぶ
        icon_filename="icon.ico",
    )
    tray_controller.start(tray_status)

    # --- XSOverlay への接続 ---
    xso_ws = connect_to_xsoverlay(config) # グローバル変数に格納
    if xso_ws is None:
        logging.error("XSOverlayへの接続に失敗しました。プログラムを終了します。")
        cleanup()
    logging.info("XSOverlayへ接続しました。")

    data_ws_thread = threading.Thread(target=connect_to_data_ws, args=(config, xso_ws,), daemon=True)
    data_ws_thread.start()

    initialize(config, xso_ws)
    sync_thread = threading.Thread(target=periodic_mute_sync, args=(config, xso_ws), daemon=True)
    sync_thread.start()

    key_listener_thread = threading.Thread(target=media_key_listener, args=(xso_ws, config), daemon=True)
    key_listener_thread.start()

    reconws_thread = threading.Thread(target=periodic_xso_reconnect, args=(config,), daemon=True)
    reconws_thread.start()

    # --- プロセス監視スレッド ---
    proc_mon_thread = threading.Thread(
        target=process_monitor_thread,
        args=(config, 10),
        daemon=True,
    )
    proc_mon_thread.start()

    while is_running:
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            break

    cleanup()

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    main()
