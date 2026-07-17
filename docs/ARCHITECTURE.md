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
- internal drag-and-drop は model を optimistic に並べ替えず、drop 位置から `insertion_slot` を計算して request signal だけを emit する
- `insertion_slot` は reorder 前の page 間位置を表し、selected pages を一度除去したあと、selected pages より前にあった slot 数だけ補正して再挿入する
- non-contiguous selection でも moved pages 同士の相対順序は常に元順序を維持する
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
- delete 実行時は selected pages を 1 回の command として削除し、少なくとも 1 ページが残る状態だけを許可する
- delete 実行後の current page は `PageIndexTransition.current_page_old_to_new` に従い、削除された current page は最も近い右側 survivor、右側がなければ左側 survivor へ寄せる
- delete execute / redo 後の selection override は空 tuple とし、reload 後は mapped current page の 1 件 selection へ fallback する
- delete undo 後は削除前の selected page indexes と current page index を明示的 override で復元する
- reorder 実行時は 1 回の drag 全体を 1 件の `ReorderPagesCommand` とし、selected pages を stable order のまま target slot へ移動する
- reorder execute / redo 後は moved page indexes を selection override として復元し、undo 後は original source indexes を selection override として復元する
- reorder の current page は固定 index へ飛ばさず、`PageIndexTransition.current_page_old_to_new` の permutation で同じ論理ページへ追従させる
- insert-from-PDF 実行時は source page range と `insertion_slot` を canonical な `PageInsertionPlan` へ落とし込み、1 回の import 全体を 1 件の `InsertPagesCommand` として扱う
- insert execute / redo 後は imported pages だけを selection override として復元し、current page は先頭の imported page へ移す
- insert undo 後は import 前の selected pages と current page を明示的 override で復元する
- file dialog や import options dialog の表示中に active document が切り替わった場合は、session identity、working-copy path、page count、save / mutation state を再確認し、古い document へ apply しない
- command が一時リソースを所有する場合は `DocumentCommand.dispose()` で開放し、redo tail 切り捨て、history clear、tab close でも同じ cleanup hook を通す

### SaveService

一時ファイルに完全保存し、構造検証と再オープン検証が通った場合のみ対象ファイルを置換する。署名対応時には増分保存を別経路で追加する。
既存保存先がある場合はPOSIX modeを可能な範囲で引き継ぎ、置換後は親directoryのfsyncをbest effortで行う。session workspace配下は永続保存先として扱わない。さらに `TargetSnapshot` を使って保存開始前と `os.replace()` 直前に対象ファイルの size / mtime を再確認し、別プロセスの変更を検知した場合は replace を中止する。
保存後のsession metadata更新はwarning扱いとし、PDF保存成功自体は失敗に戻さない。

### SessionWorkspaceManager

PDFを開くたびに、`platformdirs`が返すユーザーキャッシュ配下の`pdf-workbench/sessions/<session-id>/`を作成し、`working.pdf`、`session.json`、`session.lock`を配置する。`PdfView`と今後の編集処理はこの作業コピーを参照し、タブクローズまたは正常終了時にセッションディレクトリを削除する。

### PdfPageMutationService

working copy を直接 mutate する専用サービス。Issue #7では、selected pages の clockwise 90° rotation、selected-page duplication / deletion / reordering をここで扱う。

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
- delete は validated disk-backed undo snapshot を working copy と同じ directory に保持し、hard link を優先しつつ失敗時は streaming copy へ fallback する
- delete undo は snapshot を消費せずに別 candidate を作って atomic replace し、redo できる間は snapshot を保持する
- delete execute / redo は survivor page order、page boxes、rotation、annotations、metadata、outlines、named destinations、attachments を before snapshot から再検証する
- delete execute の render-cache mapping は survivor page だけを新 index へ re-key し、deleted page cache は drop する。delete undo では survivor cache を original index へ戻し、復元ページは fresh render 前提とする
- deleted page を指す outline、named destination、OpenAction、annotation `/Dest`、annotation `/A /GoTo` は自動補正せず fail-closed で拒否する
- `/AcroForm`、`/Widget` annotations、tagged PDF の `/StructTreeRoot`、`/PageLabels`、article `/Threads`、cross-page annotation `/P` のように安全な remap を保証できない構造は delete を拒否する
- reorder は page rasterize を行わず PDF page/object の順序だけを 1 回の candidate で組み替え、before/after snapshot、reopen validation、PDFium render validation が通った場合だけ atomic replace する
- reorder の cache remap は full permutation を返し、page count が変わらない場合でも render cache を全破棄せず新 index へ re-key する
- reorder execute / undo / redo は `PageReorderReceipt` を使って page order、rotation、page boxes、annotations、metadata、outlines、named destinations、attachments を再検証する
- reorder でも `/AcroForm`、`/Widget` annotations、`/StructTreeRoot`、`/PageLabels`、`/Threads`、`/OpenAction`、annotation `/Dest`、annotation `/A /GoTo`、cross-page annotation `/P`、unresolved annotation `/P` は fail-closed で拒否する
- insert-from-PDF では source page range parser を Qt から独立した pure value object として持ち、`all`、単一ページ、list、ascending range、mixed list/range を ascending unique tuple へ正規化する
- insert candidate は live source PDF ではなく frozen source snapshot から構築し、redo でも external source を再読込しない
- insert undo は validated disk-backed undo snapshot を working copy と同じ directory に保持して atomic replace し、redo できる間は source snapshot と undo snapshot の両方を保持する
- insert では imported pages の page boxes、effective rotation、supported annotations を materialize する一方、source document の outlines、named destinations、metadata、attachments は取り込まない
- target document 側の metadata、outlines、named destinations、attachments は before snapshot から保持し、target page を指す destination だけ `PageInsertionPlan.target_old_to_new` で remap する
- imported page の annotation `/P` back-reference と optional page boxes は imported page 側へ fixup し、source page object 共有による破損を防ぐ
- insert execute / undo / redo は `PageInsertionReceipt` を使って page order、rotation、page boxes、annotations、metadata、outlines、named destinations、attachments を再検証する

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
