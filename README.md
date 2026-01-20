<div align="center">

# YncneoXSOBridge （ゆかコネNeo - XSOverlay 連携ツール）

<p align="center">
  <img src="./assets/YncneoXSOBridge.png" style="border-radius: 100px;" width="200" height="200" alt="YncneoXSOBridge">
</p>

<p align="center">
  <a href="./LICENSE">
    <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="license">
  </a>
  <img src="https://img.shields.io/badge/python-3.11.x-blue?logo=python&logoColor=edb641" alt="python">
</p>
</div>

YncneoXSOBridge は、ゆかコネNEO と XSOverlay を連携し、
- 翻訳の Mute 状態の制御・同期
- 翻訳設定の切り替え
- 翻訳ログの保存
- タスクトレイ常駐操作

を行う、Windows 向けの常駐型ブリッジアプリです。

---

## 目次
- [機能](#機能)
- [前提](#前提)
- [設定ファイル `config.json`](#設定ファイル-configjson)
- [使い方](#使い方)
- [ログ出力](#ログ出力)
- [セットアップ](#セットアップ)
- [PyInstaller で exe 化](#pyinstaller-で-exe-化)
- [ライセンス](#ライセンス)

---

## 機能
- **XSOverlay メディア欄表示更新**
  
  XSOverlay メディア欄表示を利用して翻訳ステータスを表示します
  
  `UpdateMediaPlayerInformation` を送信（タイトル: Online/Mute、アーティスト: プロファイル名＋エンジン等）。
- **ゆかコネ制御**  
  `入力言語切り替え`, `翻訳言語切り替え`, `翻訳一時停止`
- **XSOverlay メディアキー操作（=Windowsメディアキー操作）  
  Play/Pause でミュート切替、Next / Previous で翻訳プロファイル切替（切替後は自動で Online）。
- **タスクトレイ常駐**  
  終了する場合はタスクトレイから終了させてください。
- **翻訳結果得ログ出力**  
  翻訳途中のログを整理、統合して最終確定翻訳結果と思われるもののみログ出力。
- **ゆかコネのプロセス監視**  
  ゆかコネのプロセスの有無を監視してゆかコネが終了した場合本プログラムも終了する。（起動はゆかコネ側の自動起動プラグインからの起動を想定。）

---

## 動作環境
- **Python**: 3.11+
- **動作に必要なプロセス**
  - XSOverlay（WebSocket 有効）
  - ゆかコネNEO本体（API エンドポイント有効）

### ゆかコネNEO のポート設定値を以下のレジストリから取得

```data
HKEY_CURRENT_USER
 └ Software
   └ YukarinetteConnectorNeo
      ├ HTTP      (DWORD)  # HTTP API ポート
      └ WebSocket (DWORD)  # 翻訳ログ WebSocket ポート
```

## 設定ファイル `config.json`

`YncneoXSOBridge.py` と同じフォルダに置きます。  
PyInstaller の exe でも **実行ファイルと同じディレクトリ** から読み込みます。

```json
{
  "app_name": "YncneoXSOBridge",
  "xso_endpoint": "ws://127.0.0.1:42070",
  "Yncneo_Registry_Hive": "HKEY_CURRENT_USER",
  "Yncneo_Registry_Path": "Software\\YukarinetteConnectorNeo",
  "Yncneo_Registry_Value_Http": "HTTP",
  "Yncneo_Registry_Value_Websocket": "WebSocket",
  "FLUSH_INTERVAL_SEC": 5,
  "PROCESS_STABLE_SEC": 10,
  "WS_RECONNECT_DELAY_SEC": 5,
  "WS_MAX_RECONNECT_SEC": 60,
  "TARGET_PROCESS": "YNC_Neo.exe",
  "translation_profiles": [
    {
      "name": "JP→EN (DeepL)",
      "recognition_language": "ja",
      "translation_param": { "slot": 1, "language": "en-US", "engine": "deeplpro" },
      "xso_notification": false
    },
    {
      "name": "JP→KO",
      "recognition_language": "ja",
      "translation_param": { "slot": 2, "language": "ko-KR", "engine": "deeplpro" },
      "xso_notification": false
    }
  ],
  "debug": false
}
```

- `app_name`: XSOverlay へ送る sender/クライアント名。未指定時は `"YncneoXSOBridge"`。
- `xso_endpoint`: XSOverlay の WebSocket（**例**: `ws://127.0.0.1:42070`）。接続時に `/?client=<app_name>` を付与。
- `Yncneo_Registry_Hive`: ゆかコネNEOのレジストリ Hive  
  例: `"HKEY_CURRENT_USER"`（固定）
- `Yncneo_Registry_Path`: ゆかコネNEOのベースキー  
  例: `"Software\\YukarinetteConnectorNeo"`（固定）
- `Yncneo_Registry_Value_Http`: HTTP ポートが格納されているサブキー名  
  例: `"HTTP"` → `HKCU\Software\YukarinetteConnectorNeo\HTTP` の既定値(DWORD)をポート値として読み込み（固定）
- `Yncneo_Registry_Value_Websocket`: WebSocket ポートが格納されているサブキー名  
  例: `"WebSocket"` → `HKCU\Software\YukarinetteConnectorNeo\WebSocket` の既定値(DWORD)をポート値として読み込み（固定）
- `FLUSH_INTERVAL_SEC`: 翻訳ログ確定待ち時間
- `WS_RECONNECT_DELAY_SEC`: XSOverlay 再接続間隔
- `WS_MAX_RECONNECT_SEC`: XSOverlay 接続最大待ち時間
- `PROCESS_STABLE_SEC`: プロセス監視間隔
- `TARGET_PROCESS`: 監視対象プロセス名 "YNC_Neo.exe"（固定）

読み込んだポートを使って、アプリ内部で次のURLを自動生成します。

- `yukacone_endpoint` → `http://127.0.0.1:(HTTPポート)/api`
- `yukacone_translationlog_ws` → `ws://127.0.0.1:(WebSocketポート)/`

- `translation_profiles[*]`
  - `name`: 表示用ラベル（XSOverlay の artist に反映）
  - `recognition_language`: 認識言語
  - `translation_param`: `{ "slot", "language", "engine" }` をゆかコネへ送信。
- `debug`: `true` で詳細な DEBUG ログを有効化（通常は `false` 推奨）

---

## 使い方

### 実行
```bat
YncneoXSOBridge.exe
```
- 実行フォルダに **`config.json`** を置いてください。


### キー操作（XSOverlay メディアキー）
- **Play/Pause** … ゆかコネ **Mute / Online** 切替（XSOverlay 表示も更新）
- **Next / Previous** … 翻訳プロファイル切替（切替後は自動で **Online**）

タスクトレイに常駐するのでタスクトレイから終了させてください。

---

## ログ出力
- **メインログ**: `logs/<スクリプト名>_YYYYMMDDhhmmss.log`（起動フォルダ直下に自動作成）
- **発話/翻訳ログ**: `translationlogs/data_log_YYYYMMDDhhmmss.log`
  - 1行: `MessageID,timestamp,textList(JSON)` 形式
-  `debug: true` でメインログ詳細化。

---

## セットアップ

### 1) 仮想環境（任意・推奨：cmd.exe）
```bat
py -m venv .venv
.\.venv\Scripts\activate.bat
```

### 2) 依存パッケージ
```bat
pip install requests websocket-client pynput
```
> `json`, `logging`, `threading` などは Python 標準ライブラリです。

---

### PyInstaller で exe 化
```bat
pip install pyinstaller
pyinstaller --onefile --name YncneoXSOBridge YncneoXSOBridge.py
```
- exe と同じフォルダに **`config.json`** を置いてください。
- ログフォルダ（`logs`, `translationlogs`）は **exe と同じ階層**に自動作成されます。

---

## ライセンス
MIT ライセンス

本プログラムはAIを利用して情報収集、コーディング、環境構築を行っています、Chat-GPT,Copilot,Gemini,Claude
