"""当日の新しい情報（オッズ・馬体重・馬場発表・その日のレース結果）で予想を更新し、変わった点だけ出す。

使い方:
  python3 update_today.py 202605040308 202605040309 ...   # 指定したレースを更新

買い・見送りの判定: モデルの的中確率が BUY_LINE 以上なら「買い」
（2,016レースの検証で、的中確率15%以上のレースは学習期間・確認期間とも回収率が高かった）
"""
import re
import subprocess
import sys
from pathlib import Path

from keiba import netkeiba

BUY_LINE = 15.0
WEIGHT_ALERT = 10  # 馬体重がこれ以上増減した馬を知らせる


def model_pick(md):
    sec = md.split("## モデル自動選択", 1)
    if len(sec) < 2:
        return None, None
    line = next((l for l in sec[1].splitlines() if l.startswith("軸 ")), None)
    hit = re.search(r"的中確率 ([\d.]+)%", sec[1])
    return line, float(hit.group(1)) if hit else None


def pick_sets(line):
    """軸・対抗・相手を集合で比べる（相手の並び順だけの違いは変更扱いしない）"""
    if not line:
        return None
    nums = lambda part: frozenset(int(x) for x in re.findall(r"\b(\d+)\b(?! ?頭)", part))  # noqa: E731
    axis = re.search(r"軸 (\d+)", line).group(1)
    rivals = re.findall(r"(\d+) \S+", line.split("/ 対抗", 1)[1].split("/ 相手")[0])
    others = line.split("/ 相手", 1)[1]
    return axis, frozenset(rivals), nums(others)


def going_of(race_id):
    t = netkeiba.fetch(f"{netkeiba.RACE}/race/shutuba.html?race_id={race_id}", refresh=True)
    rd = netkeiba._text(re.search(r'RaceData01">(.*?)</div>', t, re.S).group(1))
    g = re.search(r"馬場:(良|稍|重|不)", rd)
    weights = []
    for row in re.findall(r'<tr class="HorseList.*?</tr>', t, re.S):
        num = re.findall(r'<td class="Umaban\d*[^"]*"[^>]*>\s*(\d+)\s*<', row)
        name = re.search(r'HorseName.*?title="([^"]+)"', row, re.S)
        w = re.search(r'<td class="Weight"[^>]*>(.*?)</td>', row, re.S)
        diff = re.search(r"\(([+-]?\d+)\)", netkeiba._text(w.group(1))) if w else None
        if num and diff and abs(int(diff.group(1))) >= WEIGHT_ALERT:
            weights.append(f"{num[0]}{name.group(1) if name else ''} {netkeiba._text(w.group(1))}")
        if num and ("Cancel" in row or "取消" in row or "除外" in row):
            weights.append(f"{num[0]}{name.group(1) if name else ''} 取消・除外")
    return (g.group(1) if g else "良"), weights


def main():
    for rid in sys.argv[1:]:
        path = Path(f"predictions/{rid}.md")
        old_line, old_hit = model_pick(path.read_text(encoding="utf-8")) if path.exists() else (None, None)
        going, alerts = going_of(rid)
        for attempt in range(2):  # 取得の一時的な失敗に備えて1回だけやり直す
            res = subprocess.run([sys.executable, "predict.py", rid, "--going", going, "--quick"],
                                 capture_output=True, text=True, timeout=1200)
            if res.returncode == 0:
                break
        if res.returncode != 0:
            print(f"### {rid} 更新に失敗（前回の予想のまま）: {res.stderr.strip().splitlines()[-1:]}\n", flush=True)
            continue
        md = path.read_text(encoding="utf-8")
        line, hit = model_pick(md)
        title = md.splitlines()[0][2:]
        updated = re.search(r"オッズ (\S+ \S+)", md)
        verdict = "買い" if hit is not None and hit >= BUY_LINE else "見送り"
        old_verdict = None if old_hit is None else ("買い" if old_hit >= BUY_LINE else "見送り")
        changes = []
        if old_verdict and old_verdict != verdict:
            changes.append(f"判定が {old_verdict}→{verdict}")
        if old_line and pick_sets(old_line) != pick_sets(line):
            changes.append(f"買い目が変更（前: {old_line}）")
        print(f"### {rid[4:6]=='05' and '東京' or '京都'}{int(rid[-2:])}R {title}（馬場 {going} / オッズ {updated.group(1) if updated else '-'}）")
        print(f"- 判定: **{verdict}**（的中確率 {hit}%）" + ("" if old_hit is None else f" ← 前回 {old_hit}%"))
        print(f"- {line}")
        print("- 変更: " + (" / ".join(changes) if changes else "なし"))
        if alerts:
            print("- 注意: " + " / ".join(alerts))
        if verdict == "買い":
            table = md.split("## モデル自動選択", 1)[1]
            print("\n".join(l for l in table.splitlines() if l.startswith("|")))
        print(flush=True)


if __name__ == "__main__":
    main()
