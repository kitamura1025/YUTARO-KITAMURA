"""レース予想レポートを作る。

使い方:
  python3 predict.py 202606040911 --going 重
  python3 predict.py 202606040911 --going 重 --axis 15 --rivals 11 13 --others 14 3 16 10 9 6
  python3 predict.py 202606040911 --going 重 --quick   # 相手関係の詳細（時間がかかる）を省く

--going   当日の馬場（良/稍/重/不）。未指定なら良として計算
--axis/--rivals/--others  自分の買い目を評価する。省略時はモデルが自動で選ぶ
レポートは predictions/<race_id>.md に保存する。
"""
import argparse
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from keiba import betting, features, model, netkeiba, stats

LABEL = {"speed_best": "ベスト指数", "speed_avg": "平均指数", "level": "相手関係", "course_fit": "コース適性",
         "going_fit": "馬場適性", "front": "位置取り", "pace_fit": "展開", "layoff": "レース間隔",
         "weight_diff": "斤量増減", "age": "年齢", "inner": "内枠", "experience": "条件経験", "market": "人気",
         "jockey": "騎手", "trainer": "調教師", "jockey_trainer": "騎手×調教師", "jockey_course": "騎手×コース",
         "sire": "父の血統", "damsire": "母父の血統", "track_fit": "開催傾向"}


def short(name):
    return re.sub(r"\(.*?\)", "", name)[:12]


def fmt(x, nd=2, sign=True):
    return "-" if x is None else (f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}")


def opponents_note(run, before, limit=4):
    """その走で負かした相手・負けた相手の、その後の成績（上位の着順の馬のみ）"""
    try:
        _, members = netkeiba.race_members(run.race_id)
    except Exception:  # noqa: BLE001
        return []
    d0 = features.parse_date(run.date)
    notes = []
    for m in sorted(members, key=lambda m: m["rank"] or 99):
        if m["umaban"] == run.umaban or not m["rank"] or m["rank"] > max(3, (run.rank or 0) + 2):
            continue
        after = [r for r in features.history(m["horse_id"]) if d0 < features.parse_date(r.date) < before]
        if not after:
            continue
        good = [r for r in after if r.rank and r.rank <= 3]
        best = min(after, key=lambda r: (r.rank or 99))
        rel = "負けた" if (run.rank or 99) > m["rank"] else "負かした"
        notes.append(f"{rel}{m['name']}({m['rank']}着)→その後{len(after)}戦{len(good)}回3着内"
                     f"（最高: {best.name[:12]} {best.rank}着）")
        if len(notes) >= limit:
            break
    return notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("race_id")
    ap.add_argument("--going", default="良")
    ap.add_argument("--axis", type=int)
    ap.add_argument("--rivals", type=int, nargs=2)
    ap.add_argument("--others", type=int, nargs="+")
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()

    info, entries = netkeiba.race_card(a.race_id, refresh=True)
    dm = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", info["title"])
    before = features.date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
    place = next((k for k, v in netkeiba.PLACES.items() if a.race_id[4:6] == v), "")
    target = dict(info, place=place, going=a.going[:1])
    win, updated = netkeiba.odds(a.race_id, 1)
    tri, _ = netkeiba.odds(a.race_id, 8)
    try:
        tr = netkeiba.training(a.race_id, refresh=True)
    except Exception:  # noqa: BLE001
        tr = {}
    entries = [replace(e, odds=win[e.umaban][0], popularity=win[e.umaban][1],
                       training=tr.get(e.umaban, (None, ""))[0], training_note=tr.get(e.umaban, (None, ""))[1])
               for e in entries if e.umaban in win]  # 取消馬はオッズが出ない
    for e in entries:
        features.history(e.horse_id, max_age_days=0.5)  # 出走馬は最新の成績を取り直す

    total = sum(1 / e.odds for e in entries)
    early = {e.umaban: features.early_position([r for r in features.history(e.horse_id)
                                                if features.parse_date(r.date) < before]) for e in entries}
    pace_idx, pace_label = features.pace_forecast(early)
    bias = stats.meet_bias(a.race_id, info["surface"], a.going[:1], before)
    params = model.load_params()
    feats, details = {}, {}
    for e in entries:
        feats[e.umaban], details[e.umaban] = features.horse_features(
            e, target, before, pace_idx, (1 / e.odds) / total, bias)
    p = model.probs(feats, params)
    pp = betting.place_probs(p)
    name = {e.umaban: e.name for e in entries}
    order = sorted(entries, key=lambda e: -pp[e.umaban][2])

    now = datetime.now(timezone(timedelta(hours=9)))
    out = [f"# {info['title'].split(' |')[0].replace(' 5走表示', '')}", "",
           f"- {place} {info['surface']}{info['distance']}m / 想定馬場: {a.going} / 作成 {now:%Y-%m-%d %H:%M} JST / オッズ {updated}",
           "", "## 展開予想", "",
           f"- ペース予想: **{pace_label}**（ペース指数 {pace_idx:+.2f}）",
           "- 予想隊列（前走までの位置取り傾向順）: " + " → ".join(
               f"{k}{name[k]}" for k, _ in sorted(((k, v) for k, v in early.items() if v is not None), key=lambda x: x[1])),
           "", "## 開催中の馬場傾向（同じ芝ダ・同じ馬場状態のレースだけ）", "",
           f"- {bias.text()}",
           f"- 使ったレース: {', '.join(bias.used) or 'なし'}",
           f"- 馬場状態が違うので使わなかったレース: {', '.join(bias.skipped) or 'なし'}",
           "- 前有利度は+なら先行馬、内有利度は+なら内枠の馬が3着内に多い。"
           f"{stats.RULES['meet'][1]}R未満は参考外（回数が少ないデータは使わない）",
           "", "## 総合評価", "",
           "| 馬番 | 馬名 | 単勝 | 勝率 | 2着内 | 3着内 | ベスト指数 | 相手関係 | コース | 馬場 | 位置 | 調教 | 押し上げ要因 | 割引要因 |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for e in order:
        k, f = e.umaban, feats[e.umaban]
        c = model.contributions(f, params)
        c.pop("market", None)
        ups = [LABEL[x] for x, v in sorted(c.items(), key=lambda x: -x[1]) if v > 0.05][:3]
        downs = [LABEL[x] for x, v in sorted(c.items(), key=lambda x: x[1]) if v < -0.05][:3]
        out.append(f"| {k} | {e.name} | {e.odds} | {p[k]:.1%} | {pp[k][1]:.1%} | {pp[k][2]:.1%} | {fmt(f['speed_best'])} "
                   f"| {fmt(f['level'])} | {fmt(f['course_fit'])} | {fmt(f['going_fit'])} | {fmt(f['front'])} "
                   f"| {e.training or '-'} | {'・'.join(ups) or '-'} | {'・'.join(downs) or '-'} |")
    out += ["", "指数は1000mあたり基準タイムより何秒速いか（+ほど優秀）。相手関係は勝ち馬のその後の指数から見た実力。",
            f"コース・馬場は自身の平均との差（{features.APT_MIN_N}走未満は「-」）、位置は+ほど前に行く。"]

    out += ["", "## 各馬の近走（今回への参考度つき）"]
    for e in order:
        d = details[e.umaban]
        out += ["", f"### {e.umaban} {e.name}（{e.sex}{e.age} {e.jockey} {e.weight}kg / 単勝{e.odds}）",
                f"- 同コース距離帯 {d['course_n']}走 / 同じ馬場状態 {d['going_n']}走 / 位置取り傾向 "
                f"{'-' if d['early'] is None else ('逃げ・先行' if d['early'] < 0.25 else '中団' if d['early'] < 0.6 else '後方')}"
                + (f" / 調教 {e.training} {e.training_note}" if e.training else ""),
                f"- 血統: 父 {d['ped'].get('sire', '-')} / 母父 {d['ped'].get('damsire', '-')}",
                "- 人気比の3着内率（+なら人気以上に好走。回数が少ないものは参考外）: "
                + " / ".join(f"{LABEL[k]} {st.text()}" for k, st in d["conn"].items()), "",
                "| 日付 | レース | 条件 | 着順 | タイム(着差) | 指数 | ペース(前-後3F) | 通過 | 上り | 相手の指数 | 参考度 |",
                "|---|---|---|---|---|---|---|---|---|---|---|"]
        for x in d["runs"][:5]:
            r = x["run"]
            pace = f"{r.pace[0]}-{r.pace[1]}" if r.pace else "-"
            t = f"{int(r.time // 60)}:{r.time % 60:04.1f}" if r.time else "-"
            out.append(f"| {r.date[2:]} | {short(r.name)} | {r.place}{r.surface}{r.distance} {r.going} | {r.rank}/{r.field_size} "
                       f"| {t}({fmt(r.diff, 1)}) | {fmt(x['value'])} | {pace} | {'-'.join(map(str, r.passage))} "
                       f"| {r.last3f or '-'} | {fmt(x['level'])} | {x['relevance']:.0%} |")
        if not a.quick:
            notes = []
            for x in d["runs"][:3]:
                if x["run"].jra:
                    notes += [f"{short(x['run'].name)}: " + n for n in opponents_note(x["run"], before, 2)]
            if notes:
                out += ["", "相手関係:"] + [f"- {n}" for n in notes]

    def section(title, axis, rivals, others):
        ev = betting.evaluate(betting.formation(axis, rivals, others), p, tri)
        lines = ["", f"## {title}", "",
                 f"軸 {axis} {name[axis]} / 対抗 {', '.join(f'{r} {name[r]}' for r in rivals)} / 相手 {', '.join(map(str, others))}",
                 "", "| # | 1着 | 2着 | 3着 |", "|---|---|---|---|"]
        lines += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} |" for r in betting.formation_table(axis, rivals, others)]
        lines += ["", f"- {ev['points']}点 / {ev['cost']:,}円 / 的中確率 {ev['hit_prob']:.1%}"]
        if "roi" in ev:
            lines.append(f"- 期待回収率 {ev['roi']:.0%}（3連単オッズ {ev['min_odds']:.0f}〜{ev['max_odds']:.0f}倍）")
        return lines

    out += ["", "# 買い目（北村式 3連単フォーメーション 48点）"]
    if a.axis:
        out += section("指定の買い目", a.axis, a.rivals, a.others)
    out += section("モデル自動選択", *betting.auto_select(p))
    out += ["", "的中確率・期待回収率はモデルの推定値で、保証ではありません。"]

    text = "\n".join(out) + "\n"
    Path("predictions").mkdir(exist_ok=True)
    Path(f"predictions/{a.race_id}.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
