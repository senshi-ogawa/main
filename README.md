# 株取引練習アプリ

過去の好きな日から1日ずつ進めながら、買い・売り・何もしないを選んで練習するアプリです。
株価は yfinance（Yahoo Finance）から取得して、パソコンの中に保存します。練習中はネットにつながっていなくても使えます。

今はステップ1（株価データの取得）までできています。画面はステップ2で作ります。

## 起動のしかた

1. Python 3.9 以上を入れておきます（https://www.python.org/downloads/ ）。Windows では「Add python.exe to PATH」にチェックを入れてください。
2. このリポジトリを `git clone` するか、ZIP でダウンロードして展開します。
3. Mac は `run.command`、Windows は `run.bat` をダブルクリックします。

初回は必要な部品を自動で入れるので数分かかります。git で入れた場合は、起動のたびに最新版を取り込みます。

## 株価データ

起動すると、JPX の上場銘柄一覧からプライムとスタンダードの銘柄（約3,200）と主な為替ペアを取り出し、日足を取れるだけ過去までさかのぼって保存します。
初回は全銘柄で1時間ほどかかります。2回目からは新しい日の分だけを追加します。株式分割などで過去の株価が調整し直された銘柄は、その銘柄だけ全期間を取り直します。

途中で止めても、次に起動したときに続きから取得します。

保存場所（数百MBになります）:

- Mac: `~/Library/Application Support/KabuTrainer/`
- Windows: `%LOCALAPPDATA%\KabuTrainer\`

### 手動で動かす場合

```
python -m kabutrainer update --limit 20              # 先頭の20銘柄だけ試す
python -m kabutrainer update --symbols 7203.T,USDJPY=X
python -m kabutrainer status                         # 保存済みデータの状況
```

## テスト

```
python -m unittest discover -s tests
```
