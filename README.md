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

Viewer core は、タブUI、連続ページ表示、遅延レンダリング、検索、テキスト選択、コピーまで実装済みです。
加えて、runtime command history、安全保存、セッション復旧、元ファイルの外部変更検知、selected-page rotation / duplication / deletion / reordering まで `main` に入っています。
page organizer では複数選択と drag-and-drop reordering を扱い、1 回の drag を 1 件の undoable command として working copy にだけ反映します。
さらに、別PDFから選択ページを1回の command として挿入する基盤を持ち、source page range には `all`、`1`、`1,3,5`、`2-6`、`1,3-5,8` のような軽量構文を使います。
同じ parser を使って、選択した target pages を別PDFの同数 source pages で 1:1 に置換する working-copy mutation も扱います。
selected pages の CropBox だけを数値指定で編集するトリミングも扱い、display-oriented margins を回転 0 / 90 / 180 / 270 に対応づけて working copy へ undoable command として反映します。
Issue #8 の最初の focused PR として、選択ページまたはページ範囲を別PDFへ抽出する非破壊 export も追加しています。

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
- `QFileSystemWatcher`、2秒のpolling fallback、アプリ再アクティブ時の再確認で、元PDFの変更・削除・再作成・読取不能を検知する
- 外部変更が見つかったタブは `[外部変更]` を表示し、persistent banner と `Save As` 強制で黙った上書きを防ぐ
- 保存時は `TargetSnapshot` を使って保存開始前と `os.replace()` 直前に保存先を再確認し、別プロセスの変更が見つかった場合は置換を中止する
- own save / save as 直後は監視baselineを更新し、アプリ自身の保存を false positive として扱わない
- network filesystem や watcher event が欠落する環境でも polling fallback で再確認するが、size と mtime を完全に偽装した変更や filesystem API レベルの完全な conditional replace までは保証しない

## ページ挿入

- 「別のPDFからページを挿入…」は page organizer の context menu と既存のページ操作 menu から実行する
- source page range は UI では 1-based で入力し、domain では ascending unique な 0-based tuple へ正規化する
- 挿入位置は reorder と同じ `insertion_slot` を使い、先頭 / 選択または現在ページの前 / 後 / 末尾へ挿入できる
- 実行時は source PDF の選択ページだけを frozen snapshot として working copy と同じ directory に保存し、redo では live source PDF を再読込しない
- 同時に target working copy の validated undo snapshot も保持し、undo はその snapshot を atomic に戻す
- imported pages の page boxes、effective rotation、supported annotations は取り込むが、source document の bookmarks、named destinations、metadata、attachments は統合しない
- target document 側の bookmarks、named destinations、metadata、attachments は保持し、page index だけ必要最小限 remap する
- execute / undo / redo の各経路で pikepdf reopen、PDFium render、構造検証が通った場合だけ `os.replace()` で working copy を置換する
- failure 時は source PDF と target working copy の両方を保全し、破損した candidate を commit しない

## ページ置換

- 「選択ページを別のPDFで置換…」は page organizer の context menu と既存のページ操作 menu から実行する
- target selection と source page range は同数でなければ確定できず、target page count と page order は変化しない
- source page range parser は挿入と共通で、`all`、single、list、range、mixed list/range を扱う
- 実行時は source PDF の frozen snapshot と target-before undo snapshot を working copy と同じ directory に保持し、redo では live source PDF を再読込しない
- replace candidate では target page object 自体は維持しつつ、selected page の contents、page boxes、rotation、resources、supported passive annotations だけを source page へ差し替える
- そのため target document の metadata、outlines、named destinations、attachments は保持しつつ、replaced page を指す bookmark / named destination も同じ page index に残る
- replaced page の旧 annotation は破棄し、source document の metadata、outlines、named destinations、attachments は統合しない
- execute / undo / redo の各経路で pikepdf reopen、PDFium render、構造検証が通った場合だけ `os.replace()` で working copy を置換する

## ページトリミング

- 「選択ページをトリミング…」は Edit menu と page organizer の context menu から実行する
- この操作は `/MediaBox`、`/Rotate`、content stream、annotation object、annotation `/Rect` を変えず、selected page の `/CropBox` だけを変更する
- トリミングは表示範囲だけを変える。範囲外の content や annotation は PDF から削除せず、CropBox によって隠れるだけとする
- 余白は point 単位の数値入力で指定し、表示上の左 / 上 / 右 / 下として解釈する
- 回転済みページでも display-oriented margin を raw PDF user space へ変換して適用し、non-zero origin の MediaBox / CropBox も扱う
- direct / inherited / MediaBox fallback の CropBox を区別し、execute では selected page に direct `/CropBox` を materialize し、undo では元の direct presence を復元する
- execute / undo / redo の各経路で reopen validation、PDFium render、構造検証が通った場合だけ `os.replace()` で working copy を置換する
- render cache は changed pages だけ fresh render とし、selection と current page は維持する
- drag overlay による対話型 crop 編集は未実装で、現時点では numeric dialog 方式のみ提供する

## ページ抽出

- 「選択ページを抽出…」は現在の page organizer selection を昇順・重複なしに正規化して別PDFへ書き出す
- 「ページ範囲を抽出…」は selection に依存せず、`1-3, 5, 8-10` のような 1-based range syntax を受け取り、domain では 0-based ascending unique tuple として扱う
- 抽出は現在の document、selection、current page、dirty state、command history を変更しない
- output PDF には選択ページの content stream、resources、page boxes、rotation、対応済みの安全な page annotations を保持する
- source document の metadata、bookmarks、outlines、named destinations、attachments は統合しない
- `/Widget` annotations、JavaScript/action、file attachment、media、cross-page dependency のような安全に独立PDFへできない構造は silent removal せず fail-closed で拒否する
- 保存先は検証済み一時PDFを同じ directory に作ってから `os.replace()` し、source PDF、working copy、既存 target は失敗時に維持する

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
