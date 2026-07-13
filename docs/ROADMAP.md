# 開発ロードマップ

## M0A: Bootstrap and development environment — complete

- Python 3.12 / 3.13
- `venv + pip`
- Ruff
- mypy
- pytest
- macOS / Windows / Ubuntu の cross-platform CI
- PySide6 application bootstrap
- user-profile settings and logs

完了条件: CI が成功し、開発環境を macOS と Windows で再現できる。

## M0B: Packaging automation — partial, non-blocking

- Windows `onedir`
- experimental Windows `onefile`
- macOS arm64 artifact
- packaged smoke tests
- Windows packaged GUI validation は #20 で継続
- macOS x86_64 は #36 で追跡
- signing、notarization、installer、physical-device validation は後続
- 現時点では #9、#19、#7、#8 をブロックしない

完了条件: 自動 packaging hardening を継続しつつ、次の実装順序を止めない。

## M1: Viewer core — complete

- tabbed document shell
- PDFium rendering
- continuous-page view
- lazy rendering
- render cache
- page navigation
- zoom
- search
- text selection
- copy
- coordinate mapping
- light / dark application UI

完了条件: 1000 ページ級 PDF を全ページ一括ラスタライズせず閲覧でき、検索・選択・コピーまで行える。

## M2A: Safe document lifecycle — complete

- working copy
- atomic Save / Save As
- reopen and render validation
- unsaved-change close guard
- recovery metadata
- startup recovery
- source-file external-change detection
- save-target race checks

Issue #6 は完了済み。

## M2B: Command architecture and Undo/Redo — next

詳細は #9 を参照。

## Cross-cutting: Compatibility and regression corpus — starts before page writing

詳細は #19 を参照。#19 Phase A は、#7 の破壊的ページ操作を merge する前に必要だが、#19 全体の完了は前提にしない。

## M2C: Page organizer and core page operations

詳細は #7 を参照。

## M2D: Merge, split, extract, and image-to-PDF

詳細は #8 を参照。

## M3: Markup

- ハイライト、下線、取り消し線
- 付箋、テキストボックス
- 図形、矢印、フリーハンド
- 透かし、ページ番号
- 署名画像

完了条件: 標準的な PDF 注釈として主要ビューアで表示できる。

## M4: OCR and redaction

- OCRmyPDF 統合
- 日本語・英語
- 傾き補正、自動回転
- 手動・検索ベース墨消し
- メタデータ等のサニタイズ

完了条件: 墨消し対象が抽出テキスト、コンテンツストリーム、埋め込み画像に残らない。

## M5: Optimize and protect

- 画像圧縮
- 不要オブジェクト削除
- Web 最適化
- AES 暗号化
- パスワードと権限
- バッチ処理

完了条件: 処理結果と削減サイズを提示し、失敗時に原本を保持する。

## M6: Forms and limited editing

- AcroForm 入力・保存・フラット化
- テキスト追加
- 短文置換
- 画像追加・移動・置換
- リンク編集

完了条件: 日本語を埋め込みフォントで追加でき、他の主要ビューアで表示できる。

## M7: Compare and release hardening

- テキスト比較
- ページ画像比較
- release packaging hardening
- 破損 PDF・暗号化 PDF の検証
- Windows 署名・インストーラー検討

視覚回帰と互換性テストは M7 だけに閉じず、#19 の横断活動として前倒しで拡張する。

完了条件: 日常利用版として再現可能なリリースを作成できる。

## Current implementation order

1. #9 Command architecture and Undo/Redo
2. #19 Phase A compatibility corpus
3. #7 Page organizer and core page operations
4. #8 Merge, split, extract, and image-to-PDF
5. #10 以降の markup、OCR、redaction、optimization、forms 等
6. #20 と #36 は初期リリース前の packaging hardening として並行または後続

## PyInstaller 方針

- 開発中と初期配布: `onedir`
- 実験的配布: `onefile`
- Windows ランナー上でのみ Windows EXE を作る
- 実行ファイルの隣へ設定を書かず、ユーザープロファイル配下へ保存する
- 外部 OCR 実行ファイルは初期段階では別途インストールとし、後に同梱方式を検討する
