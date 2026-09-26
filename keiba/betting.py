"""北村さんの3連単フォーメーション（軸1・対抗2・相手6）の組み立てと評価。

ルール（CLAUDE.md 参照）:
  - 軸は1着か2着が必須
  - 対抗2頭のうちどちらか1頭が3着以内
  - 残りの1枠に相手6頭のどれか1頭
  → 軸・対抗・対抗だけの決着は買わない（48点）

  ① 軸 → 対抗 → 相手
  ② 軸 → 相手 → 対抗
  ③ 対抗 → 軸 → 相手
  ④ 相手 → 軸 → 対抗
"""
import itertools


def formation(axis, rivals, others):
    t = []
    for r in rivals:
        for o in others:
            t += [(axis, r, o), (axis, o, r), (r, axis, o), (o, axis, r)]
    return t


def formation_table(axis, rivals, others):
    rv, ot = ",".join(map(str, rivals)), ",".join(map(str, others))
    return [("①", str(axis), rv, ot), ("②", str(axis), ot, rv), ("③", rv, str(axis), ot), ("④", ot, str(axis), rv)]


def trifecta_prob(p, a, b, c):
    """Harvilleモデル: 勝率から着順の確率を出す。"""
    pa, pb, pc = p.get(a, 0), p.get(b, 0), p.get(c, 0)
    if pa >= 1 or pa + pb >= 1:
        return 0.0
    return pa * pb / (1 - pa) * pc / (1 - pa - pb)


def place_probs(p):
    """{馬番: (1着率, 2着以内率, 3着以内率)}"""
    top1 = dict(p)
    top2 = {k: 0.0 for k in p}
    top3 = {k: 0.0 for k in p}
    for a, b, c in itertools.permutations(p, 3):
        pr = trifecta_prob(p, a, b, c)
        top3[a] += pr
        top3[b] += pr
        top3[c] += pr
        top2[a] += pr
        top2[b] += pr
    return {k: (top1[k], top2[k], top3[k]) for k in p}


def evaluate(tickets, p, tri_odds=None, stake=100):
    hit = sum(trifecta_prob(p, *t) for t in tickets)
    out = {"points": len(tickets), "cost": len(tickets) * stake, "hit_prob": hit}
    if tri_odds:
        ev = sum(trifecta_prob(p, *t) * tri_odds.get(t, 0) * stake for t in tickets)
        out["expected_return"] = ev
        out["roi"] = ev / (len(tickets) * stake)
        hits = sorted(((tri_odds.get(t, 0), t) for t in tickets), reverse=True)
        out["max_odds"], out["min_odds"] = hits[0][0], min(o for o, _ in hits if o)
    return out


def auto_select(p, n_others=6):
    """モデルの確率から 軸・対抗・相手 を自動で選ぶ。
    軸 = 2着以内率が最も高い馬 / 対抗 = 残りで3着以内率の上位2頭 / 相手 = その次の6頭"""
    pp = place_probs(p)
    axis = max(pp, key=lambda k: pp[k][1])
    rest = sorted((k for k in pp if k != axis), key=lambda k: -pp[k][2])
    return axis, rest[:2], rest[2:2 + n_others]


def search_best(p, tri_odds, n_others=6, top=5, pool=10):
    """期待回収率が高い 軸・対抗・相手 の組み合わせを探す（上位 pool 頭から）。"""
    pp = place_probs(p)
    cand = sorted(pp, key=lambda k: -pp[k][2])[:pool]
    results = []
    for axis in cand[:6]:
        for rivals in itertools.combinations([c for c in cand if c != axis], 2):
            rest = [c for c in sorted(pp, key=lambda k: -pp[k][2]) if c != axis and c not in rivals]
            others = rest[:n_others]
            ev = evaluate(formation(axis, rivals, others), p, tri_odds)
            results.append((ev.get("roi", 0), ev["hit_prob"], axis, rivals, others))
    results.sort(reverse=True)
    return results[:top]
