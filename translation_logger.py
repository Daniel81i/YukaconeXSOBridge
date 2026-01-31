import os
import time
import threading
import logging
import json
from datetime import datetime


class TranslationLogger:
    """
    翻訳ログ保持・確定ロジック

    受信データ例（WebSocket）:
    {
      "textList": {"ja":"...", "en":"..."},
      "MessageID": "...",
      "talkerName": "...",
      "fixedText": true/false,
      ...
    }

    仕様:
    - MessageID は更新中同じIDが来る（確定まで同じ）
    - MessageID が変わったら、保持中の旧MessageIDを確定ログ出力
    - 一定時間更新が止まったら確定ログ出力（stable_sec）
    - textList の言語キーは不定（ja/en/ko/cn...）。1個以上あれば保持対象
    - DEBUG 時は受信データをテキスト化してログ出力
    - 確定ログ行には「取得開始時刻（MessageIDを初めて見た時刻）」と「経過秒」を入れる
    """

    def __init__(self, base_dir: str, stable_sec: float = 10.0, flush_interval: float = 5.0):
        # ./log 固定
        self.log_dir = os.path.join(base_dir, "log")
        os.makedirs(self.log_dir, exist_ok=True)

        ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        self.log_filename = f"translation-{ts}.log"

        self.stable_sec = float(stable_sec)
        self.flush_interval = float(flush_interval)

        # 1件ずつ保持する前提（あなたの仕様に合わせる）
        self.current_id = None
        self.first_seen_time = None      # MessageID を最初に見た時刻（epoch秒）
        self.last_update_time = None     # 最終更新時刻（epoch秒）
        self.last_data = None            # 最新受信データ（内部形式）

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
        logging.info("TranslationLogger started (stable=%ss, flush=%ss, dir=%s)",
                     int(self.stable_sec), int(self.flush_interval), self.log_dir)

    def stop(self):
        """スレッド停止＆残りのメッセージを強制フラッシュ"""
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        with self._lock:
            self._flush_locked(reason="shutdown")
        logging.info("TranslationLogger stopped.")

    def add_yukacone_message(self, data: dict):
        """
        Yukacone WebSocket から受け取った JSON を内部形式へ変換し、バッファに追加する。
        """
        # DEBUG指定時：受信データをテキスト化して出力
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            try:
                raw = json.dumps(data, ensure_ascii=False, indent=2)
                logging.debug("TranslationLogger RAW WS message:\n%s", raw)
            except Exception:
                logging.debug("TranslationLogger RAW WS message (repr): %r", data)

        converted = self._convert_to_internal_format(data)
        if not converted:
            return

        self._add_message_internal(converted)

    # ----------------------------------------
    # 内部処理
    # ----------------------------------------
    def _convert_to_internal_format(self, data: dict):
        """
        受信JSON（MessageID, textList(dict) 等）を内部形式へ。

        内部形式:
        {
          "MsgID": "...",
          "Talker": "...",
          "Fixed": bool,
          "Texts": {"ja":"...", "en":"...", ...}  # 言語キー不定
        }
        """
        msg_id = data.get("MessageID")
        if not msg_id:
            return None

        text_map = data.get("textList") or {}
        if not isinstance(text_map, dict):
            # 想定外形式は捨てる（必要ならここで救う）
            return None

        # 1個以上の言語があれば対象
        if len(text_map.keys()) < 1:
            return None

        # isDeleted は保持/確定の対象外（必要なら削除ログへ拡張可）
        if data.get("isDeleted") is True:
            return None

        talker = data.get("talkerName") or data.get("talkerID") or ""
        fixed = bool(data.get("fixedText", False))

        # 値を文字列化＆改行つぶし（ログ1行化）
        cleaned = {}
        for lang, txt in text_map.items():
            if txt is None:
                continue
            s = str(txt).replace("\r", "\\r").replace("\n", "\\n")
            cleaned[str(lang)] = s

        if not cleaned:
            return None

        return {
            "MsgID": str(msg_id),
            "Talker": str(talker),
            "Fixed": fixed,
            "Texts": cleaned,
        }

    def _add_message_internal(self, msg: dict):
        msg_id = msg.get("MsgID")
        if not msg_id:
            logging.warning("TranslationLogger: No MsgID after convert")
            return

        now = time.time()
        with self._lock:
            if self.current_id is None:
                # 初回MessageID
                self.current_id = msg_id
                self.first_seen_time = now
                self.last_update_time = now
                self.last_data = msg
                return

            if msg_id != self.current_id:
                # 別IDが来た → 旧IDを確定してから新IDへ
                self._flush_locked(reason="mid_changed", flush_now=now)

                self.current_id = msg_id
                self.first_seen_time = now
                self.last_update_time = now
                self.last_data = msg
            else:
                # 同じID更新 → 最新保持
                self.last_update_time = now
                self.last_data = msg

    def _periodic_flush_loop(self):
        while not self._stop:
            time.sleep(self.flush_interval)
            with self._lock:
                if self.current_id is None or self.last_update_time is None:
                    continue
                now = time.time()
                if (now - self.last_update_time) >= self.stable_sec:
                    self._flush_locked(reason="stable_timeout", flush_now=now)

    def _flush_locked(self, reason: str, flush_now: float | None = None):
        """
        保持中のメッセージを「確定」として1行ログに出す。
        - 先頭時刻: MessageID を最初に取得した時刻（first_seen）
        - 追加項目: 経過秒（flush_now - first_seen、整数秒）
        """
        if not self.last_data or not self.current_id or self.first_seen_time is None:
            # 状態が揃ってない場合は何もしない
            self.current_id = None
            self.first_seen_time = None
            self.last_update_time = None
            self.last_data = None
            return

        if flush_now is None:
            flush_now = time.time()

        elapsed_sec = int(max(0.0, flush_now - self.first_seen_time))

        # first_seen をログ行の先頭時刻に採用（ミリ秒まで）
        ts_first = datetime.fromtimestamp(self.first_seen_time).strftime("%Y%m%d-%H:%M:%S%f")[:-3]

        talker = self.last_data.get("Talker", "")
        fixed = 1 if self.last_data.get("Fixed", False) else 0
        texts = self.last_data.get("Texts", {}) or {}

        # 言語キー不定 → ソートして安定化
        parts = []
        for lang in sorted(texts.keys()):
            parts.append(f"{lang}:{texts.get(lang, '')}")

        # ここが「1行フォーマット」（必要なら後で調整）
        # 先頭: first_seen_time / 次: elapsed_sec（整数秒）/ 次: reason / talker / fixed / 本文
        line = f"{ts_first},{elapsed_sec},{reason},{talker},fixed={fixed}," + ",".join(parts)

        log_path = os.path.join(self.log_dir, self.log_filename)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            logging.info("[TranslationLog] %s", line)
        except Exception as e:
            logging.error("Translation log write error: %s", e)

        # 状態リセット
        self.current_id = None
        self.first_seen_time = None
        self.last_update_time = None
        self.last_data = None
