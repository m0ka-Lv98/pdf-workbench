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
永続ファイルの`source_path`と、アプリ内部で扱う`working_copy_path`、セッション単位の`workspace_directory`を明確に分離する。Issue #6では、`recovered_from_interrupted_session`、`requires_save_as`、`recovery_source_status`、`source_status`、`source_status_checked_at`、`source_change_detected_at`も保持し、異常終了からの復元状態と外部変更状態をUIへ伝える。

### RenderService

PDFiumを使い、ページを必要な解像度で遅延レンダリングする。現在は単一worker thread上でPDFium呼び出しを直列化し、表示領域優先キュー、隣接ページprefetch、LRUキャッシュを持つ。

### CoordinateMapper

PDFの左下原点・point座標とQtの左上原点・pixel座標を一元変換する。CropBox、MediaBox、ページ回転、ズーム、DPIを考慮する。

### CommandBus

編集操作をCommand化する。各Commandは実行、取り消し、再実行、説明、影響ページを持つ。Issue #9では runtime 専用の per-document `CommandHistory` を導入し、Undo/Redo と save 時の clean marker をここで管理する。

- executable command stack は Python オブジェクトとしてメモリ上だけに保持し、再起動後の replay は行わない
- `DocumentSession.operation_history` は recovery metadata に保存する文字列の監査履歴であり、Undo/Redo stack とは別物として扱う
- 保存成功時は current history position を clean marker にし、Undo/Redo stack 自体は保持する
- 復旧された dirty session は stack が空でも dirty state を維持し、初回保存まで Undo/Redo は無効とする
- `affected_pages` は page-level invalidation の拡張点として保持し、`None` は whole-document refresh を意味する

### SaveService

一時ファイルに完全保存し、構造検証と再オープン検証が通った場合のみ対象ファイルを置換する。署名対応時には増分保存を別経路で追加する。
既存保存先がある場合はPOSIX modeを可能な範囲で引き継ぎ、置換後は親directoryのfsyncをbest effortで行う。session workspace配下は永続保存先として扱わない。さらに `TargetSnapshot` を使って保存開始前と `os.replace()` 直前に対象ファイルの size / mtime を再確認し、別プロセスの変更を検知した場合は replace を中止する。
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

### SourceChangeMonitor

開いている元PDFの外部変更監視を担当する。

- `QFileSystemWatcher` で source file と parent directory の両方を監視する
- watcher event は通知契機としてだけ使い、最終判定は `SourceFileInspector` が size / mtime を再取得して行う
- watcher event 欠落に備えて 2 秒の polling fallback を併用する
- アプリが foreground に戻ったときも全タブを即時再確認する
- external change が見つかった tab は `[外部変更]` suffix、tooltip、warning banner、`Save As` 強制へ反映する
- save / save as 成功時は新しい fingerprint で baseline を更新し、アプリ自身の保存を false positive として扱わない

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
Issue #6では、作業コピー、保存用一時PDF、復旧metadata、session lock、外部変更監視の watcher/polling 状態をこの配下で扱う。network filesystem や removable storage では watcher event が完全ではないため polling fallback と activation check を併用するが、size と mtime を完全に偽装した変更や filesystem API レベルの完全な conditional replace までは保証しない。
