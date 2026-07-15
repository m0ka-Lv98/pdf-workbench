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
- relevant English/Japanese text の抽出
- source と round-trip output の platform-neutral visual comparison
- byte equality は要求しない
- fixture 追加・更新時は provenance、license、SHA-256、manifest expectation を更新
- Phase A では物理実機、Acrobat、Edge、Chrome による手動確認を要求しない

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
