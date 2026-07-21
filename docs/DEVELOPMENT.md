# 開発手順

## セットアップ

```bash
git clone <repository-url>
cd pdf-workbench
python -m venv .venv
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

仮想環境の有効化:

macOS / Linux

```bash
source .venv/bin/activate
```

Windows PowerShell

```powershell
.\.venv\Scripts\Activate.ps1
```

OCR開発も行う場合:

```bash
python -m pip install -e ".[dev,ocr]"
```

OCRmyPDFはPythonパッケージ以外にもTesseract等の外部依存を必要とするため、M4でWindows導入手順を固定する。

macOS と Windows は開発・検証対象とする。Ubuntu は GUI 配布対象ではなく、移植性と単体テスト検証の対象とする。最終配布物の検証と PyInstaller の EXE 生成は Windows で行う。

## 起動

```bash
python -m pdf_workbench
```

## 品質確認

```bash
ruff check .
ruff format --check .
mypy src/pdf_workbench
pytest --cov=pdf_workbench
```

PDF を書き換える PR では、`tests/fixtures/compatibility/` の互換性 corpus を使った回帰確認を追加する。corpus は committed static fixture を正本とし、通常の test / CI 実行中には再生成しない。fixture を更新する場合だけ `scripts/generate_compatibility_fixtures.py` を手動実行し、`manifest.json` の provenance と expectation を更新する。

日本語 text fixture を追加・更新する場合は、redistributable font の出典、version、license、取得 font file の SHA-256 を `tests/fixtures/compatibility/manifest.json` と `tests/fixtures/compatibility/README.md` に記録する。

PDF-writing PR の最低限の回帰チェック:

- pikepdf で構造再オープン
- page count と page order
- MediaBox、CropBox、visible box
- intrinsic rotation
- PDFium で全対象ページを再オープン・描画
- relevant annotation subtype、rectangle、appearance の保存
- duplicate / delete のような page-count-changing command では、page index transition と cache remap を明示的に検証する
- selected-page duplication では original source PDF が Save まで不変であることを確認する
- selected-page duplication では source page の直後に duplicate が入ること、page order が安定していることを検証する
- selected-page deletion では少なくとも 1 ページが残ること、deleted current page が nearest survivor へ mapping されること、undo で original selection / current page が戻ることを検証する
- selected-page deletion では undo snapshot を working copy と同じ directory に置き、SHA-256、pikepdf reopen、PDFium render を通してから receipt に保存する
- selected-page deletion では execute failure、redo tail discard、tab close の各タイミングで undo snapshot cleanup が走ることを確認する
- selected-page reordering では drag-and-drop 1 回を 1 件の command history entry とし、stable relative order、`insertion_slot` semantics、execute / undo / redo の permutation を検証する
- selected-page reordering では page count 不変のまま cache remap が full permutation になること、current page と selection が page identity に追従することを検証する
- selected-page reordering では optimistic model move を行わず、mutation failure 時に organizer order、selection、current page、working copy SHA が維持されることを確認する
- insert-from-PDF では source page range parser を pure function または value object としてテストし、`all`、single、list、ascending range、mixed list/range、空白許容、duplicate / descending / non-ASCII digit rejection を確認する
- insert-from-PDF では canonical `PageInsertionPlan` の `inserted_page_indexes_after` と `target_old_to_new` を UI 非依存で検証する
- insert-from-PDF では source file dialog cancel、options dialog cancel、active document 変更時に command history、dirty state、selection、current page、working copy SHA が変わらないことを確認する
- insert-from-PDF では execute / undo / redo の各経路で page order、imported page text、page boxes、rotation、supported annotations、target metadata / outlines / named destinations / attachments の保存を検証する
- insert-from-PDF では source metadata / outlines / named destinations / attachments を統合しない方針を fixture で明示し、source PDF 自体が Save まで不変であることを確認する
- insert-from-PDF では frozen source snapshot と target-before undo snapshot を working copy と同じ directory に保持し、redo が live source PDF の変更や削除に依存しないことを確認する
- insert-from-PDF では source snapshot 改ざんや replace failure で fail-closed になり、working copy SHA が維持されることを確認する
- replace-from-PDF では source page range parser を挿入と共有し、target selection 数と source selection 数の完全一致を UI と domain の両方で検証する
- replace-from-PDF では canonical `PageReplacementPlan` の replacement pairs、cache remap、current-page identity mapping を UI 非依存で検証する
- replace-from-PDF では source file dialog cancel、options dialog cancel、active document 変更時に command history、dirty state、selection、current page、working copy SHA が変わらないことを確認する
- replace-from-PDF では execute / undo / redo の各経路で page count 不変、page order 不変、replaced page text、page boxes、rotation、supported annotations、target metadata / outlines / named destinations / attachments の保存を検証する
- replace-from-PDF では source metadata / outlines / named destinations / attachments を統合しない方針を fixture で明示し、source PDF 自体が Save まで不変であることを確認する
- replace-from-PDF では frozen source snapshot と target-before undo snapshot を working copy と同じ directory に保持し、redo が live source PDF の変更や削除に依存しないことを確認する
- replace-from-PDF では source snapshot 改ざんや replace failure で fail-closed になり、working copy SHA が維持されることを確認する
- crop-box editing では `/CropBox` だけを変更し、`/MediaBox`、`/Rotate`、content stream、annotation object、annotation `/Rect` が不変であることを確認する
- crop-box editing では display-oriented margins を raw PDF user-space へ変換する pure helper を Qt UI から分離し、rotation 0 / 90 / 180 / 270 と non-zero origin の expected 値を固定した test を追加する
- crop-box editing では direct / inherited / MediaBox fallback の各状態を fixture で分け、undo が元の direct presence を復元することを確認する
- crop-box editing では execute / undo / redo の各経路で selection と current page を維持し、changed pages だけ cache remap が `None` になることを検証する
- crop-box editing では numeric dialog cancel、active document 変更、save/mutation 開始後の stale context で command history、dirty state、working copy SHA が変わらないことを確認する
- crop-box editing では content を削除せず annotation も移動しない方針を README と test に明記し、inside / partial / outside annotation で `/Rect` と fingerprint が不変なことを確認する
- crop-box editing では drag overlay を追加せず、numeric dialog 方式だけを対象にする
- page extraction では `PageExtractionPlan` と range parser を Qt 非依存でテストし、selected pages / range が ascending unique な 0-based tuple へ正規化されることを確認する
- page extraction では source PDF、working copy、current page、selection、dirty state、command history を変更しないことを UI 経路で確認する
- page extraction では output PDF の page count、source order、content、resources、page boxes、rotation、安全な annotations を pikepdf reopen と PDFium render で検証する
- page extraction では source metadata / outlines / named destinations / attachments を統合しないことを fixture で明示する
- page extraction では `/AcroForm`、`/Widget` annotations、annotation action、file attachment、media、cross-page dependency を fail-closed にし、silent removal しないことを確認する
- page extraction では `TargetSnapshot` drift、source revision drift、atomic replace failure、candidate validation failure で source / working copy / existing target が維持されることを確認する
- PDF split では manual range parser と max-pages builder を Qt 非依存でテストし、全ページを昇順でちょうど1回ずつ含むpartition、overlap / gap / reverse / malformed / bool rejection、2 output以上の制約を検証する
- PDF split では deterministic filename、zero padding、source stem handling、basename `.pdf` invariant、path traversal rejection、output directory containment、source / working copy / managed workspace rejection を確認する
- PDF split では overwrite off の既存target衝突がbatch開始snapshotに基づくglobal preflight failureになり、1ファイルも出力しないことを確認する
- PDF split では snapshot取得後にtargetが作成・変更された場合も置換せず、該当outputだけfailed、後続outputは継続、既存targetは維持されることを確認する
- PDF split では overwrite on のtarget snapshot drift、candidate validation failure、atomic replace failureで該当outputだけfailedになり、後続outputと既存targetが維持されることを確認する
- PDF split では source revision driftを専用failureとして扱い、現在outputをfailed、残りをskippedにし、異なるsource revisionの混在output setを作らないことを確認する
- PDF split では thread-safe cancel tokenを実QThread経路で検証し、queued worker slotへ依存せずoutput間で処理し、完了済みoutputを維持しつつ残りをcancelledとしてsummaryへ表示することを確認する
- PDF split では source PDF、working copy、current tab/page、selection、dirty state、command historyを変更しないことをUI経路で確認する
- PDF split では page/object copyを逐次実行し、QImage/Pixmapへのrasterize、並列export、複数candidate同時保持を行わないことを設計レビューとテストdoubleで確認する
- PDF merge では `PdfMergePlan` をQt非依存でテストし、2件以上のresolved unique inputs、source-to-output range mapping、metadata source policy、bookmark policy、output/input同一path拒否、output `.pdf` invariantを検証する
- PDF merge では dialogで固定したsource revisions / target snapshotをworkerとserviceへ渡し、same-page-count content drift、missing / extra expected revision mapping、target creation raceを拒否することを確認する
- PDF merge では input validation、duplicate canonical path、managed workspace rejection、target snapshot drift、source revision drift、cancel、source snapshot cleanup failure、candidate/source snapshot cleanup、existing target preservation を確認する
- PDF merge では source snapshotをsource directoryではなくtarget directoryへ作り、read-only source directory、max concurrent source snapshot count = 1、snapshot fsync、cleanupをinstrumentationで確認する
- PDF merge では output PDF の page count、input order、content、resources、page boxes、rotation、安全な annotations、annotation `/P` を pikepdf reopen と全ページ逐次PDFium renderで検証する
- PDF merge では metadata none / selected-source whitelist、bookmark none / grouped preserve、duplicate filename group suffix、source-local named destinationの明示destination化、output name tree非保持、unsupported bookmark action / malformed destination / document action の fail-closed を fixture で明示する
- PDF merge では worker thread経路で progress、cancel、failure、result summary、thread cleanup、window close cancel、Splitとの同時実行ブロック、active documentを暗黙に含めないことを確認する
- Image-to-PDF では `ImageToPdfPlan` をQt非依存でテストし、unique resolved inputs、frame mapping、output `.pdf` invariant、page size / orientation / margin / scaling / transparency policy、actual-size overflow rejectionを検証する
- Image-to-PDF では Pillow の実format判定を使い、JPEG / PNG / TIFF / BMP / WebP、multi-page TIFF、animated GIF / animated WebP / APNG rejection、static GIF rejection、拡張子だけに依存しない検査をfixtureで確認する
- Image-to-PDF では EXIF orientation、DPI fallback、A4 / Letter / custom / image-fit geometry、fit / fill / actual-size matrix、transparent imageのwhite / black flattenと soft mask保持を検証する
- Image-to-PDF では ICCなしCMYK、floating NaN / infinite pixel、unsupported or animated input、source revision drift、target snapshot drift、managed workspace output、atomic replace failureで既存targetと入力画像が維持されることを確認する
- Image-to-PDF では worker thread経路で progress、cancel、failure、result summary、thread cleanup、window close cancel、Split / Mergeとの同時実行ブロック、active documentを暗黙に変更しないことを確認する
- Image-to-PDF では画像を一括rasterize/list保持せず、source imageを1件ずつ開いてframe単位で処理し、candidateはpikepdf reopenと全ページPDFium render validationを通した後だけatomic replaceすることを設計レビューとテストdoubleで確認する
- page object と annotation object の独立性、annotation `/P` back-reference、raw/effective rotation、all page boxes の保存を検証する
- execute / undo / redo の各経路で、pikepdf 再オープンと PDFium render を通して semantic restoration を確認する
- form / widget page duplication は fail-closed とし、working copy hash が変わらないことを確認する
- page deletion では deleted destination を自動補正せず、outline / named destination / OpenAction / annotation GoTo が deleted page を指す場合は fail-closed にする
- page reordering でも `/AcroForm`、`/Widget` annotations、`/StructTreeRoot`、`/PageLabels`、`/Threads`、`/OpenAction`、annotation `/Dest`、annotation `/A /GoTo`、cross-page annotation `/P`、unresolved annotation `/P` は fail-closed にする
- relevant English/Japanese text の抽出
- source と round-trip output の platform-neutral visual comparison
- byte equality は要求しない
- fixture 追加・更新時は provenance、license、SHA-256、manifest expectation を更新
- Phase A では物理実機、Acrobat、Edge、Chrome による手動確認を要求しない
- working copy を mutate する command では、少なくとも execute / undo / redo の3経路と、reopen validation failure 時に original working copy が維持される経路を追加で確認する
- pypdf の public API だけを使用し、`writer._*` / `reader._*` の private API へ依存しない
- page mutation PR では、通常環境に加えて pypdf 5 系でも focused tests を流し、5.x / 6.x の両方で互換性を確認する

## ブランチ

- `main`: 常に起動可能
- `feat/<issue-number>-<short-name>`
- `fix/<issue-number>-<short-name>`

## コミット

Conventional Commitsを使用する。

```text
feat(viewer): add lazy page rendering
fix(save): preserve page rotation during merge
test(redaction): verify removed text is not extractable
```

## Definition of Done

- 受け入れ条件を満たす
- 単体テストまたは統合テストを追加
- Ruff、mypy、pytestが成功
- PDFを書き換える変更は構造検査と再オープン検査を追加
- UI変更は少なくともCIまたはreview artifactで確認し、必要に応じてmacOSまたはWindowsで追加確認する
- セキュリティ上の制約をREADMEまたはIssueへ記録

Page organizerのような一覧UIでは、1000ページ級テストでも`QWidget`を大量生成せず、`QListView` + model + delegateを優先する。thumbnailはvisible rowsと近傍だけを要求し、main viewerと同じPDFium document contextを共有したまま、低解像度の別`RenderCacheKey`で描画する。
