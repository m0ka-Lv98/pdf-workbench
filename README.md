# PDF Workbench

Windowsで個人利用することを主目的としつつ、macOSとWindowsの両方で開発できる、完全ローカル動作のPython製PDFデスクトップアプリです。
Acrobat Proの全機能再現ではなく、日常的に使う閲覧・ページ整理・注釈・OCR・墨消し・圧縮・フォーム入力を段階的に実装します。

## 方針

- クラウド、共同レビュー、署名依頼サービスは実装しない
- PDF JavaScriptは実行しない
- 原本を直接上書きせず、安全保存を標準にする
- PDF表示とPDF書き換えのエンジンを分離する
- Windows向け配布物はPyInstallerで作る
- 初期リリースは安定性を優先して`onedir`、`onefile`は実験的ターゲットとする

## 技術スタック

- GUI: PySide6
- PDF表示・文字位置取得: pypdfium2 / PDFium
- PDF構造操作・修復・最適化: pikepdf / QPDF
- ページ操作・フォーム: pypdf
- OCR: OCRmyPDF + Tesseract（後続フェーズ）
- Windows配布: PyInstaller
- 依存関係管理: venv + pip

## 現在の状態

PDFを開くタブUI、連続ページ表示、検索、選択表示に加えて、安全保存の基盤まで実装されています。
本格的なページ編集はIssueとロードマップに沿って後続PRで追加します。現在は安全保存に加えて、異常終了後のセッション復旧UIまで実装されています。

## 作業コピーと安全保存

- 開いたPDFはそのまま編集対象にせず、`platformdirs` が返すユーザーキャッシュ配下のセッションディレクトリへ `working.pdf` として複製して扱う
- 表示と今後の編集対象は常に作業コピー側を参照し、元ファイルは保存完了まで直接変更しない
- 保存と名前を付けて保存は、保存先と同じディレクトリに一時PDFを書き出してから検証する
- 検証では `pikepdf` による再オープン、ページ数一致、`pypdfium2` による再オープン、ページ数一致、先頭ページの低解像度レンダリング成功を確認する
- すべて成功した場合だけ `os.replace()` で atomic replace を行う
- POSIX では既存保存先の permission mode を可能な範囲で temp file へ引き継ぎ、replace 後に親 directory の fsync を best effort で行う
- 保存失敗時は既存の保存先ファイルを維持し、元のPDFも変更しない
- アプリの session workspace 配下や `working.pdf` 自体は永続保存先として選べない
- 各 session workspace には `session.json` と `session.lock` を置き、作業コピーの状態を atomic に保存する
- 起動時は前回の異常終了で残った workspace を scan し、復元、破棄、「後で」を選べる
- 元のPDFが消失または変更されていた場合は復元自体は許可しつつ、通常の上書き保存は行わず `Save As` を強制する
- metadata が壊れている候補や working PDF を検証できない候補も自動削除せず、復元不可候補として扱う
- 通常終了またはタブクローズ時はセッションごとの作業ディレクトリを削除する
- packaged smoke や診断用途では `--skip-recovery-prompt` を付けることで復旧ダイアログを抑止できる
- 元PDFの常時監視やリアルタイム外部変更検知は未実装で、Issue #6 の残件として継続する

## 開発環境

前提:

- macOS 14+ または Windows 10/11 x64
- Python 3.12または3.13
- Git
- Ubuntu は GUI 配布対象ではなく、移植性と単体テスト検証の対象

```bash
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

起動:

```bash
python -m pdf_workbench
```

PDFファイルを指定して起動できます。

```bash
python -m pdf_workbench /path/to/document.pdf
```

## テスト

```bash
ruff check .
ruff format --check .
mypy src/pdf_workbench
pytest --cov=pdf_workbench
```

## 設定とログ

- ログは `platformdirs` が返すユーザープロファイル配下のログディレクトリへ保存する
- Qt設定はレジストリではなく、ユーザープロファイル配下の設定ディレクトリへINIファイルとして保存する
- 実行ファイルの隣には設定やログを書き込まない

macOS と Windowsを開発・検証対象とし、Ubuntu は移植性と単体テスト検証の対象とします。最終配布ターゲットは Windows です。

## Windows実行ファイル

安定性確認用の`onedir`ビルド:

```bash
pyinstaller packaging/pdf_workbench_onedir.spec --noconfirm --clean
```

単一EXEの実験ビルド:

```bash
pyinstaller packaging/pdf_workbench_onefile.spec --noconfirm --clean
```

出力先は`dist/`です。GitHub Actionsの`Build Windows executable`からも生成できます。Windows EXEはWindowsランナー上でのみ生成します。

## GitHubリポジトリの初期化

GitHub CLIで認証済みのPowerShellまたはターミナルから実行します。

```powershell
.\scripts\bootstrap_github.ps1
```

このスクリプトは以下を行います。

1. `pdf-workbench`リポジトリを作成
2. ローカルコミットをpush
3. `docs/issues/`の計画Issueを登録

既定ではprivateリポジトリです。

## ドキュメント

- [開発ロードマップ](docs/ROADMAP.md)
- [アーキテクチャ](docs/ARCHITECTURE.md)
- [開発手順](docs/DEVELOPMENT.md)
- [機能スコープ](docs/SCOPE.md)

## ライセンス

このリポジトリの独自コードはMIT Licenseです。依存ライブラリと同梱バイナリには各プロジェクトのライセンスが適用されます。
