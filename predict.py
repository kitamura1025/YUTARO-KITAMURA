"""レースの予想と、北村式3連単フォーメーションの評価を出す。

使い方:
  python3 predict.py 202606040911 --going 重
  python3 predict.py 202606040911 --going 重 --axis 15 --rivals 11 13 --others 14 3 16 10 9 6

--going  当日の馬場（良/稍/重/不）。重・不良なら道悪実績を加味する
--axis/--rivals/--others を渡すと、その買い目を評価する。省略時はモデルが自動で選ぶ
結果は predictions/<race_id>.md にも保存する。
"""
import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from keiba import betting, model, netkeiba


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("race_id")
    ap.add_argument("--going", default="")
    ap.add_argument("--axis", type=int)
    ap.add_argument("--rivals", type=int, nargs=2)
    ap.add_argument("--others", type=int, nargs="+")
    a = ap.parse_args()

    info, entries = netkeiba.race_card(a.race_id, refresh=True)
    win, updated = netkeiba.odds(a.race_id, 1)
    tri, _ = netkeiba.odds(a.race_id, 8)
    try:
        tr = netkeiba.training(a.race_id, refresh=True)
    except Exception:  # noqa: BLE001  調教ページがないレースもある
        tr = {}
    entries = [replace(e, odds=win.get(e.umaban, (None, None))[0], popularity=win.get(e.umaban, (None, None))[1],
                       training=tr.get(e.umaban, (None, ""))[0], training_note=tr.get(e.umaban, (None, ""))[1])
               for e in entries if e.umaban in win]  # 取消馬はオッズが出ない

    params = model.load_params()
    p, scores = model.win_probs(entries, info, params, a.going)
    mk = model.market_probs(entries)
    pp = betting.place_probs(p)

    out = [f"# {info['title'].split(' |')[0].replace(' 5走表示', '')}", "",
           f"- 作成: {datetime.now(timezone(timedelta(hours=9))):%Y-%m-%d %H:%M} JST / オッズ更新: {updated} / 想定馬場: {a.going or '指定なし'}",
           f"- モデル: 近走スコア {1 - params['market_blend']:.0%} + 市場オッズ {params['market_blend']:.0%}（params.json）", "",
           "## 各馬の評価", "",
           "| 馬番 | 馬名 | 単勝 | 市場勝率 | モデル勝率 | 2着内率 | 3着内率 | 近走スコア | 調教 |",
           "|---|---|---|---|---|---|---|---|---|"]
    for e in sorted(entries, key=lambda e: -pp[e.umaban][2]):
        k = e.umaban
        out.append(f"| {k} | {e.name} | {e.odds} | {mk.get(k, 0):.1%} | {p[k]:.1%} | {pp[k][1]:.1%} | {pp[k][2]:.1%} "
                   f"| {scores[k]:+.2f} | {e.training or '-'} {e.training_note} |")

    def section(title, axis, rivals, others):
        tickets = betting.formation(axis, rivals, others)
        ev = betting.evaluate(tickets, p, tri)
        name = {e.umaban: e.name for e in entries}
        lines = ["", f"## {title}", "",
                 f"軸 {axis} {name[axis]} / 対抗 {', '.join(f'{r} {name[r]}' for r in rivals)} / "
                 f"相手 {', '.join(map(str, others))}", "",
                 "| # | 1着 | 2着 | 3着 |", "|---|---|---|---|"]
        lines += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} |" for r in betting.formation_table(axis, rivals, others)]
        lines += ["", f"- {ev['points']}点 / {ev['cost']:,}円 / 的中確率 {ev['hit_prob']:.1%}"]
        if "roi" in ev:
            lines.append(f"- 期待回収率 {ev['roi']:.0%}（3連単オッズ {ev['min_odds']:.0f}〜{ev['max_odds']:.0f}倍）")
        return lines

    if a.axis:
        out += section("指定の買い目", a.axis, a.rivals, a.others)
    out += section("モデル自動選択", *betting.auto_select(p))
    out += ["", "## 期待回収率が高い組み合わせ（参考）", "",
            "Harvilleモデルは人気薄の組み合わせを高く見積もりがちなので、100%超えでも過信しないこと。", "", "| 期待回収率 | 的中確率 | 軸 | 対抗 | 相手 |", "|---|---|---|---|---|"]
    for roi, hit, axis, rivals, others in betting.search_best(p, tri):
        out.append(f"| {roi:.0%} | {hit:.1%} | {axis} | {rivals[0]},{rivals[1]} | {','.join(map(str, others))} |")

    text = "\n".join(out) + "\n"
    Path("predictions").mkdir(exist_ok=True)
    Path(f"predictions/{a.race_id}.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
