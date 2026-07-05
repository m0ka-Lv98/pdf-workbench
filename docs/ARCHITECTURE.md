# アーキテクチャ

## 基本原則

1. Qt UI、ドメインモデル、PDFバックエンドを分離する。
2. 読み取り・表示と書き換えを別エンジンに分ける。
3. 原本を直接更新せず、一時ファイルへの安全保存後に置換する。
4. すべての編集をCommandとして表現し、Undo/Redoとバッチ実行を共通化する。
5. 外部ツールはワーカープロセスで実行し、GUIスレッドをブロックしない。
6. PDFを信頼できない入力として扱い、JavaScriptや外部起動アクションを実行しない。

## レイヤー

```text
ui
  PySide6の画面、入力イベント、描画オーバーレイ

application
  ユースケース、Command、Undo/Redo、タスク管理

domain
  DocumentSession、PageRef、Annotation、Redaction、FormField

services
  PDFium、pikepdf、pypdf、OCRmyPDF、PyInstallerとのアダプター

infrastructure
  設定、ログ、一時ファイル、Windows統合
```

## 主要コンポーネント

### DocumentSession

開いている文書ごとの状態を保持する。PDFライブラリ固有オブジェクトを直接持たせず、再オープン可能なパスと論理状態を保持する。

### RenderService

PDFiumを使い、ページを必要な解像度で遅延レンダリングする。将来はLRUキャッシュ、バックグラウンド描画、表示領域優先キューを追加する。

### CoordinateMapper

PDFの左下原点・point座標とQtの左上原点・pixel座標を一元変換する。CropBox、MediaBox、ページ回転、ズーム、DPIを考慮する。

### CommandBus

編集操作をCommand化する。各Commandは実行、取り消し、再実行、説明、影響ページを持つ。

### SaveService

一時ファイルに完全保存し、構造検証と再オープン検証が通った場合のみ対象ファイルを置換する。署名対応時には増分保存を別経路で追加する。

## プロセス分離

以下は別プロセスで実行する。

- OCRmyPDF / Tesseract
- LibreOffice変換を追加する場合
- 大規模圧縮
- PDF比較のページレンダリング
- 不正PDFの構造検査

## データ保存先

ユーザー設定、ログ、キャッシュは実行ファイルの隣ではなく、`platformdirs`で取得したWindowsのユーザーディレクトリへ保存する。
