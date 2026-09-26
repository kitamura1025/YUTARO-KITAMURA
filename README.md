# 競馬予想ツール

netkeiba の公開データから各馬の勝率を推定し、3連単フォーメーション（軸1・対抗2・相手6の48点）の的中確率と期待回収率を出す。

```sh
# 予想（馬場を指定）
python3 predict.py 202606040911 --going 重

# 自分の買い目を評価
python3 predict.py 202606040911 --going 重 --axis 15 --rivals 11 13 --others 14 3 16 10 9 6

# 過去レースで検証・パラメータ調整
python3 backtest.py --fit
```

Python 3.10以上、標準ライブラリのみ。買い方のルールと運用方法は [CLAUDE.md](CLAUDE.md) を参照。

推定値はあくまで目安で、的中や利益を保証するものではありません。
