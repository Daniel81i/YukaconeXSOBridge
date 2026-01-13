<div align="center">

# XSOYukaconeBridge

<p align="center">
  <img src="./assets/XSOYukaconeBridge.png" style="border-radius: 100px;" width="200" height="200" alt="XSOYukaconeBridge">
</p>

<p align="center">
  <a href="./LICENSE">
    <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="license">
  </a>
  <img src="https://img.shields.io/badge/python-3.11.x-blue?logo=python&logoColor=edb641" alt="python">
</p>
</div>

XSOverlay と ゆかコネ（Yukakone）をつなぐ **翻訳表示＆操作ブリッジ** です。  
- XSOverlay メディア情報欄に（Online / Mute、プロファイル名・エンジン）を表示
- XSOverlay メディア操作(=Windows の メディアキー（Play/Pause, Next, Previous）)で、ゆかコネのミュート切替・翻訳プロファイル切替
- ゆかコネの 翻訳結果をログに出力

---

## 目次
- [機能](#機能)
- [前提](#前提)
- [セットアップ](#セットアップ)
- [設定ファイル `config.json`](#設定ファイル-configjson)
- [使い方](#使い方)
- [ログ出力](#ログ出力)
- [PyInstaller で exe 化](#pyinstaller-で-exe-化)
- [ライセンス](#ライセンス)

---

## 機能
- **XSOverlay メディア欄表示更新**  
  `UpdateMediaPlayerInformation` を送信（タイトル: Online/Mute、アーティスト: プロファイル名＋エンジン等）。
- **ゆかコネ制御**  
  `入力言語切り替え`, `翻訳言語切り替え`, `翻訳一時停止`
- **XSOverlay メディアキー操作（=Windowsメディアキー操作）  
  Play/Pause でミュート切替、Next / Previous で翻訳プロファイル循環（切替後は自動で Online）。
- **翻訳ログ保存**  
  - **1秒間更新が無い**、または **新しい `MessageID` に更新された際に翻訳ログ出力**
  を実現。
- **終了キー検知**  
  SIGINT/SIGTERM でクリーンアップし、未確定メッセージがあればログへ確定。

---

## 前提
- **Python**: 3.8+ 目安
- **動作に必要なプロセス**
  - XSOverlay（WebSocket 有効）
  - ゆかコネ本体（API エンドポイント稼働）

---

## セットアップ

### 1) 仮想環境（任意・推奨：cmd.exe）
```bat
py -m venv .venv
.\.venv\Scriptsctivate.bat
```

### 2) 依存パッケージ
```bat
pip install requests websocket-client pynput
```
> `json`, `logging`, `threading` などは Python 標準ライブラリです。

---

## 設定ファイル `config.json`

`XSOYukaconeBridge.py` と同じフォルダに置きます。  
PyInstaller の exe でも **実行ファイルと同じディレクトリ** から読み込みます。

```json
{
  "app_name": "YukaBridge",
  "debug": false,

  "xso_endpoint": "ws://127.0.0.1:42070",
  "yukacone_endpoint": "http://127.0.0.1:15520",
  "yukacone_translationlog_ws": "ws://127.0.0.1:50000/text",

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
  ]
}
```

- `app_name`: XSOverlay へ送る sender/クライアント名。未指定時は `"YukaBridge"`。
- `xso_endpoint`: XSOverlay の WebSocket（**例**: `ws://127.0.0.1:42070`）。接続時に `/?client=<app_name>` を付与。
- `yukacone_endpoint`: ゆかコネ API のベース URL（本プログラムが `/setRecognitionParam` 等を付与）。
- `yukacone_translationlog_ws`: 発話の受信 (WebSocket)。
- `translation_profiles[*]`
  - `name`: 表示用ラベル（XSOverlay の artist に反映）
  - `recognition_language`: 認識言語
  - `translation_param`: `{ "slot", "language", "engine" }` をゆかコネへ送信。

---

## 使い方

### 実行
```bat
python XSOYukaconeBridge.py
```
- 実行フォルダに **`config.json`** を置いてください。


### キー操作（XSOverlay メディアキー）
- **Play/Pause** … ゆかコネ **Mute / Online** 切替（XSOverlay 表示も更新）
- **Next / Previous** … 翻訳プロファイル切替（切替後は自動で **Online**）

> コンソール上でCtrl+C で終了します。

---

## ログ出力
- **メインログ**: `logs/<スクリプト名>_YYYYMMDDhhmmss.log`（起動フォルダ直下に自動作成）
- **発話/翻訳ログ**: `translationlogs/data_log_YYYYMMDDhhmmss.log`
  - 1行: `MessageID,timestamp,textList(JSON)` 形式

> `debug: true` でメインログ詳細化。

---

## PyInstaller で exe 化
```bat
pip install pyinstaller
pyinstaller --onefile --name XSOYukaconeBridge XSOYukaconeBridge.py
```
- exe と同じフォルダに **`config.json`** を置いてください。
- ログフォルダ（`logs`, `translationlogs`）は **exe と同じ階層**に自動作成されます。

---

## ライセンス
MIT ライセンス
