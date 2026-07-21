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
- replace-from-PDF 実行時は target selection と source page range を canonical な `PageReplacementPlan` へ落とし込み、昇順 zip された replacement pairs 全体を 1 件の `ReplacePagesCommand` として扱う
- replace execute / redo 後は replaced target indexes を selection override として復元し、current page は同じ page index のまま維持する
- replace undo 後は replace 前の selected pages と current page を明示的 override で復元する
- crop-box editing では numeric dialog の入力を Qt 非依存の `PageCropPlan` へ落とし込み、selected pages 全体を 1 件の `CropPagesCommand` として扱う
- crop execute / undo / redo 後は selected pages と current page を維持し、changed pages だけ render-cache mapping を `None` にして fresh render させる
- crop dialog の余白は display-oriented left / top / right / bottom として解釈し、0 / 90 / 180 / 270 の effective rotation に応じて raw PDF user-space の `/CropBox` へ変換する
- page extraction、PDF split、PDF merge は current document を変更しない export 操作として扱い、`CommandHistory` へ積まない。UI は `PageExtractionPlan` / `PageSplitPlan` / `PdfMergePlan` を生成して service へ渡し、成功/失敗しても selection、current page、dirty state を変えない
- file dialog や import options dialog の表示中に active document が切り替わった場合は、session identity、working-copy path、page count、save / mutation state を再確認し、古い document へ apply しない
- command が一時リソースを所有する場合は `DocumentCommand.dispose()` で開放し、redo tail 切り捨て、history clear、tab close でも同じ cleanup hook を通す

### SaveService

一時ファイルに完全保存し、構造検証と再オープン検証が通った場合のみ対象ファイルを置換する。署名対応時には増分保存を別経路で追加する。
既存保存先がある場合はPOSIX modeを可能な範囲で引き継ぎ、置換後は親directoryのfsyncをbest effortで行う。session workspace配下は永続保存先として扱わない。さらに `TargetSnapshot` を使って保存開始前と `os.replace()` 直前に対象ファイルの size / mtime を再確認し、別プロセスの変更を検知した場合は replace を中止する。
保存後のsession metadata更新はwarning扱いとし、PDF保存成功自体は失敗に戻さない。

### SessionWorkspaceManager

PDFを開くたびに、`platformdirs`が返すユーザーキャッシュ配下の`pdf-workbench/sessions/<session-id>/`を作成し、`working.pdf`、`session.json`、`session.lock`を配置する。`PdfView`と今後の編集処理はこの作業コピーを参照し、タブクローズまたは正常終了時にセッションディレクトリを削除する。

### PdfPageMutationService

working copy を直接 mutate する専用サービス。Issue #7では、selected pages の clockwise 90° rotation、selected-page duplication / deletion / reordering、insert-from-PDF、replace-from-PDF をここで扱う。

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
- replace candidate も live source PDF ではなく frozen source snapshot から構築し、redo でも external source を再読込しない
- replace undo は validated disk-backed undo snapshot を working copy と同じ directory に保持して atomic replace し、redo できる間は source snapshot と undo snapshot の両方を保持する
- replace では selected target page object を残したまま、source page の contents、page boxes、effective rotation、resources、supported passive annotations を in-place で差し替える
- replace では page count と page order を変えず、render-cache mapping は replaced pages だけ drop し、untouched pages は同じ page index へ re-key する
- replace では target document 側の metadata、outlines、named destinations、attachments を before snapshot から保持し、replaced page を指す bookmark / named destination も同じ page index に残す
- replace では replaced target page の旧 annotation は破棄し、source document の outlines、named destinations、metadata、attachments は統合しない
- replace execute / undo / redo は `PageReplacementReceipt` を使って page order、rotation、page boxes、annotations、metadata、outlines、named destinations、attachments を再検証する
- crop-box editing では `/CropBox` だけを変更し、`/MediaBox`、`/Rotate`、content stream、annotation object、annotation `/Rect`、metadata、outlines、named destinations、attachments は変更しない
- crop state 読み取りは pypdf の raw page tree を使い、direct `/CropBox`、inherited `/CropBox`、CropBox absent による MediaBox fallback を区別する。一方で幾何計算には `PdfRect.normalized()` と同じ方針で normalized geometry を使い、undo 用には元の direct value を保持する
- crop execute では target box を selected page の direct `/CropBox` として materialize し、undo では元の direct presence/value を復元する。対話型 drag overlay は導入せず、現在は numeric dialog だけを提供する
- crop candidate validation では changed page の effective CropBox と rendered dimensions を検証しつつ、MediaBox、rotation、resources、contents、annotations、document-level metadata が不変であることを確認する

### PdfPageExportService / PdfPageSplitService / PdfMergeService / ImageToPdfService

現在の文書、選択された入力PDF、または明示的に選んだ画像から別PDFを作る非破壊exportを担当する。selected pages / page range extraction は `PdfPageExportService` が単一outputとして扱い、PDF split は `PdfPageSplitService` が同じ export service を複数outputへ逐次適用する。PDF merge は `PdfMergeService` が複数入力を1つの独立outputへ逐次copyする。Image-to-PDF は `ImageToPdfService` が Pillow で検査した画像入力を新しい独立PDFへ逐次変換する。

- `PageExtractionPlan` は Qt 非依存のdomain objectで、正の page count、昇順・重複なし・範囲内の source page indexes、source-to-output mapping を検証する
- range parser は UI の 1-based `1-3, 5, 8-10` 構文を 0-based ascending unique tuple へ正規化し、空入力、0以下、page count 超過、逆順range、不正形式、非ASCII数字を拒否する
- export は source PDF / working copy を変更せず、target と同じ directory に candidate PDF を作成して fsync、pikepdf reopen、構造検証、PDFium render validation を通した後だけ atomic replace する
- candidate は pikepdf の page/object copy を使い rasterize しない。page content stream、resources、MediaBox、CropBox、TrimBox、BleedBox、ArtBox、rotation、安全な page annotations を保持する
- source metadata、outlines/bookmarks、named destinations、attachments は出力PDFへ統合しない
- `/AcroForm`、`/Widget` annotations、`/StructTreeRoot`、`/PageLabels`、`/Threads`、`/OpenAction`、annotation action、file attachment、media、cross-page annotation `/P` のような独立PDF化を保証できない構造は fail-closed にする
- target は `TargetSnapshot` を保存前と replace 直前に再確認し、別プロセスの変更が見つかった場合は既存targetを維持して中止する
- `PageSplitPlan` は Qt 非依存のdomain objectで、manual range split と max-pages split の両方を全ページの完全partitionへ正規化する。chunk数は2以上、各chunkは連続範囲、overlap/gapなし、filenameはuniqueで deterministic とする
- split の filename は `<source-stem>_pages_<start>-<end>.pdf` で、1-based page number を `max(4, len(str(page_count)))` 桁に zero padding する。mode差で命名結果を変えず、domain invariant と service boundary の両方で basename `.pdf` と output directory containment を検証する
- split は GUI thread では実行せず、1 outputずつ順番に worker thread から `PdfPageExportService.extract_pages()` を呼ぶ。並列export、eager rasterize、複数candidateの同時保持は行わない
- overwrite off / on のどちらでも各targetの `TargetSnapshot` をbatch開始前に固定する。overwrite off ではsnapshot上の既存target衝突を global preflight failure とし、snapshot後に出現・変更したtargetは置換せず、そのoutputだけ failed として後続outputを継続する
- source revision はbatch開始時に固定し、各output開始前と export service 内で再確認する。途中driftが見つかった場合は現在outputをfailed、残りをskippedにして、異なるsource revisionの混在output setを作らない
- cancellation はGUI threadが所有するthread-safe cancel tokenでoutput間だけに観測し、worker threadのqueued slot処理には依存しない。進行中のatomic exportは中断せず、完了済みoutputは維持し、残りはcancelledとしてcopy可能なsummaryへ含める
- `PdfMergePlan` は Qt 非依存のdomain objectで、2件以上のresolved unique input、positive page count、`.pdf` output path、source-to-output range mapping、metadata source policy、bookmark policy を検証する
- merge は active document に依存しない独立exportで、現在開いているdirty working copyを暗黙に含めない。input listはdialogで明示的に選択・並べ替えたPDFだけを使う
- merge dialog はinput追加時のcanonical path、page count、fingerprint、SHAを保持し、OK時に全inputを再検査する。workerにはdialog時点のexpected source revisionsとtarget snapshotを渡し、service境界でmissing / extra / driftをfail-closedにする
- merge service はsource revisionをdialog時点で固定し、各sourceのsnapshot前、snapshot検証時、replace前に再確認する。処理中にsourceが変わった場合はcandidateを破棄してtargetを維持する
- merge candidate とsource snapshotはtarget directoryに作成し、source directoryへ一時ファイルを書かない。各sourceは同時に1件だけsnapshot/openし、処理後ただちにsnapshotを削除する。ページはpikepdf page/object copyで追加し、全ページrasterizeや複数source openは行わない
- source snapshot cleanup failureは正常完了を拒否する。primary errorが既にある場合はprimary errorを維持し、cleanup failureをログへ残す
- metadataは既定off。選択sourceから `/Title`、`/Author`、`/Subject`、`/Keywords`、`/Creator` だけをコピーし、Producer、日付、custom info、XMPはコピーしない
- bookmarksは既定off。保持する場合は入力filenameごとのsynthetic top-level groupを作り、重複filenameは `(2)` のようにsuffixを付ける。ローカルGoTo destinationだけをoutput page indexへoffsetし、source-local named destinationは明示destinationへ解決する。outputへname treeをコピーせず、remote/file/action/JavaScript/Launchや解決不能destinationはfail-closedにする
- candidate validation はpage countだけでなく、input order、source-to-output mapping、content、resources、MediaBox / CropBox / TrimBox / BleedBox / ArtBox、rotation、安全な annotationsとannotation `/P`、metadata policy、bookmark hierarchy / destination、unsupported root entriesを独立再オープン後に検証する
- Merge用validationでは `PdfDocumentValidator` に全output page indexを渡し、PDFiumで1ページずつrenderしてbitmap/PIL imageを各iterationでcloseする。render imageをlistへ保持しない
- mergeでも `TargetSnapshot` をpreflightとreplace直前に確認し、overwrite offの既存target、snapshot後のtarget作成/変更、managed workspace配下出力を拒否する
- merge worker はGUI threadをブロックせず、progress/cancel/result summaryをQt signalで返す。Split workerとは同時実行しない。window close時はcancel tokenをsetし、GUI threadから`QThread.quit()`を明示してbounded waitし、終了できない場合はcloseを拒否する
- `ImageToPdfPlan` は Qt 非依存のdomain objectで、resolved unique image inputs、frame mapping、output `.pdf` invariant、page size mode、orientation、margins、scaling、transparency policy を検証する
- Image-to-PDF は JPEG / PNG / TIFF / BMP / WebP を Pillow の実formatで判定し、multi-page TIFF は各frameをページ化する。animated GIF / animated WebP / APNG / static GIF、未対応format、拡張子と実formatの不整合は fail-closed にする
- Image-to-PDF geometry は EXIF orientation 後のpixel sizeとDPIから作り、Image Fit / A4 / Letter / Custom、auto orientation、fit / fill / actual size、mm marginを point 単位の page box と draw matrix へ変換する。FILL はcenter cropで、colorとalphaへ同じcropを適用する
- transparency は白/黒flattenまたは soft mask で保持する。ICC付きRGBA / LAはalphaを色変換前に分離し、PRESERVE_ALPHAでは `/SMask` として保持する。ICC付きCMYKは必ずsRGB RGBへ変換し、ICCなしCMYKやICC変換失敗、NaN / infinite pixelを含むfloating imageは拒否する
- 16-bit unsigned、integer、floating point image は8-bitへ決定的に正規化する。floating point imageはextremaだけでなく全pixelを検査し、NaN / positive infinity / negative infinity をfail-closedにする
- Image-to-PDF service はsource revisionをdialog時点で固定し、candidate作成前後とreplace直前に再確認する。targetは `TargetSnapshot` をpreflightとreplace直前に確認し、managed workspace配下出力を拒否する
- Image-to-PDF candidate はtarget directoryに作成し、pikepdf reopen、root構造検証、全ページPDFium render validation、source revision再確認、target snapshot再確認を通過した場合だけ `os.replace()` でatomicに置換する。page validationでは `/Type /Page`、MediaBox、unexpected CropBox / Rotate / page actionなし、単一 `/Im0` XObject、image / SMask dimensions、8-bit component、color space、content stream の限定operator列を検証する
- Image-to-PDF はPillowの実formatを主判定としつつ、入力suffixは既知画像suffixに限定する。truncated image と decompression bomb warning/errorは拒否し、Pillow global stateは復元する
- multi-page TIFF はframeごとのdimensions、mode、DPI、alphaを使用して逐次処理する。file-level dialog summaryは先頭frame情報ベースで、可変frame TIFFの完全なframe一覧表示は将来scopeとする
- Image-to-PDF worker はGUI threadをブロックせず、progress/cancel/result summaryをQt signalで返す。Split / Merge / Image-to-PDF は同時実行しない。window close時はcancel tokenをsetし、GUI threadから`QThread.quit()`を明示してbounded waitし、終了できない場合はcloseを拒否する
- Image-to-PDF は現在開いている document、working copy、selection、current page、dirty state、command history を変更しない。OCR、既存PDFへの画像挿入、metadata/bookmark/attachment生成、Undo / Redo command化は別scopeとする

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
