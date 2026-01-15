import json
import logging
import os
import threading
import time
import signal
import sys
from datetime import datetime
from websocket import WebSocketApp
import requests
from pynput import keyboard
from PIL import Image
import pystray
import winreg
from urllib.parse import urlparse, urlunparse

# グローバル変数の定義
is_running = True
current_translation_index = 0
is_muted = True
translation_profiles_lock = threading.Lock()
APP_NAME = "YukaBridge"  # デフォルトのアプリ名
reconnect_attempts = 0  # データ用WebSocketの再接続試行回数
reconnect_lock = threading.Lock()  # 再接続試行回数を保護するロック

# 新しい機能のためのグローバル変数
last_message_data = None
log_timer = None
data_log_lock = threading.Lock()
xso_ws = None  # XSOverlayのWebSocketオブジェクトを格納するグローバル変数

# 認識言語のデフォルト値を定義する新しいグローバル変数
DEFAULT_RECOGNITION_LANGUAGE = "ja"
last_recognition_language = DEFAULT_RECOGNITION_LANGUAGE  # 前回の認識言語を保持

# タスクトレイ用
tray_icon = None
tray_status = "Initializing..."
XSO_PORT = None
YUKACONE_HTTP_PORT = None
YUKACONE_WS_PORT = None
DEBUG_MODE = False

# --- シグナルハンドラーとクリーンアップ ---
def signal_handler(sig, frame):
    """終了シグナルを検知し、プログラムを安全に終了させるためのハンドラー"""
    global is_running
    logging.info("終了シグナルを検知しました。プログラムを安全に終了します...")
    is_running = False
    cleanup()

def cleanup():
    """プログラム終了時に必要なクリーンアップ処理を行う"""
    global is_running
    logging.info("クリーンアップ処理を開始します...")
    is_running = False
    
    # 未処理のメッセージがあればログに出力
    with data_log_lock:
        if last_message_data:
            log_message_to_file(last_message_data)
    
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
        logging.debug(f"URLからポート抽出に失敗: url={url}, err={e}")
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
    hive_name = config.get("Yncneo_Registry_Hive")
    base_path = config.get("Yncneo_Registry_Path")
    sub_name = config.get(value_key_name)

    hive = get_registry_hive_from_name(hive_name)
    subkey_path = base_path + "\\" + sub_name

    try:
        with winreg.OpenKey(hive, subkey_path) as key:
            value, _ = winreg.QueryValueEx(key, "")
            if not isinstance(value, int):
                raise ValueError(f"{desc} のレジストリ値が整数ではありません: {value}")
            logging.info(f"{desc} ポート値取得: {value}")
            return value
    except Exception as e:
        raise RuntimeError(f"{desc} のレジストリ読み出しに失敗: {hive_name}\\{subkey_path} / {e}")

# --- 共通: PyInstaller 対応のリソースパス ---
def resource_path(relative_path: str) -> str:
    """PyInstaller の onefile 実行時でもリソースにアクセスできるパスを返す"""
    if hasattr(sys, "_MEIPASS"):
        base_path = sys._MEIPASS  # type: ignore[attr-defined]
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


# --- タスクトレイ用: アイコン画像作成 ---
def create_tray_image():
    """タスクトレイ用アイコン画像を返す"""
    try:
        icon_path = resource_path("icon.ico")
        if os.path.exists(icon_path):
            return Image.open(icon_path)
        else:
            logging.warning("icon.ico が見つからないため、透明のプレースホルダーアイコンを使用します。")
    except Exception as e:
        logging.error(f"タスクトレイアイコン読み込み中にエラー: {e}")

    # フォールバック: 透明 64x64
    return Image.new("RGBA", (64, 64), (0, 0, 0, 0))

def on_tray_exit(icon, item):
    """タスクトレイメニューからの終了処理"""
    logging.info("タスクトレイメニューから終了が選択されました。")
    global is_running
    is_running = False
    # cleanup の中で tray_icon.stop() も呼ぶ
    cleanup()

def setup_tray_icon():
    """タスクトレイアイコンを作成して表示する"""
    global tray_icon
    image = create_tray_image()
    update_tray_status()  # APP_NAME / is_muted をもとに初期ステータスを構成

    menu = pystray.Menu(
        pystray.MenuItem("Exit", on_tray_exit)
    )

    tray_icon = pystray.Icon(APP_NAME, image, tray_status, menu)
    # メインスレッドをブロックしないようにデタッチ
    tray_icon.run_detached()

def update_tray_status():
    """タスクトレイのタイトル（ホバー時のステータス表示）を更新する"""
    global tray_status, tray_icon
    global XSO_PORT, YUKACONE_HTTP_PORT, YUKACONE_WS_PORT, DEBUG_MODE

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

    if tray_icon is not None:
        tray_icon.title = tray_status


def cleanup():
    """プログラム終了時に必要なクリーンアップ処理を行う"""
    global is_running, tray_icon
    logging.info("クリーンアップ処理を開始します...")
    is_running = False
    
    # 未処理のメッセージがあればログに出力
    with data_log_lock:
        if last_message_data:
            log_message_to_file(last_message_data)
    
    # ★ 追加: トレイアイコンを停止
    if tray_icon is not None:
        try:
            tray_icon.visible = False
            tray_icon.stop()
        except Exception as e:
            logging.error(f"トレイアイコン停止中にエラー: {e}")
    
    logging.info("プログラムを終了します...")
    sys.exit(0)

# --- ログの初期化 ---
def setup_logger(script_name, debug):
    """メインロガーを初期化する"""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    executable_path = os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__))
    log_dir = os.path.join(executable_path, 'logs')

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

def setup_data_logger():
    """データロガーを初期化する"""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    # 実行中のプログラムのディレクトリパスを取得
    executable_path = os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__))
    # ログディレクトリを 'translationlogs' に変更
    log_dir = os.path.join(executable_path, 'translationlogs')

    if not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir)
            print(f"ディレクトリを作成しました: {log_dir}")
        except OSError as e:
            print(f"ディレクトリ作成中にエラーが発生しました: {e}")
            sys.exit(1)

    data_log_file = os.path.join(log_dir, f"data_log_{timestamp}.log")
    
    data_logger = logging.getLogger("data_logger")
    data_logger.setLevel(logging.INFO)
    
    formatter = logging.Formatter('%(asctime)s %(message)s')

    file_handler = logging.FileHandler(data_log_file, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    data_logger.addHandler(file_handler)
    
    return data_log_file

def log_message_to_file(message_data):
    """指定されたデータを専用の形式でログに出力する"""
    if message_data is None:
        return
    
    data_logger = logging.getLogger("data_logger")
    
    try:
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S:%f")[:-3]
        message_id = message_data.get("MessageID")
        text_list = message_data.get("textList")
        
        # textListを結合して文字列にする
        text_str = json.dumps(text_list, ensure_ascii=False)
        
        log_line = f'{message_id},{timestamp},{text_str}'
        data_logger.info(log_line)
        logging.info(f"ログ出力: {text_str}")
        
    except Exception as e:
        logging.error(f"ログ出力中にエラーが発生しました: {e}")

# --- 翻訳された文字列を取得するヘルパー関数 ---
def get_translated_text(data, source_lang):
    """認識言語以外の翻訳文字列を取得する"""
    text_list = data.get("textList", {})
    
    # 認識言語のキー以外のキーを探す
    for lang_code, text in text_list.items():
        if lang_code != source_lang:
            return text
            
    return None

# --- ゆかコネのAPI呼び出し ---
def call_yukacone_api(base_url, path, params):
    """ゆかコネAPIを呼び出す"""
    try:
        url = f"{base_url}{path}"
        logging.info(f"{path} 実行: {params}")
        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()
        logging.info(f"{path} 成功: {response.text}")
    except Exception as e:
        logging.error(f"{path} 失敗: {e}")

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

# --- XSOverlay表示更新 ---
def send_xso_status(ws, config, index, is_muted):
    """XSOverlayのメディア情報表示を更新する"""
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
                is_muted = not is_muted
                cmd = "/mute-on" if is_muted else "/mute-off"
                logging.info(f"翻訳{'一時停止' if is_muted else '再開'}を実行: {cmd}")
                call_yukacone_api(config["yukacone_endpoint"], cmd, {})
                send_xso_status(ws, config, current_translation_index, is_muted)
                update_tray_status()
            elif key == keyboard.Key.media_next:
                with translation_profiles_lock:
                    current_translation_index = (current_translation_index + 1) % len(config["translation_profiles"])
                update_translation(config, current_translation_index)
                time.sleep(0.5)
                is_muted = False
                cmd = "/mute-on" if is_muted else "/mute-off"
                logging.info(f"翻訳{'一時停止' if is_muted else '再開'}を実行: {cmd}")
                call_yukacone_api(config["yukacone_endpoint"], cmd, {})
                send_xso_status(ws, config, current_translation_index, is_muted)
                update_tray_status()
            elif key == keyboard.Key.media_previous:
                with translation_profiles_lock:
                    current_translation_index = (current_translation_index - 1) % len(config["translation_profiles"])
                update_translation(config, current_translation_index)
                time.sleep(0.5)
                is_muted = False
                cmd = "/mute-on" if is_muted else "/mute-off"
                logging.info(f"翻訳{'一時停止' if is_muted else '再開'}を実行: {cmd}")
                call_yukacone_api(config["yukacone_endpoint"], cmd, {})
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
            websocket_url = f'{config["xso_endpoint"]}/?client={APP_NAME}'
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

# --- データ用 WebSocket接続 ---
def connect_to_data_ws(config, xso_ws):
    global is_running, reconnect_attempts, last_message_data, log_timer
    ws_url = config.get("yukacone_translationlog_ws", "ws://127.0.0.1:50000/text")

    # ★ 追加: 保留中メッセージのスナップショット
    pending_message = None
    pending_id = None

    def flush_pending(reason: str):
        nonlocal pending_message, pending_id
        global log_timer, last_message_data
        if not pending_message:
            return

        # ログ出力（＝確定）
        log_message_to_file(pending_message)

        # 必要なら通知（従来ロジックを流用）
        try:
            current_profile = config["translation_profiles"][current_translation_index]
            if current_profile.get("xso_notification", False):
                translated_text = get_translated_text(pending_message, last_recognition_language)
                if translated_text and xso_ws:
                    send_xso_notification(xso_ws, config, translated_text)
        except Exception as e:
            logging.error(f"通知処理中エラー: {e}")

        # 保留クリア（＝MessageID = NULL）
        pending_message = None
        pending_id = None
        last_message_data = None
        if log_timer and log_timer.is_alive():
            log_timer.cancel()
        log_timer = None

    def arm_timer():
        """1秒の静寂で確定させるためのタイマーを張る（同ID更新時は張り直し）"""
        global log_timer
        if log_timer and log_timer.is_alive():
            log_timer.cancel()

        def _timeout():
            with data_log_lock:
                flush_pending("timeout")

        log_timer = threading.Timer(1.0, _timeout)
        log_timer.start()

    def on_message(ws, message):
        nonlocal pending_message, pending_id
        global last_message_data
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logging.error("受信したメッセージのJSON形式が不正です。")
            return

        incoming_id = data.get("MessageID")

        with data_log_lock:
            # 初回 or 保留なし（MessageID = NULL）
            if pending_id is None:
                pending_message = data
                pending_id = incoming_id
                last_message_data = pending_message
                arm_timer()
                return

            # 同じID → スナップショットを更新してタイマー張り直し（最後の更新から1秒）
            if incoming_id == pending_id:
                pending_message = data
                last_message_data = pending_message  # 既存のcleanup互換
                arm_timer()
                return

            # 別IDが来た → 旧IDを即時フラッシュ＋新IDを保留し直す
            flush_pending("new_id")
            pending_message = data
            pending_id = incoming_id
            last_message_data = pending_message
            arm_timer()

    def on_open(ws):
        global reconnect_attempts
        logging.info("データ用WebSocketに接続しました。")
        reconnect_attempts = 0

    def on_close(ws, close_status_code, close_msg):
        global is_running, reconnect_attempts
        with reconnect_lock:
            logging.warning("データ用WebSocketが切断されました。")
            reconnect_attempts += 1
            if reconnect_attempts <= 2 and is_running:
                logging.warning(f"{reconnect_attempts}回目の再接続を試行します...")
                time.sleep(3)
            else:
                if is_running:
                    logging.error("再接続試行回数が上限に達しました。プログラムを終了します。")
                    is_running = False

    def on_error(ws, err):
        logging.error(f"データ用WebSocketエラー: {err}")

    while is_running:
        logging.info("データ用WebSocketへの接続を試行します...")
        ws = WebSocketApp(ws_url, on_open=on_open, on_message=on_message, on_close=on_close, on_error=on_error)
        ws.run_forever()
        if not is_running:
            break
        time.sleep(3)

# --- 初期化処理 ---
def initialize(config, ws):
    """プログラムの初期化処理を行う"""
    global last_recognition_language
    logging.info("初期化処理を開始します。")

    # 認識言語の初期設定をupdate_translationに任せる
    # ここでの直接的なAPI呼び出しは削除
    
    # 翻訳プロファイルの設定
    update_translation(config, current_translation_index)
    
    time.sleep(3.0)
    call_yukacone_api(config["yukacone_endpoint"], "/mute-on", {})
    send_xso_status(ws, config, current_translation_index, is_muted)
    logging.info("初期化処理が完了しました。")

# --- メイン処理 ---
def main():
    global APP_NAME, DEBUG_MODE
    global XSO_PORT, YUKACONE_HTTP_PORT, YUKACONE_WS_PORT

    config = load_config()

    APP_NAME = config.get("app_name", "YukaBridge")
    DEBUG_MODE = bool(config.get("debug", False))

    log_path = setup_logger(APP_NAME, DEBUG_MODE)
    logging.info(f"開始: {APP_NAME}")

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
    config["yukacone_endpoint"] = f"http://127.0.0.1:{YUKACONE_HTTP_PORT}/"
    config["yukacone_translationlog_ws"] = f"ws://127.0.0.1:{YUKACONE_WS_PORT}/"

    logging.info(f"HTTP  → {config['yukacone_endpoint']}")
    logging.info(f"WS    → {config['yukacone_translationlog_ws']}")

    # トレイ起動・WS接続
    setup_tray_icon()
    update_tray_status()
    
    xso_ws = connect_to_xsoverlay(config) # グローバル変数に格納
    if xso_ws is None:
        logging.error("XSOverlayへの接続に失敗しました。プログラムを終了します。")
        cleanup()
    logging.info("XSOverlayへ接続しました。")

    data_ws_thread = threading.Thread(target=connect_to_data_ws, args=(config, xso_ws,), daemon=True)
    data_ws_thread.start()

    initialize(config, xso_ws)

    key_listener_thread = threading.Thread(target=media_key_listener, args=(xso_ws, config), daemon=True)
    key_listener_thread.start()
    
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
