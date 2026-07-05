# 開発ロードマップ

## 開発目標

### M0: Bootstrap

- Python 3.12/3.13プロジェクト
- uvによる依存固定
- Ruff、mypy、pytest
- GitHub Actions
- PyInstallerのWindowsビルド
- 最小Qtアプリ

完了条件: CIが成功し、Windowsでアプリを起動できる。

### M1: Viewer

- PDFiumレンダリング
- 連続ページ表示
- サムネイル
- ページ移動
- ズーム
- 検索とコピー
- 大規模PDFの遅延読み込み

完了条件: 1000ページPDFを全ページ一括画像化せず閲覧できる。

### M2: Page operations

- 結合、分割、抽出
- 挿入、削除、複製、置換
- 並べ替え
- 回転、切り抜き
- Undo/Redo
- 安全保存

完了条件: 保存後のPDFがQPDF検査と再レンダリング検査を通る。

### M3: Markup

- ハイライト、下線、取り消し線
- 付箋、テキストボックス
- 図形、矢印、フリーハンド
- 透かし、ページ番号
- 署名画像

完了条件: Acrobat Reader、Edge、Chromeで標準注釈として表示できる。

### M4: OCR and redaction

- OCRmyPDF統合
- 日本語・英語
- 傾き補正、自動回転
- 手動・検索ベース墨消し
- メタデータ等のサニタイズ

完了条件: 墨消し対象が抽出テキスト、コンテンツストリーム、埋め込み画像に残らない。

### M5: Optimize and protect

- 画像圧縮
- 不要オブジェクト削除
- Web最適化
- AES暗号化
- パスワードと権限
- バッチ処理

完了条件: 処理結果と削減サイズを提示し、失敗時に原本を保持する。

### M6: Forms and limited editing

- AcroForm入力・保存・フラット化
- テキスト追加
- 短文置換
- 画像追加・移動・置換
- リンク編集

完了条件: 日本語を埋め込みフォントで追加でき、他の主要ビューアで表示できる。

### M7: Compare and release hardening

- テキスト比較
- ページ画像比較
- 視覚回帰テスト
- 破損PDF・暗号化PDFの検証
- Windows署名・インストーラー検討

完了条件: 日常利用版として再現可能なリリースを作成できる。

## PyInstaller方針

- 開発中と初期配布: `onedir`
- 安定後の任意配布: `onefile`
- Windowsランナー上でのみWindows EXEを作る
- EXEの隣へ設定を書かず、`%LOCALAPPDATA%`等へ保存する
- 外部OCR実行ファイルは初期段階では別途インストールとし、後に同梱方式を検討する
