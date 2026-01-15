import os
import json
import time
import threading
import logging
from datetime import datetime


class TranslationLogger:
    """
    YukarinetteLogger 相当の翻訳ログロジックを、
    別モジュールとして切り出したクラス。

    - ./log/translation-YYYY-MM-DD-HHMMSS.log に追記
    - MsgID が一定時間変化しなかったら1行として確定
    """

    def __init__(self, base_dir: str, stable_sec: float = 10.0, flush_interval: float = 1.0):
        # ./log 固定
        self.log_dir = os.path.join(base_dir, "log")
        os.makedirs(self.log_dir, exist_ok=True)

        ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        self.log_filename = f"translation-{ts}.log"

        self.stable_sec = stable_sec        # 何秒更新が止まったら確定とみなすか
        self.flush_interval = flush_interval  # 何秒おきに安定チェックするか

        self.current_id = None
        self.last_data = None
        self.last_update_time = None

        self._lock = threading.Lock()
        self._stop = False
        self._thread = None

    # ----------------------------------------
    # 公開API
    # ----------------------------------------
    def start(self):
        """バックグラウンドで安定チェック用スレッドを開始"""
        if self._thread is not None:
            return
        self._stop = False
        self._thread = threading.Thread(target=self._periodic_flush_loop, daemon=True)
        self._thread.start()
        logging.info("TranslationLogger started.")

    def stop(self):
        """スレッド停止＆残りのメッセージを強制フラッシュ"""
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        with self._lock:
            self._flush_locked()
        logging.info("TranslationLogger stopped.")

    def add_yukacone_message(self, data: dict):
        """
        Yukacone WebSocket から受け取った JSON（MessageID, textList 等）を
        Logger 用の内部形式に変換してバッファに追加する。
        """
        converted = self._convert_to_internal_format(data)
        if not converted:
            return

        self._add_message_internal(converted)

    # ----------------------------------------
    # 内部処理
    # ----------------------------------------
    def _convert_to_internal_format(self, data: dict):
        """
        {MessageID, textList, ...} → {MsgID, Lang1, Text1, Lang2, Text2}
        という形に変換する。

        ※ textList の実際の構造に応じてここを調整する必要あり
        """
        msg_id = data.get("MessageID")
        if msg_id is None:
            return None

        text_list = data.get("textList") or []
        if len(text_list) < 2:
            # 2言語揃っていない場合はログ対象外にするなど
            return None

        # 例: textList = [{ "Lang": "ja", "Text": "こんにちは" }, { "Lang": "en", "Text": "Hello" }]
        lang1 = text_list[0].get("Lang", "")
        text1 = text_list[0].get("Text", "")
        lang2 = text_list[1].get("Lang", "")
        text2 = text_list[1].get("Text", "")

        return {
            "MsgID": msg_id,
            "Lang1": lang1,
            "Text1": text1,
            "Lang2": lang2,
            "Text2": text2,
        }

    def _add_message_internal(self, msg: dict):
        msg_id = msg.get("MsgID")
        if msg_id is None:
            logging.warning("Message Error: No MsgID")
            return

        now = time.time()
        with self._lock:
            if self.current_id is None:
                # 初回
                self.current_id = msg_id
                self.last_data = msg
                self.last_update_time = now
                return

            if msg_id != self.current_id:
                # 別IDが来た → 旧IDを確定してから新IDに切り替え
                self._flush_locked()
                self.current_id = msg_id
                self.last_data = msg
                self.last_update_time = now
            else:
                # 同じIDの更新 → 最新に差し替え
                self.last_data = msg
                self.last_update_time = now

    def _periodic_flush_loop(self):
        while not self._stop:
            time.sleep(self.flush_interval)
            with self._lock:
                if self.current_id is None or self.last_update_time is None:
                    continue

                now = time.time()
                if now - self.last_update_time >= self.stable_sec:
                    # 一定時間更新が止まっていれば確定
                    self._flush_locked()

    def _flush_locked(self):
        if not self.last_data:
            return

        timestamp = datetime.now().strftime("%Y%m%d-%H:%M:%S%f")[:-3]
        lang1 = self.last_data.get("Lang1", "")
        text1 = self.last_data.get("Text1", "")
        lang2 = self.last_data.get("Lang2", "")
        text2 = self.last_data.get("Text2", "")

        line = f"{timestamp},{lang1}:{text1},{lang2}:{text2}"

        log_path = os.path.join(self.log_dir, self.log_filename)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            logging.info(f"[TranslationLog] {line}")
        except Exception as e:
            logging.error(f"Translation log write error: {e}")

        # 状態リセット
        self.current_id = None
        self.last_data = None
        self.last_update_time = None
