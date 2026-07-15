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
Issue #7 Phase 1では、同じdocument contextを共有したまま、main canvas用の高解像度描画とpage organizer用の低解像度サムネイル描画を別の`RenderCacheKey`で要求する。結果と失敗はpage indexだけではなくcache key単位でUIへ振り分け、main viewとthumbnail sidebarが互いの描画結果を取り違えないようにする。

### PageOrganizer

連続ページ表示とは別に、ページ一覧、current page marker、multi-selection、低解像度サムネイルの表示を担当する。`QListView` + model + delegateで構成し、選択中ページ集合とmain viewerのcurrent pageを分離して扱う。

- 通常クリックはsingle selectionとページ移動
- Shiftはrange selection
- Ctrl/Commandはnon-contiguous multi-selection
- viewer側のスクロールでcurrent pageが変わっても、organizerの既存selectionは解除しない
- サムネイル要求はvisible rowsと隣接行だけに限定し、全文書のeager rasterizeは行わない
- thumbnail logical zoomはpage geometryとtarget rectangleから算出し、main canvasとは別の`RenderCacheKey`で管理する
- desired thumbnail pagesとexpected cache keysを一体で保持し、generation変更後はvisible rowsを明示的に再通知する
- desired外になったページへ遅れて届いたsuccess/failureは捨て、Phase 1のselection変更はdirty stateや`CommandHistory`へ影響させない
- PDFium bitmap、`to_pil()`のsource image、RGBA conversion imageは明示的にcloseし、cleanup failureはbest effortでログ化する

### CoordinateMapper

PDFの左下原点・point座標とQtの左上原点・pixel座標を一元変換する。CropBox、MediaBox、ページ回転、ズーム、DPIを考慮する。

### CommandBus

編集操作をCommand化する。各Commandは実行、取り消し、再実行、説明、影響ページを持つ。Issue #9では runtime 専用の per-document `CommandHistory` を導入し、Undo/Redo と save 時の clean marker をここで管理する。

- executable command stack は Python オブジェクトとしてメモリ上だけに保持し、再起動後の replay は行わない
- `DocumentSession.operation_history` は recovery metadata に保存する文字列の監査履歴であり、Undo/Redo stack とは別物として扱う
- 保存成功時は current history position を clean marker にし、Undo/Redo stack 自体は保持する
- `DocumentCommand.execute()` / `undo()` / `redo()` は partial mutation を残さない atomic contract を守る
- `CompoundCommand` は child command の途中失敗時に rollback して execute / undo / redo の atomicity を満たす
- 復旧された dirty session は stack が空でも dirty state を維持し、初回保存まで Undo/Redo は無効とする
- `affected_pages` は page-level invalidation の拡張点として保持し、`None` は whole-document refresh を意味する
- Issue #7 Phase 2では、永続ページ操作を `RotatePagesCommand` のような working-copy 専用 command として追加し、実行前に RenderService 側の document backend を有限待ちで解放してから mutate する
- 永続ページ操作の成功後は、同じ `PdfView` を破棄せずに作業コピーを再オープンし、current page・page organizer selection・検索 query を復元する
- page count が変わる mutation は `PageIndexTransition` を返し、cache 再利用用 mapping と current-page mapping を明示する
- duplicate 実行時は source page の直後へ clone を 1 回ずつ挿入し、execute / redo 後は duplicate pages だけを selection として復元する
- duplicate undo 後は original source pages を selection として復元し、selected current page が duplicate だった場合は対応する original page へ戻す

### SaveService

一時ファイルに完全保存し、構造検証と再オープン検証が通った場合のみ対象ファイルを置換する。署名対応時には増分保存を別経路で追加する。
既存保存先がある場合はPOSIX modeを可能な範囲で引き継ぎ、置換後は親directoryのfsyncをbest effortで行う。session workspace配下は永続保存先として扱わない。さらに `TargetSnapshot` を使って保存開始前と `os.replace()` 直前に対象ファイルの size / mtime を再確認し、別プロセスの変更を検知した場合は replace を中止する。
保存後のsession metadata更新はwarning扱いとし、PDF保存成功自体は失敗に戻さない。

### SessionWorkspaceManager

PDFを開くたびに、`platformdirs`が返すユーザーキャッシュ配下の`pdf-workbench/sessions/<session-id>/`を作成し、`working.pdf`、`session.json`、`session.lock`を配置する。`PdfView`と今後の編集処理はこの作業コピーを参照し、タブクローズまたは正常終了時にセッションディレクトリを削除する。

### PdfPageMutationService

working copy を直接 mutate する専用サービス。Issue #7 Phase 2では、selected pages の clockwise 90° rotation と selected-page duplication をここで扱う。

- mutation 対象は `DocumentSession.working_copy_path` のみで、`source_path` は直接変更しない
- candidate PDF は workspace 内ではなく working copy と同じ directory に一時作成し、write → fsync → reopen validation → atomic replace の順で適用する
- rotation state 読み取りは pypdf の raw page tree を使い、direct `/Rotate` の有無と effective rotation を区別する
- candidate の最終検証は既存の `PdfDocumentValidator` を再利用し、pikepdf 再オープンと PDFium 描画の両方を通す
- duplicate は各 selected page を source の直後へ 1 回ずつ挿入し、非選択ページと selected pages 同士の相対順序は維持する
- duplicate page は original と同じ indirect page object を再利用しない。annotation array、annotation objects、annotation `/P` back-reference、page-level direct box entry、direct `/Rotate` は duplicate 側で独立していることを検証する
- immutable な content stream や document-level resource は共有を許容するが、page object と annotation object の共有は fail とする
- existing outlines / bookmarks は original destinations を維持し、duplicate page 用の bookmark は自動生成しない
- `/AcroForm` document または selected page に `/Widget` annotation を含む場合は、field tree の破損を避けるため duplication を明示的に拒否する
- duplicate execute / undo の両方で `PageDuplicationReceipt` を使って page order fingerprint、rotation、page boxes、annotations、metadata、outlines、attachments の整合性を検証する

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
