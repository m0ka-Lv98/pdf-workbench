# 開発手順

## セットアップ

```powershell
git clone <repository-url>
cd pdf-workbench
uv sync --extra dev
```

OCR開発も行う場合:

```powershell
uv sync --extra dev --extra ocr
```

OCRmyPDFはPythonパッケージ以外にもTesseract等の外部依存を必要とするため、M4でWindows導入手順を固定する。

## 起動

```powershell
uv run pdf-workbench
```

## 品質確認

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src/pdf_workbench
uv run pytest --cov=pdf_workbench
```

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
- UI変更はWindowsで確認
- セキュリティ上の制約をREADMEまたはIssueへ記録
