"""買い目と結果を記録し、北村さんの選択とモデルの選択の成績を比べる。

使い方:
  python3 record.py add 202606040911 --axis 15 --rivals 11 13 --others 14 3 16 10 9 6 --memo "重馬場想定"
  python3 record.py settle            # 結果が出たレースの的中・払戻を記録
  python3 record.py summary           # これまでの成績（北村さん vs モデル）

記録は predictions/log.json（git管理）。
"""
import argparse
import json
import re
from pathlib import Path

from keiba import betting, netkeiba

LOG = Path(__file__).resolve().parent / "predictions" / "log.json"


def load():
    return json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() else []


def save(rows):
    LOG.parent.mkdir(exist_ok=True)
    LOG.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def model_pick(race_id):
    """predictions/<race_id>.md の「モデル自動選択」から軸・対抗・相手を読む"""
    path = LOG.parent / f"{race_id}.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    sec = text.split("## モデル自動選択", 1)
    if len(sec) < 2:
        return None
    line = next(l for l in sec[1].splitlines() if l.startswith("軸 "))
    axis = int(re.search(r"軸 (\d+)", line).group(1))
    rivals = [int(x) for x in re.findall(r"(\d+) \S+", line.split("/ 対抗", 1)[1].split("/ 相手")[0])]
    others = [int(x) for x in line.split("/ 相手", 1)[1].replace(",", " ").split()]
    return {"axis": axis, "rivals": rivals[:2], "others": others}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("race_id")
    add.add_argument("--axis", type=int, required=True)
    add.add_argument("--rivals", type=int, nargs=2, required=True)
    add.add_argument("--others", type=int, nargs="+", required=True)
    add.add_argument("--memo", default="")
    sub.add_parser("settle")
    sub.add_parser("summary")
    a = ap.parse_args()
    rows = load()

    if a.cmd == "add":
        rows = [r for r in rows if r["race_id"] != a.race_id]
        rows.append({"race_id": a.race_id, "mine": {"axis": a.axis, "rivals": a.rivals, "others": a.others},
                     "model": model_pick(a.race_id), "memo": a.memo, "result": None})
        save(rows)
        print("記録しました:", a.race_id)
    elif a.cmd == "settle":
        for r in rows:
            if r["result"]:
                continue
            res = netkeiba.result(r["race_id"])
            if not res["trifecta"]:
                print("未確定:", r["race_id"])
                continue
            combo, pay = res["trifecta"]
            r["result"] = {"top3": list(combo), "payout": pay, "going": res["going"]}
            for who in ("mine", "model"):
                pick = r.get(who)
                if pick:
                    t = betting.formation(pick["axis"], pick["rivals"], pick["others"])
                    pick["hit"] = tuple(combo) in t
                    pick["return"] = pay if pick["hit"] else 0
                    pick["cost"] = len(t) * 100
            print(r["race_id"], combo, f"{pay:,}円", "北村:", "的中" if r["mine"]["hit"] else "外れ",
                  "/ モデル:", "的中" if (r["model"] or {}).get("hit") else "外れ")
        save(rows)
    else:
        done = [r for r in rows if r["result"]]
        for who, label in (("mine", "北村さん"), ("model", "モデル")):
            ps = [r[who] for r in done if r.get(who) and "hit" in r[who]]
            if ps:
                cost = sum(p["cost"] for p in ps)
                ret = sum(p["return"] for p in ps)
                print(f"{label}: {len(ps)}R 的中{sum(p['hit'] for p in ps)} 回収率 {ret / cost:.0%} "
                      f"(投資{cost:,}円 払戻{ret:,}円)")
        print(f"未確定 {len(rows) - len(done)}R")


if __name__ == "__main__":
    main()
