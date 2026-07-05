# AGENTS

- `uv` は使用しない
- 依存関係管理は `venv + pip` を使用する
- `pyproject.toml` を依存関係の正本とする
- Windows と macOS を検証対象にする
- Windows を正式配布対象にする
- PDF 原本を直接上書きしない
- PDF JavaScript を実行しない
- 重い処理を Qt UI スレッドで実行しない
- GUI、PDF レンダリング、PDF 書き換え、ドメインモデル、外部プロセスを分離する
- 型ヒントと `mypy --strict` を維持する
- 各変更にテストを追加する
