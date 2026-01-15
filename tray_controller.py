# tray_controller.py
import os
import sys
import logging
from typing import Callable, Optional

import pystray
from PIL import Image


def resource_path(relative_path: str) -> str:
    """PyInstaller の onefile 実行時でもリソースにアクセスできるパスを返す"""
    if hasattr(sys, "_MEIPASS"):
        base_path = sys._MEIPASS  # type: ignore[attr-defined]
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


class TrayController:
    """
    タスクトレイアイコンの共通制御クラス。

    - icon.ico を使ったタスクトレイアイコン表示
    - ホバー時タイトル（ツールチップ）更新
    - メニューから Exit を選んだときにコールバック呼び出し
    """

    def __init__(
        self,
        app_name: str,
        on_exit_callback: Optional[Callable[[], None]],
        icon_filename: str = "icon.ico",
    ) -> None:
        self.app_name = app_name
        self.on_exit_callback = on_exit_callback
        self.icon_filename = icon_filename
        self.icon: Optional[pystray.Icon] = None

    # -------------------------
    # 公開 API
    # -------------------------
    def start(self, initial_tooltip: str) -> None:
        """トレイアイコンを作成して非ブロッキングで表示する"""
        image = self._create_tray_image()

        menu = pystray.Menu(
            pystray.MenuItem("Exit", self._on_tray_exit)
        )

        self.icon = pystray.Icon(self.app_name, image, initial_tooltip, menu)
        # メインスレッドをブロックしないようにデタッチ
        self.icon.run_detached()
        logging.info("Tray icon started")

    def update_tooltip(self, text: str) -> None:
        """ホバー時のタイトル（ツールチップ）を更新する"""
        if self.icon is not None:
            self.icon.title = text

    def stop(self) -> None:
        """トレイアイコンを明示的に停止する"""
        if self.icon is not None:
            try:
                self.icon.visible = False
                self.icon.stop()
                logging.info("Tray icon stopped")
            except Exception as e:
                logging.error(f"トレイアイコン停止中にエラー: {e}")

    # -------------------------
    # 内部ヘルパ
    # -------------------------
    def _create_tray_image(self) -> Image.Image:
        """タスクトレイ用アイコン画像を返す"""
        try:
            icon_path = resource_path(self.icon_filename)
            if os.path.exists(icon_path):
                return Image.open(icon_path)
            else:
                logging.warning(
                    f"{self.icon_filename} が見つからないため、透明のプレースホルダーアイコンを使用します。"
                )
        except Exception as e:
            logging.error(f"タスクトレイアイコン読み込み中にエラー: {e}")

        # フォールバック: 透明 64x64
        return Image.new("RGBA", (64, 64), (0, 0, 0, 0))

    def _on_tray_exit(self, icon, item) -> None:
        """タスクトレイメニューから Exit が選択されたとき"""
        logging.info("タスクトレイメニューから終了が選択されました。")
        if self.on_exit_callback:
            # cleanup() などを呼び出す
            self.on_exit_callback()
