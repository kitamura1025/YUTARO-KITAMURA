"""netkeiba の公開ページからデータを取得・解析する。

取得したHTML/JSONは data/cache/ に保存し、同じURLは再取得しない
（オッズなど変化するものは refresh=True で取り直す）。
"""
import html
import json
import re
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"
UA = "Mozilla/5.0 (keiba-research; personal use)"
RACE = "https://race.netkeiba.com"


def fetch(url, refresh=False, wait=1.0):
    CACHE.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9]+", "_", url.split("://", 1)[1])[:200]
    path = CACHE / key
    if path.exists() and not refresh:
        return path.read_text(encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8", errors="ignore")
    path.write_text(body, encoding="utf-8")
    time.sleep(wait)  # サイトに負荷をかけない
    return body


def _text(s):
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


GRADE = {"GI": "G1", "JpnI": "G1", "GII": "G2", "JpnII": "G2", "GIII": "G3", "JpnIII": "G3"}


@dataclass
class PastRun:
    race_id: str
    date: str
    place: str
    name: str
    grade: str  # G1/G2/G3/OP/L/3勝/2勝/1勝/...
    surface: str  # 芝/ダ/障
    distance: int
    outer: bool
    going: str  # 良/稍/重/不
    rank: int | None
    field_size: int | None
    number: int | None
    popularity: int | None
    jockey: str
    weight: float | None
    passage: list
    last3f: float | None
    diff: float | None  # 勝ち馬との差（勝った場合は2着との差をマイナスで）
    overseas: bool


@dataclass
class Entry:
    waku: int
    umaban: int
    name: str
    sex: str
    age: int
    jockey: str
    weight: float
    trainer: str
    style: str  # 逃/先/差/追/大(大逃げ)
    past: list = field(default_factory=list)
    odds: float | None = None
    popularity: int | None = None
    training: str | None = None  # A/B/C/D
    training_note: str = ""


def _parse_past_cell(td):
    rid = re.search(r'id="myhorse_(\d+)"', td)
    d01 = re.search(r'Data01">\s*<span>(.*?)</span>\s*<span class="Num">(.*?)</span>', td, re.S)
    if not (rid and d01):
        return None
    date_place = _text(d01.group(1)).split(" ")
    rank_s = _text(d01.group(2))
    d02 = re.search(r'Data02">(.*?)</div>', td, re.S)
    name = _text(re.sub(r"<span.*?</span>", "", d02.group(1), flags=re.S)) if d02 else ""
    g = re.search(r"Icon_GradeType\d*\">(.*?)<", td)
    grade = GRADE.get(g.group(1), g.group(1)) if g else ""
    d05 = _text(re.search(r'Data05">(.*?)</div>', td, re.S).group(1))
    m = re.match(r"(芝|ダ|障)(\d+)(\(外\))?.*?(良|稍|重|不)?$", d05)
    surface, dist, outer, going = (m.group(1), int(m.group(2)), bool(m.group(3)), m.group(4) or "") if m else ("", 0, False, "")
    if not going:
        gm = re.search(r"(良|稍|重|不)", d05)
        going = gm.group(1) if gm else ""
    d03 = _text(re.search(r'Data03">(.*?)</div>', td, re.S).group(1))
    m3 = re.match(r"(\d+)頭 (\d+)番 (\d+)人 (.*?) ([\d.]+)$", d03)
    d06 = _text(re.search(r'Data06">(.*?)</div>', td, re.S).group(1))
    passage = [int(x) for x in re.findall(r"\d+", d06.split(" ")[0])] if re.match(r"[\d-]+", d06) else []
    l3 = re.search(r"\(([\d.]+)\)", d06)
    d07 = re.search(r'Data07">.*?\(([-\d.]+)\)', td, re.S)
    diff = float(d07.group(1)) if d07 else None
    rid = rid.group(1)
    overseas = not rid[4:6].isdigit() or not (1 <= int(rid[4:6]) <= 10)
    if overseas and diff == 0.0:
        diff = None  # 海外レースは着差が入らない
    return PastRun(
        race_id=rid, date=date_place[0], place=date_place[1] if len(date_place) > 1 else "",
        name=name, grade=grade, surface=surface, distance=dist, outer=outer, going=going,
        rank=int(rank_s) if rank_s.isdigit() else None,
        field_size=int(m3.group(1)) if m3 else None, number=int(m3.group(2)) if m3 else None,
        popularity=int(m3.group(3)) if m3 else None, jockey=m3.group(4) if m3 else "",
        weight=float(m3.group(5)) if m3 else None, passage=passage,
        last3f=float(l3.group(1)) if l3 and float(l3.group(1)) > 0 else None,
        diff=diff, overseas=overseas,
    )


def race_card(race_id, refresh=False):
    """出馬表（5走表示）を解析して (レース情報, [Entry]) を返す。"""
    t = fetch(f"{RACE}/race/shutuba_past.html?race_id={race_id}", refresh=refresh)
    title = _text(re.search(r"<title>(.*?)</title>", t, re.S).group(1))
    rd = _text(re.search(r'RaceData01">(.*?)</div>', t, re.S).group(1))
    m = re.search(r"(芝|ダ)(\d+)m", rd)
    info = {"race_id": race_id, "title": title, "surface": m.group(1) if m else "",
            "distance": int(m.group(2)) if m else 0, "outer": "外" in rd}
    entries = []
    for row in re.findall(r'<tr class="HorseList.*?</tr>', t, re.S):
        tds = re.findall(r"<td class=\"(.*?)\"[^>]*>(.*?)</td>", row, re.S)
        if len(tds) < 5 or not tds[0][0].startswith("Waku"):
            continue
        info_td = row
        name = _text(re.search(r'Horse02">\s*<a[^>]*>(.*?)</a>', info_td, re.S).group(1))
        trainer = _text(re.search(r'Horse05">(.*?)</div>', info_td, re.S).group(1))
        style = re.search(r'kyakusitu">(.*?)<', info_td)
        barei = re.search(r'Barei">(.*?)<', row).group(1)
        jk = re.search(r'class="Jockey">.*?<a[^>]*>(.*?)</a>.*?<span>([\d.]+)</span>', row, re.S)
        past = [p for p in (_parse_past_cell(td) for td in re.findall(r'<td class="Past.*?</td>', row, re.S)) if p]
        entries.append(Entry(
            waku=int(_text(tds[0][1])), umaban=int(_text(tds[1][1])), name=name,
            sex=barei[0], age=int(re.search(r"\d+", barei).group(0)),
            jockey=_text(jk.group(1)) if jk else "", weight=float(jk.group(2)) if jk else 0.0,
            trainer=trainer, style=style.group(1) if style else "", past=past,
        ))
    return info, entries


def odds(race_id, kind=1, refresh=True):
    """kind=1 単勝 {馬番: (odds, 人気)} / kind=8 3連単 {(a,b,c): odds}"""
    t = fetch(f"{RACE}/api/api_get_jra_odds.html?race_id={race_id}&type={kind}&action=update", refresh=refresh)
    d = json.loads(t).get("data") or {}
    o = (d.get("odds") or {}).get(str(kind)) or {}
    out = {}
    for k, v in o.items():
        try:
            val = float(v[0])
        except ValueError:
            continue
        if kind == 1:
            out[int(k)] = (val, int(v[2]) if v[2].isdigit() else None)
        else:
            out[tuple(int(k[i:i + 2]) for i in range(0, len(k), 2))] = val
    return out, d.get("update_datetime", "")


def training(race_id, refresh=False):
    """調教の短評と評価（A〜D）。{馬番: (評価, 短評)}"""
    t = fetch(f"{RACE}/race/oikiri.html?race_id={race_id}", refresh=refresh)
    out = {}
    for row in re.findall(r'<tr class = "OikiriDataHead\d+ HorseList".*?</tr>', t, re.S):
        num = re.search(r'class="Umaban">(\d+)</td>', row)
        crit = re.search(r'Training_Critic">(.*?)</td>', row, re.S)
        rank = re.search(r'class="Rank_[^"]*">([A-D])</td>', row)
        if num and rank:
            out[int(num.group(1))] = (rank.group(1), _text(crit.group(1)) if crit else "")
    return out


def result(race_id):
    """確定結果。{'order': [(着順, 馬番, 単勝オッズ, 人気)], 'going': 馬場, 'trifecta': (組, 払戻)}"""
    t = fetch(f"{RACE}/race/result.html?race_id={race_id}")
    order = []
    for row in re.findall(r'<tr\s+class="(?:FirstDisplay )?HorseList"[^>]*>\s*<td class="Result_Num">.*?</tr>', t, re.S):
        rk = re.search(r'class="Rank">(.*?)<', row)
        num = re.findall(r'<td class="Num[^"]*">\s*<div>(\d+)</div>', row)
        od = re.search(r'<td class="Odds Txt_R">\s*<span[^>]*>([\d.]+)</span>', row)
        pp = re.search(r'OddsPeople">(\d+)<', row)
        if rk and len(num) >= 2:
            r = _text(rk.group(1))
            order.append((int(r) if r.isdigit() else None, int(num[1]),
                          float(od.group(1)) if od else None, int(pp.group(1)) if pp else None))
    rd = re.search(r'RaceData01">(.*?)</div>', t, re.S)
    gm = re.search(r"馬場:(良|稍|重|不)", _text(rd.group(1))) if rd else None
    tri = re.search(r'<tr class="Tan3">.*?<td class="Result">(.*?)</td>\s*<td class="Payout">(.*?)</td>', t, re.S)
    trifecta = None
    if tri:
        combo = tuple(int(x) for x in re.findall(r"<span>(\d+)</span>", tri.group(1)))
        pay = re.search(r"([\d,]+)円", _text(tri.group(2)))
        trifecta = (combo, int(pay.group(1).replace(",", "")) if pay else None)
    return {"order": order, "going": gm.group(1) if gm else "", "trifecta": trifecta}
