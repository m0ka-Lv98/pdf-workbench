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
  設定、ログ、一時ファイル、OS統合
```

## 主要コンポーネント

### DocumentSession

開いている文書ごとの状態を保持する。PDFライブラリ固有オブジェクトを直接持たせず、再オープン可能なパスと論理状態を保持する。
永続ファイルの`source_path`と、アプリ内部で扱う`working_copy_path`、セッション単位の`workspace_directory`を明確に分離する。Issue #6 Phase 2では、`recovered_from_interrupted_session`、`requires_save_as`、`recovery_source_status`も保持し、異常終了からの復元状態をUIへ伝える。

### RenderService

PDFiumを使い、ページを必要な解像度で遅延レンダリングする。将来はLRUキャッシュ、バックグラウンド描画、表示領域優先キューを追加する。

### CoordinateMapper

PDFの左下原点・point座標とQtの左上原点・pixel座標を一元変換する。CropBox、MediaBox、ページ回転、ズーム、DPIを考慮する。

### CommandBus

編集操作をCommand化する。各Commandは実行、取り消し、再実行、説明、影響ページを持つ。

### SaveService

一時ファイルに完全保存し、構造検証と再オープン検証が通った場合のみ対象ファイルを置換する。署名対応時には増分保存を別経路で追加する。
既存保存先がある場合はPOSIX modeを可能な範囲で引き継ぎ、置換後は親directoryのfsyncをbest effortで行う。session workspace配下は永続保存先として扱わない。
保存後のsession metadata更新はwarning扱いとし、PDF保存成功自体は失敗に戻さない。

### SessionWorkspaceManager

PDFを開くたびに、`platformdirs`が返すユーザーキャッシュ配下の`pdf-workbench/sessions/<session-id>/`を作成し、`working.pdf`、`session.json`、`session.lock`を配置する。`PdfView`と今後の編集処理はこの作業コピーを参照し、タブクローズまたは正常終了時にセッションディレクトリを削除する。

### SessionRecoveryService

復旧metadataのserialize、atomic write、起動時scan、復元候補の判定を担当する。

- `session.json` は UTF-8 JSON とし、schema version、source fingerprint、page index、zoom、dirty state、operation history を保持する
- metadata は同一workspace内の一時JSONへ書き出したあと `os.replace()` で置換し、POSIXでは親directoryのfsyncをbest effortで行う
- 起動時scanでは sessions root 直下の real directory だけを対象にし、metadata と working PDF の両方を検証する
- malformed metadata、unsupported schema、破損PDF、symlink candidate は自動削除せず、復元不可候補として表示する
- 元PDFの size と mtime を起動時に一度だけ比較し、missing / modified / unreadable では `Save As` を強制する
- `--skip-recovery-prompt` を付けた起動では scan dialog を表示せず、candidate を変更しない

### Workspace Lock

session workspace の active 判定には単なる lock file の存在ではなく OS の file lock を使う。

- POSIX では `fcntl.flock`
- Windows では `msvcrt.locking`
- lock handle はセッションが開いている間だけ保持する
- 起動時scanで lock を取得できなかった workspace は、別プロセスで active とみなして復旧候補へ表示しない
- 「後で」を選んだ候補は lock を release し、次回起動時に再表示できるようにする

## プロセス分離

以下は別プロセスで実行する。

- OCRmyPDF / Tesseract
- LibreOffice変換を追加する場合
- 大規模圧縮
- PDF比較のページレンダリング
- 不正PDFの構造検査

## データ保存先

ユーザー設定、ログ、キャッシュは実行ファイルの隣ではなく、`platformdirs`で取得した各OSのユーザーディレクトリへ保存する。Windowsは `%LOCALAPPDATA%` 系、macOSは `~/Library` 系を利用する。
Issue #6のPhase 2では、作業コピー、保存用一時PDF、復旧metadata、session lock をこの配下で扱う。外部変更の常時監視は未実装で、Issue #6 の残件として維持する。
