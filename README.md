# XSOYukaconeBridge

XSOverlay と ゆかコネ（Yukakone）をつなぐ **翻訳表示＆操作ブリッジ** です。  
- XSOverlay の WebSocket に接続して、**メディア情報（Online / Mute、プロファイル名・エンジン）を表示**
- Windows の **メディアキー**（Play/Pause, Next, Previous）で、**ゆかコネのミュート切替・翻訳プロファイル切替**
- ゆかコネの **翻訳ログ WebSocket** から受信した文を、**1秒の静寂 or 新しい MessageID 到着**のどちらか早い方で**ちょうど一度だけ**ログに確定
- プロファイルごとに設定可能な **XSOverlay 通知**（翻訳結果のトースト表示）

---

## 目次
- [機能](#機能)
- [前提](#前提)
- [セットアップ](#セットアップ)
- [設定ファイル `config.json`](#設定ファイル-configjson)
- [使い方](#使い方)
- [ログ出力](#ログ出力)
- [PyInstaller で exe 化](#pyinstaller-で-exe-化)
- [トラブルシュート](#トラブルシュート)
- [ライセンス](#ライセンス)

---

## 機能
- **XSOverlay 表示更新**  
  `UpdateMediaPlayerInformation` を送信（タイトル: Online/Mute、アーティスト: プロファイル名＋エンジン等）。
- **XSOverlay 通知**（任意）  
  プロファイル設定に `xso_notification: true` で、翻訳テキストを `SendNotification` で通知。
- **ゆかコネ制御**  
  `/setRecognitionParam`, `/setTranslationParam`, `/mute-on`, `/mute-off` を HTTP GET でコール。
- **メディアキー連携**（Windows）  
  Play/Pause でミュート切替、Next / Previous で翻訳プロファイル循環（切替後は自動で Online）。
- **翻訳ログ取り込み**  
  `yukacone_translationlog_ws` から `MessageID` と `textList` を受信し、
  - 同一 `MessageID` の更新が続く間は **保留**
  - **1秒間更新が無い**、または **新しい `MessageID` が到着** したら **一度だけ確定ログ**
  を実現。
- **安全終了**  
  SIGINT/SIGTERM でクリーンアップし、未確定メッセージがあればログへ確定。

---

## 前提
- **OS**: Windows（メディアキー検出のため）
- **Python**: 3.8+ 目安
- **動作に必要なプロセス**
  - XSOverlay（WebSocket 有効）
  - ゆかコネ本体（API エンドポイント稼働）
  - ゆかコネ翻訳ログサーバ（`yukacone_translationlog_ws`）

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
  "yukacone_endpoint": "http://127.0.0.1:12345",
  "yukacone_translationlog_ws": "ws://127.0.0.1:50000/text",

  "translation_profiles": [
    {
      "name": "JP→EN (DeepL)",
      "recognition_language": "ja",
      "translation_param": { "slot": 1, "language": "en-US", "engine": "deeplpro" },
      "xso_notification": true
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
- `yukacone_translationlog_ws`: 翻訳ログ WebSocket（`MessageID` / `textList` を受信）。
- `translation_profiles[*]`
  - `name`: 表示用ラベル（XSOverlay の artist に反映）
  - `recognition_language`: 認識言語（同一時は API 呼び出しスキップ）
  - `translation_param`: `{ "slot", "language", "engine" }` をゆかコネへ送信。
  - `xso_notification`: true で、そのプロファイル時に翻訳結果を XSOverlay 通知。

---

## 使い方

### 実行
```bat
python XSOYukaconeBridge.py
```

### キー操作（Windows メディアキー）
- **Play/Pause** … ゆかコネ **Mute / Online** 切替（XSOverlay 表示も更新）
- **Next / Previous** … 翻訳プロファイル切替（切替後は自動で **Online**）

> Ctrl+C で終了すると、最後に未確定の受信メッセージがあればログへ書き出してから終了します。

---

## ログ出力
- **メインログ**: `logs/<スクリプト名>_YYYYMMDDhhmmss.log`（起動フォルダ直下に自動作成）
- **翻訳データログ**: `translationlogs/data_log_YYYYMMDDhhmmss.log`
  - 1行: `MessageID,timestamp,textList(JSON)` 形式
  - **同一IDの更新中は保留**し、**1秒静寂**または**新ID到着**で確定出力。

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

## トラブルシュート
- **XSOverlay に繋がらない**  
  `xso_endpoint` を確認。起動時に `/?client=<app_name>` で接続します。
- **翻訳ログが出ない/二重に見える**  
  実装は **一度だけ確定** するよう調整済みですが、メインログとデータログに **似た内容が双方出る** ため重複に見えることがあります。必要ならメイン側の INFO を減らしてください。
- **データ WebSocket が切断される**  
  自動再接続は **最大2回**。以降は安全に終了します。`yukacone_translationlog_ws` を確認。

---

## ライセンス
MIT ライセンス相当（必要に応じて差し替えてください）。
