#!/usr/bin/env python3
"""자동 분류 평가: fixture_prompts.tsv의 정답 작업과 haiku 분류 결과를 비교.

실제 사용처럼 두 번에 나눠 분류한다 (s1~s4 먼저, 다음 날 s5를 캐시 위에 추가).
결과: 콘솔 점수 + tests/grouping_result.html
사용: python3 tests/eval_grouping.py
"""
import html
import json
import os
import sys
import tempfile
from collections import Counter, OrderedDict
from itertools import combinations

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import tracker  # noqa: E402


def load_fixture():
    rows, truth = [], {}
    with open(os.path.join(HERE, "fixture_prompts.tsv")) as f:
        lines = [l.rstrip("\n").split("\t") for l in f if l.strip() and not l.startswith("#")]
    turn = {}
    for i, (session, branch, task, prompt) in enumerate(lines):
        day = 8 if session != "s5" else 9
        n = turn[session] = turn.get(session, 0) + 1  # 세션 안에서 턴이 쌓일수록 cache read 증가
        write = 30000 if n == 1 else 2000 + (i * 977) % 9000
        r = {"ts": "2026-11-%02dT%02d:%02d:00" % (day, 9 + i // 60, i % 60), "session": session,
             "branch": branch, "feature": "(none)", "purpose": prompt,
             "model": "claude-sonnet-5-5" if session == "s3" else "claude-opus-5-5",  # 가짜 토큰, 실제 가격
             "input_tokens": 3, "output_tokens": 200 + (i * 37) % 2500,
             "cache_creation_input_tokens": write, "cache_write_1h_tokens": write,
             "cache_read_input_tokens": 25000 + n * 12000 * (1 + i % 3)}
        r["total"] = sum(r[k] for k in tracker.KEYS)
        rows.append(r)
        truth[tracker.row_key(r)] = task
    return rows, truth


def pair_f1(pred, truth):
    keys = list(truth)
    tp = fp = fn = 0
    for a, b in combinations(keys, 2):
        same_p, same_t = pred[a] == pred[b], truth[a] == truth[b]
        tp += same_p and same_t
        fp += same_p and not same_t
        fn += same_t and not same_p
    p, r = tp / max(tp + fp, 1), tp / max(tp + fn, 1)
    return p, r, 2 * p * r / max(p + r, 1e-9)


def run_once(rows, truth):
    d = tempfile.mkdtemp()
    split = next(i for i, r in enumerate(rows) if r["session"] == "s5")
    tracker.load_groups(d, rows[:split])       # 1일차 report
    pred = tracker.load_groups(d, rows)         # 2일차 report: 새 프롬프트만 분류

    by_pred = OrderedDict()
    for r in rows:
        by_pred.setdefault(pred[tracker.row_key(r)], []).append(r)
    majority = {g: Counter(truth[tracker.row_key(r)] for r in rs).most_common(1)[0][0]
                for g, rs in by_pred.items()}
    wrong = {tracker.row_key(r) for g, rs in by_pred.items() for r in rs
             if truth[tracker.row_key(r)] != majority[g]}
    by_truth = OrderedDict()
    for r in rows:
        by_truth.setdefault(truth[tracker.row_key(r)], Counter())[pred[tracker.row_key(r)]] += 1
    p, rc, f1 = pair_f1(pred, truth)
    acc = 1 - len(wrong) / len(rows)
    return dict(pred=pred, by_pred=by_pred, majority=majority, wrong=wrong, by_truth=by_truth,
                acc=acc, p=p, rc=rc, f1=f1)


def main():
    """사용: python3 tests/eval_grouping.py [실행 횟수=1]  (여러 번 돌려 분류 일관성 확인)"""
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    rows, truth = load_fixture()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(runs) as ex:
        results = list(ex.map(lambda _: run_once(rows, truth), range(runs)))
    for n, r in enumerate(results, 1):
        print("[{}] 작업 {:>2}개 · 정확도 {:.0%} · precision {:.0%} recall {:.0%} F1 {:.0%}".format(
            n, len(r["by_pred"]), r["acc"], r["p"], r["rc"], r["f1"]))
    if runs > 1:
        avg = lambda k: sum(r[k] for r in results) / runs
        print("평균: 작업 {:.1f}개 (정답 {}개) · 정확도 {:.0%} · recall {:.0%} · F1 {:.0%}".format(
            sum(len(r["by_pred"]) for r in results) / runs, len(results[0]["by_truth"]),
            avg("acc"), avg("rc"), avg("f1")))
    r = results[-1]
    print("\n마지막 실행의 정답 작업 → 분류:")
    for t, c in r["by_truth"].items():
        print("  {:<14} → {}".format(t, ", ".join("{}({})".format(g, n) for g, n in c.items())))
    write_html(rows, truth, r["pred"], r["by_pred"], r["majority"], r["wrong"], r["by_truth"],
               r["acc"], r["p"], r["rc"], r["f1"])
    print("\n결과 페이지: " + os.path.join(HERE, "grouping_result.html"))


def write_html(rows, truth, pred, by_pred, majority, wrong, by_truth, acc, p, rc, f1):
    e = html.escape
    tasks = list(by_truth)
    color = {t: "hsl({},55%,{}%)".format(i * 360 // len(tasks), 45) for i, t in enumerate(tasks)}

    def chip(t):
        return '<span class="chip" style="--c:{}">{}</span>'.format(color[t], e(t))

    cards = []
    for g, rs in by_pred.items():
        tot = sum(r["total"] for r in rs)
        items = "".join(
            '<li class="{cls}"><span class="mk">{mk}</span><span class="meta">{s} · {ts}</span>'
            '<span class="p">{p}</span>{chip}</li>'.format(
                cls="bad" if tracker.row_key(r) in wrong else "", mk="✗" if tracker.row_key(r) in wrong else "✓",
                s=r["session"], ts=r["ts"][5:16].replace("T", " "), p=e(r["purpose"]),
                chip=chip(truth[tracker.row_key(r)]))
            for r in rs)
        cards.append('<section class="card"><h3>{}</h3><div class="sub">프롬프트 {}개 · 토큰 {:,} · 다수 정답: {}</div>'
                     '<ul>{}</ul></section>'.format(e(g), len(rs), tot, chip(majority[g]), items))
    trows = "".join("<tr><td>{}</td><td>{}</td></tr>".format(
        chip(t), " ".join('<span class="g">{} <b>{}</b></span>'.format(e(g), n) for g, n in c.items()))
        for t, c in by_truth.items())
    timeline = "".join('<i title="{}" style="background:{}" class="{}"></i>'.format(
        e(r["purpose"]), color[truth[tracker.row_key(r)]], "x" if tracker.row_key(r) in wrong else "")
        for r in rows)

    page = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>자동 분류 평가</title>
<style>
:root{{--bg:#f7f7f5;--fg:#1d1d1b;--mute:#6b6b66;--card:#fff;--line:#e4e4df;--bad:#fdecea}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--fg:#ececea;--mute:#9a9a94;--card:#21211f;--line:#33332f;--bad:#3a2220}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,system-ui,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 8px}} h3{{margin:0;font-size:15px}}
.stats{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}}
.stat{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}}
.stat b{{display:block;font-size:22px}} .stat span{{color:var(--mute);font-size:12px}}
.tl{{display:flex;gap:2px;flex-wrap:wrap}} .tl i{{width:12px;height:22px;border-radius:2px}}
.tl i.x{{outline:2px solid var(--fg);outline-offset:-2px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(480px,1fr));gap:12px}}
@media (max-width:540px){{.grid{{grid-template-columns:1fr}}}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}
.sub{{color:var(--mute);font-size:12px;margin:2px 0 8px}}
ul{{list-style:none;margin:0;padding:0}} li{{display:flex;gap:8px;align-items:baseline;padding:4px 6px;border-radius:6px}}
li.bad{{background:var(--bad)}} .mk{{width:14px;color:var(--mute)}} li.bad .mk{{color:#d33}}
.meta{{color:var(--mute);font-size:11px;white-space:nowrap}} .p{{flex:1}}
.chip{{font-size:11px;white-space:nowrap;padding:1px 7px;border-radius:9px;color:#fff;background:var(--c)}}
table{{border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;width:100%}}
td{{padding:6px 10px;border-top:1px solid var(--line)}} .g{{margin-right:10px}}
</style></head><body><main>
<h1>자동 작업 분류 평가</h1>
<div class="sub">fixture_prompts.tsv · 1일차(s1~s4) 분류 후 2일차(s5)를 캐시 위에 추가 분류</div>
<div class="stats">
<div class="stat"><b>{acc:.0%}</b><span>정확도 (그룹 다수결 기준)</span></div>
<div class="stat"><b>{f1:.0%}</b><span>쌍 F1 (precision {p:.0%} · recall {rc:.0%})</span></div>
<div class="stat"><b>{n}</b><span>프롬프트</span></div>
<div class="stat"><b>{nt} → {np}</b><span>정답 작업 → 분류된 작업</span></div>
</div>
<h2>시간순 타임라인 (색 = 정답 작업, 테두리 = 오분류)</h2><div class="tl">{timeline}</div>
<h2>정답 작업이 어디로 갔나</h2><table>{trows}</table>
<h2>분류된 작업별 프롬프트</h2><div class="grid">{cards}</div>
</main></body></html>""".format(acc=acc, f1=f1, p=p, rc=rc, n=len(rows), nt=len(by_truth), np=len(by_pred),
                                timeline=timeline, trows=trows, cards="".join(cards))
    with open(os.path.join(HERE, "grouping_result.html"), "w") as f:
        f.write(page)


if __name__ == "__main__":
    main()
