# PDF compatibility corpus (Phase A)

このディレクトリは、将来の PDF 書き換え機能で使い回すための committed regression corpus です。通常の `pytest` や CI 実行時に再生成せず、リポジトリに含まれる静的 fixture を正本として扱います。

## 目的

- PDF 構造、page geometry、intrinsic rotation、annotation subtype、抽出可能テキスト、PDFium 描画の回帰検知
- no-op round-trip 後の structure / rendering 比較
- Issue #19 Phase A のテスト基盤提供

Phase B の fixture はまだ含みません。画像 PDF、暗号化 PDF、破損 PDF、フォーム、添付ファイル、線形化 PDF などは後続フェーズで追加します。

## 収録 fixture

- `digital-basic.pdf`
  - 通常のデジタル生成 PDF
  - 2 ページ、図形、塗り、線、固有の視覚マーカー
- `english-text.pdf`
  - PDFium で抽出可能な英語テキスト
  - 期待文字列: `PDF Workbench English compatibility fixture`
- `japanese-text.pdf`
  - PDFium で抽出可能な横書き日本語テキスト
  - 期待文字列: `PDFワークベンチ 日本語互換性テスト`
- `page-boxes.pdf`
  - MediaBox / CropBox の差異と non-zero origin
- `rotations.pdf`
  - `/Rotate` = `0 / 90 / 180 / 270`
- `annotations.pdf`
  - 標準 annotation subtype を含む構造 fixture

## 生成方法

生成スクリプト:

- `scripts/generate_compatibility_fixtures.py`

この corpus は第三者サイトから PDF を転載していません。すべてリポジトリ内の生成スクリプトで作成しています。

通常テストでは fixture を再生成しません。fixture を更新する場合だけ、手動で次を実行します。

```bash
PDF_WORKBENCH_JP_FONT=/absolute/path/to/NotoSansCJKjp-Regular.otf \
QT_QPA_PLATFORM=offscreen \
python scripts/generate_compatibility_fixtures.py
```

または:

```bash
QT_QPA_PLATFORM=offscreen \
python scripts/generate_compatibility_fixtures.py \
  --font-path /absolute/path/to/NotoSansCJKjp-Regular.otf
```

要件:

- 日本語フォントは redistributable なものを明示指定する
- テスト中にネットワーク download を行わない
- full font binary をこのディレクトリへコミットしない
- 生成後は manifest と回帰テストを必ず更新・実行する

## 埋め込みフォント provenance

`japanese-text.pdf` などの日本語 fixture は、subset embedding された以下のフォントを利用します。

- family: `Noto Sans CJK JP`
- version: `2.004`
- source: <https://github.com/notofonts/noto-cjk/tree/main/Sans>
- license: `SIL Open Font License 1.1`
- source file SHA-256:
  `68a3fc98800b2a27b371f2fb79991daf3633bd89309d4ffaa6946fd587f375b5`

ライセンス文は `licenses/OFL-1.1.txt` に保持します。

## ライセンス

- fixture PDFs: repository-generated, MIT
- embedded Japanese font subset: SIL Open Font License 1.1

## manifest

- `manifest.json` は corpus expectation の正本です
- 各 fixture の `sha256`、用途、provenance、page count、各ページの geometry / rotation / annotation subtype、text fixture の expected text を保持します
- Python テストへ同じ期待値を大量に重複ハードコードしない方針です

fixture を更新したら、生成スクリプトを再実行して `manifest.json` の SHA-256 と expectation を更新してください。

更新後の確認:

```bash
ruff check .
ruff format --check .
mypy src/pdf_workbench
QT_QPA_PLATFORM=offscreen pytest --cov=pdf_workbench
QT_QPA_PLATFORM=offscreen pytest -q tests/test_pdf_compatibility_corpus.py
git diff --check
```
