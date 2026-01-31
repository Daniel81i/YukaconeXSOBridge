import time
import threading
import json
import logging

class TranslationLogger:
    def __init__(
        self,
        base_dir=None,
        stable_seconds=10,
        flush_interval=5,
        on_finalize=None,
    ):
        self.base_dir = base_dir
        self.stable_seconds = stable_seconds
        self.flush_interval = flush_interval
        self.on_finalize = on_finalize or self._default_finalize

        self._lock = threading.Lock()
        self._pending = {}
        self._last_mid = None

        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._thread.start()
        logging.info("TranslationLogger started (stable=%ss, flush=%ss)", self.stable_seconds, self.flush_interval)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        self.flush_all(reason="shutdown")

    def add_message(self, data: dict):
        # DEBUG raw dump（前回要望）
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            try:
                raw = json.dumps(data, ensure_ascii=False, indent=2)
                logging.debug("TranslationLogger RAW WS message:\n%s", raw)
            except Exception:
                logging.debug("TranslationLogger RAW WS message (non-json): %r", data)

        mid = data.get("MessageID")
        if not mid:
            return

        if data.get("isDeleted") is True:
            with self._lock:
                if mid in self._pending:
                    del self._pending[mid]
            logging.info("TranslationLogger: deleted mid=%s", mid)
            return

        now = time.time()

        with self._lock:
            # mid が変わったら直前を確定
            if self._last_mid is not None and self._last_mid != mid:
                self._finalize_locked(self._last_mid, reason="mid_changed")

            if mid in self._pending:
                self._pending[mid]["data"] = data
                self._pending[mid]["last"] = now
            else:
                # ★first_seen（新しいMessageIDとして取得した時刻）
                self._pending[mid] = {"data": data, "first": now, "last": now}

            self._last_mid = mid

    def flush_all(self, reason="manual"):
        with self._lock:
            for mid in list(self._pending.keys()):
                self._finalize_locked(mid, reason=reason)

    def _flush_loop(self):
        while self._running:
            time.sleep(self.flush_interval)
            self._flush_stable()

    def _flush_stable(self):
        now = time.time()
        with self._lock:
            for mid, rec in list(self._pending.items()):
                if (now - rec["last"]) >= self.stable_seconds:
                    self._finalize_locked(mid, reason="stable_timeout")

    def _finalize_locked(self, mid: str, reason: str):
        rec = self._pending.get(mid)
        if not rec:
            return

        data = rec["data"]
        first = rec["first"]
        finalize_now = time.time()

        # ★経過秒（小数不要）
        elapsed_s = int(finalize_now - first)

        line = self._format_line(data, reason)

        # ★ログ先頭時刻を first_seen に差し替える & 経過秒を追加
        extra = {
            "created_override": first,  # フォーマッタ側で asctime に使う
            "elapsed_tag": f"{elapsed_s}s" if reason in ("mid_changed", "stable_timeout") else "",
        }

        try:
            self.on_finalize(line, data, extra)
        except TypeError:
            # 既存互換（on_finalize が extra を受けない場合）
            self._default_finalize(line, data, extra)
        except Exception:
            logging.exception("TranslationLogger finalize failed (mid=%s)", mid)

        del self._pending[mid]

        if self._last_mid == mid:
            self._last_mid = None

    def _format_line(self, data: dict, reason: str):
        mid = data.get("MessageID", "")
        talker = data.get("talkerName") or data.get("talkerID", "")
        fixed = data.get("fixedText", False)

        tl = data.get("textList") or {}
        if not isinstance(tl, dict):
            tl = {"raw": str(tl)}

        parts = []
        for lang in sorted(tl.keys()):
            text = tl.get(lang)
            if text is None:
                continue
            text = str(text).replace("\r", "\\r").replace("\n", "\\n")
            parts.append(f"{lang}={text}")

        joined = " | ".join(parts)
        return f"[finalize:{reason}] {talker} mid={mid} fixed={1 if fixed else 0} {joined}"

    # ★extra を受ける形に（recommended）
    def _default_finalize(self, line: str, data: dict, extra: dict | None = None):
        logging.info(line, extra=extra or {})
