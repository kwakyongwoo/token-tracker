#!/usr/bin/env python3
"""token-tracker: record token usage per prompt and total it per work unit.

Data: <project>/.claude/token-usage/
  log.jsonl    one line per prompt
  groups.json  work-unit classification cache
  feature      optional pinned work name (one line)
"""
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, OrderedDict
from datetime import datetime

KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
LABELS = ("input", "output", "cache write", "cache read")

# $/MTok: (input, output, cache read). cache write = input x1.25 (5-min TTL) / x2 (1-hour TTL).
# Source: Anthropic API list prices as of 2026-10-06. Update here when prices change.
# shortcut: ignores Haiku 5.5's >100K-prompt surcharge and Batch discounts; add if needed.
PRICES = {
    "claude-fable-5-1": (10, 50, 0.25), "claude-mythos-5-1": (10, 50, 0.25),
    "claude-fable-5": (10, 50, 1.0), "claude-mythos-5": (10, 50, 1.0),
    "claude-opus-5-5": (4, 20, 0.20), "claude-opus-5": (5, 25, 0.50),
    "claude-opus-4-8": (5, 25, 0.50), "claude-opus-4-7": (5, 25, 0.50), "claude-opus-4-6": (5, 25, 0.50),
    "claude-sonnet-5-5": (2, 10, 0.20), "claude-sonnet-5": (2, 10, 0.20), "claude-sonnet-4-6": (3, 15, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-haiku-4-5": (1, 5, 0.10),
}


def price_for(model):
    """Price for a model id, by longest prefix match ('claude-opus-5-5[1m]' -> opus-5-5)."""
    keys = [k for k in PRICES if (model or "").startswith(k)]
    return PRICES[max(keys, key=len)] if keys else None


def cost_parts(row):
    """Cost ($) per token type for one prompt (row). None if the model is unknown.
    shortcut: a turn that mixes models is priced at its most-used model; store per message if mixing becomes common."""
    p = price_for(row.get("model"))
    if p is None:
        return None
    inp, out, read = p
    w1h = row.get("cache_write_1h_tokens", 0)
    parts = {"input": row["input_tokens"] * inp, "output": row["output_tokens"] * out,
             "cache write": (row["cache_creation_input_tokens"] - w1h) * inp * 1.25 + w1h * inp * 2,
             "cache read": row["cache_read_input_tokens"] * read}
    mult = 2 if row.get("fast") else 1  # fast mode costs 2x
    return {k: v * mult / 1e6 for k, v in parts.items()}


def data_dir(cwd):
    d = os.path.join(cwd, ".claude", "token-usage")
    os.makedirs(d, exist_ok=True)
    gi = os.path.join(d, ".gitignore")
    if not os.path.exists(gi):  # keep prompt text out of the project's git
        with open(gi, "w") as f:
            f.write("# token-tracker logs (contain prompt text) - do not commit\n*\n")
    return d


def current_feature(cwd):
    try:
        with open(os.path.join(data_dir(cwd), "feature")) as f:
            return f.read().strip() or "(none)"
    except FileNotFoundError:
        return "(none)"


def is_prompt(entry):
    """True if the entry is a prompt typed by the user (not a tool_result)."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return False


def prompt_text(entry):
    content = entry["message"]["content"]
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    # slash commands are logged as XML tags -> shorten to "/name args"
    cmd = re.search(r"<command-name>(.*?)</command-name>", content)
    if cmd:
        args = re.search(r"<command-args>(.*?)</command-args>", content, re.S)
        content = cmd.group(1) + (" " + args.group(1) if args else "")
    return " ".join(content.split())


def last_turn_usage(lines):
    """Return the last prompt and the summed usage of the assistant messages after it."""
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    start = next((i for i in range(len(entries) - 1, -1, -1) if is_prompt(entries[i])), None)
    if start is None:
        return None, None, True, None
    # one message is logged once per content block, so dedupe by message.id
    by_id = OrderedDict()
    last_stop = None
    for e in entries[start + 1:]:
        msg = e.get("message") or {}
        if e.get("type") == "assistant" and msg.get("usage"):
            by_id[msg.get("id") or id(e)] = (msg.get("model"), msg["usage"])
            last_stop = msg.get("stop_reason")
    total = {k: sum(u.get(k, 0) or 0 for _, u in by_id.values()) for k in KEYS}
    total["cache_write_1h_tokens"] = sum(
        (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0 for _, u in by_id.values())
    total["fast"] = any(u.get("speed") == "fast" for _, u in by_id.values())
    out_by_model = Counter()
    for m, u in by_id.values():
        out_by_model[m] += u.get("output_tokens") or 0
    model = out_by_model.most_common(1)[0][0] if out_by_model else None
    done = last_stop not in (None, "tool_use")
    return prompt_text(entries[start]), total, done, model


def git_branch(cwd):
    try:
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def cmd_stop():
    if os.environ.get("TOKEN_TRACKER_SKIP"):  # skip the classifier calls that report itself makes
        return
    hook = json.load(sys.stdin)
    cwd = hook.get("cwd") or os.getcwd()
    # the Stop hook can fire before the last reply is flushed to the transcript, so retry for up to 3s
    for _ in range(15):
        try:
            with open(hook["transcript_path"]) as f:
                prompt, usage, done, model = last_turn_usage(f)
        except (KeyError, OSError):
            return
        if done:
            break
        time.sleep(0.2)
    if prompt is None or prompt.startswith("/token-tracker:"):
        return
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "session": hook.get("session_id"),
        "branch": git_branch(cwd),
        "feature": current_feature(cwd),
        "purpose": prompt[:200],
        "model": model,
        **usage,
        "total": sum(usage[k] for k in KEYS),
    }
    with open(os.path.join(data_dir(cwd), "log.jsonl"), "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def cmd_feature(name):
    cwd = os.getcwd()
    path = os.path.join(data_dir(cwd), "feature")
    if name in ("off", "auto"):
        open(path, "w").close()
    elif name:
        with open(path, "w") as f:
            f.write(name.strip() + "\n")
    print("Current work: " + current_feature(cwd) + "  ((none) = automatic classification)")


def row_key(r):
    return "{}|{}".format(r.get("session"), r["ts"])


CLASSIFY_PROMPT = """Below is a developer's prompt history in Claude Code (chronological).
Label each prompt with the "work unit" it belongs to.

What counts as one work unit:
- A work unit = one user-facing feature or one bug. All steps of that feature (UI, validation,
  error handling, tests, refactoring, follow-up fixes, extensions of the same feature) are the same unit.
  e.g. "login form" / "password validation" / "logout button" / "add social login" -> one login unit.
- Fixing the same bug again when it reappears via another path is still the original bug unit.
- Different features stay separate even with similar names (e.g. payment error fix != payment page design).
- An urgent task that interrupts other work is its own unit; "back to X" returns to unit X.
- Short replies such as "ok", "yes", "continue", "go" belong to the same unit as the prompt right before.
- Same session/branch and close in time suggests the same unit, but content wins.

Existing work units (name and example prompts):
{known}

If a prompt belongs to an existing unit, reuse that name exactly, character for character.
Name new units in 2-5 words, in the same language as the prompts, after the feature rather than a
sub-step (e.g. "Login", "Checkout 500 error").

Prompts to classify (number<TAB>time<TAB>session<TAB>branch<TAB>text):
{lines}

Output only a JSON object, nothing else: {{"number": "work unit name", ...}}"""


def classify(rows, known):
    """Classify rows into work units with claude (sonnet by default).
    known = {existing work name: [example prompts]}. Returns {row_key: work name}."""
    lines = "\n".join("{}\t{}\t{}\t{}\t{}".format(
        i, r["ts"], (r.get("session") or "")[:8], r.get("branch") or "-", r["purpose"])
        for i, r in enumerate(rows))
    known_text = "\n".join("- {}: {}".format(g, " / ".join(ex)) for g, ex in known.items()) or "(none)"
    prompt = CLASSIFY_PROMPT.format(known=known_text, lines=lines)
    out = subprocess.run(
        # sonnet: more consistent run-to-run than haiku (tests/eval_grouping.py, 4 runs: same F1 92%, same group count).
        # dropping the default system prompt and CLAUDE.md cuts the cost per call ~10x.
        ["claude", "-p", "--model", os.environ.get("TOKEN_TRACKER_MODEL", "sonnet"), "--tools", "",
         "--no-session-persistence", "--setting-sources", "",
         "--system-prompt", "You group developer prompts into work units. Output only the requested JSON."],
        input=prompt, capture_output=True, text=True, timeout=180,
        env=dict(os.environ, TOKEN_TRACKER_SKIP="1"))
    m = re.search(r"\{.*\}", out.stdout, re.S)
    if not m:
        raise RuntimeError("classification failed: " + (out.stderr or out.stdout)[:300])
    labels = json.loads(m.group(0))
    return {row_key(r): labels[str(i)] for i, r in enumerate(rows) if str(i) in labels}


def load_groups(d, rows):
    """Pinned name > cache > new classification. New results are cached in groups.json."""
    path = os.path.join(d, "groups.json")
    try:
        with open(path) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    todo = [r for r in rows if r.get("feature") in (None, "(none)") and row_key(r) not in cache]
    # shortcut: classifies in batches of 200; across batches only the earlier work names carry over
    purpose = {row_key(r): r["purpose"] for r in rows}
    for i in range(0, len(todo), 200):
        known = OrderedDict()  # 3 recent examples per work unit: reuses existing names better than names alone
        for k, g in cache.items():
            if k in purpose:
                known.setdefault(g, []).append(purpose[k][:60])
        known = OrderedDict((g, ex[-3:]) for g, ex in known.items())
        cache.update(classify(todo[i:i + 200], known))
        with open(path, "w") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
    return {row_key(r): (r["feature"] if r.get("feature") not in (None, "(none)")
                         else cache.get(row_key(r), "(unclassified)")) for r in rows}


def load_rows(d):
    path = os.path.join(d, "log.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        rows = [json.loads(l) for l in f if l.strip()]
    groups = load_groups(d, rows)
    for r in rows:
        r["group"] = groups[row_key(r)]
    return rows


def aggregate(rows):
    """Totals per work unit: 4 token types + cost per type. unknown = prompts with no known model (no cost)."""
    agg = OrderedDict()
    for r in rows:
        a = agg.setdefault(r["group"], dict(prompts=0, unknown=0, cost=0.0, total=0,
                                            **dict.fromkeys(KEYS, 0), **{"$" + l: 0.0 for l in LABELS}))
        a["prompts"] += 1
        a["total"] += r.get("total", 0)
        for k in KEYS:
            a[k] += r.get(k, 0)
        c = cost_parts(r)
        if c is None:
            a["unknown"] += 1
        else:
            for l in LABELS:
                a["$" + l] += c[l]
            a["cost"] += sum(c.values())
    return agg


def usd(v):
    return "${:,.2f}".format(v) if v >= 10 else "${:.3f}".format(v)


def cmd_report(only=None):
    rows = load_rows(data_dir(os.getcwd()))
    if not rows:
        print("No records yet.")
        return
    if only:
        rows = [r for r in rows if r["group"] == only]
        for r in rows:
            c = cost_parts(r)
            print("{}  {:>8}  out {:>7,}  write {:>9,}  read {:>10,}  {}".format(
                r["ts"], usd(sum(c.values())) if c else "?", r["output_tokens"],
                r["cache_creation_input_tokens"], r["cache_read_input_tokens"], r["purpose"][:60]))
        print()
    agg = OrderedDict(sorted(aggregate(rows).items(), key=lambda kv: -kv[1]["cost"]))
    print("| Work | Prompts | input | output | cache write | cache read | Cost |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for name, a in agg.items():
        print("| {} | {} | {:,} | {:,} | {:,} | {:,} | {}{} |".format(
            name, a["prompts"], a["input_tokens"], a["output_tokens"],
            a["cache_creation_input_tokens"], a["cache_read_input_tokens"], usd(a["cost"]),
            " (+{} with unknown model)".format(a["unknown"]) if a["unknown"] else ""))


COLORS = dict(zip(LABELS, ("#8b5cf6", "#3b82f6", "#f59e0b", "#9ca3af")))

HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Token Usage Report</title>
<style>
:root{--bg:#f7f7f5;--fg:#1d1d1b;--mute:#6b6b66;--card:#fff;--line:#e4e4df;--track:#ececE8}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ececea;--mute:#9a9a94;--card:#21211f;--line:#33332f;--track:#2c2c29}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 8px} h3{margin:0;font-size:15px}
.sub{color:var(--mute);font-size:12px}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.stat b{display:block;font-size:22px;font-variant-numeric:tabular-nums} .stat span{color:var(--mute);font-size:12px}
.box{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;overflow-x:auto}
.bar{display:grid;grid-template-columns:minmax(120px,220px) 1fr 130px;gap:10px;align-items:center;padding:4px 0}
.t{height:14px;background:var(--track);border-radius:4px;overflow:hidden;display:flex}
.t i{display:block;height:100%}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.dot{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;background:var(--c)}
.tl{display:flex;gap:2px;height:70px} .tl i{flex:1;max-width:12px;min-width:3px;border-radius:2px;align-self:flex-end}
.legend{color:var(--mute);font-size:12px;margin-top:8px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 10px;vertical-align:-1px}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:8px}
summary{padding:10px 14px;cursor:pointer;display:flex;gap:10px;align-items:baseline}
summary h3{flex:1}
table{border-collapse:collapse;width:100%;font-size:13px}
td,th{padding:5px 10px;border-top:1px solid var(--line);text-align:left}
th{color:var(--mute);font-weight:500;font-size:12px;white-space:nowrap}
td.p{width:100%} td.sub{white-space:nowrap}
@media (max-width:600px){.bar{grid-template-columns:1fr 90px}.bar .t{grid-column:1/-1;order:3}.hide-sm{display:none}}
</style></head><body><main>
<h1>Token Usage Report</h1><div class="sub">{project} · {span} · costs estimated at API list prices</div>
<div class="stats">{stats}</div>
<h2>Cost breakdown (all)</h2><div class="box">{mix}</div>
<h2>Cost by work unit</h2><div class="box">{bars}<div class="legend">{legend}</div></div>
<h2>Tokens by work unit</h2><div class="box"><table>{tokens}</table></div>
<h2>Timeline (bar height = cost, color = work unit)</h2><div class="box tl">{timeline}</div>
<h2>Prompts by work unit</h2>{details}
</main></body></html>"""


def cmd_html():
    import html
    cwd = os.getcwd()
    d = data_dir(cwd)
    rows = load_rows(d)
    if not rows:
        print("No records yet.")
        return
    e = html.escape
    agg = OrderedDict(sorted(aggregate(rows).items(), key=lambda kv: (-kv[1]["cost"], -kv[1]["total"])))
    color = {g: "hsl({},55%,48%)".format(i * 360 // len(agg)) for i, g in enumerate(agg)}
    grand = sum(a["cost"] for a in agg.values())
    top = max(a["cost"] for a in agg.values()) or 1
    costs = [sum(c.values()) if c else 0 for c in map(cost_parts, rows)]
    peak = max(costs) or 1
    unknown = sum(a["unknown"] for a in agg.values())
    tok = {k: sum(a[k] for a in agg.values()) for k in KEYS}
    by_label = {l: sum(a["$" + l] for a in agg.values()) for l in LABELS}

    def pct(v, base):
        return "{:.2f}%".format(100.0 * v / (base or 1))

    def segs(a, base):
        return "".join('<i style="width:{};background:{}"></i>'.format(pct(a["$" + l], base), COLORS[l]) for l in LABELS)

    stats = [("Total cost", usd(grand)), ("Prompts", len(rows)), ("Work units", len(agg)),
             ("Avg per prompt", usd(grand / max(len(rows) - unknown, 1))),
             ("Sessions", len({r.get("session") for r in rows}))]
    if unknown:
        stats.append(("Unknown model (no cost)", unknown))
    stats = "".join('<div class="stat"><b>{}</b><span>{}</span></div>'.format(v, k) for k, v in stats)
    mix = ('<div class="t" style="height:22px">{}</div><table style="margin-top:10px">'
           '<tr><th>Type</th><th class="num">Tokens</th><th class="num">Cost</th><th class="num">Share</th></tr>{}</table>').format(
        segs({"$" + l: by_label[l] for l in LABELS}, grand),
        "".join('<tr><td><span class="dot" style="--c:{}"></span>{}</td><td class="num">{:,}</td>'
                '<td class="num">{}</td><td class="num">{:.0f}%</td></tr>'.format(
                    COLORS[l], l, tok[k], usd(by_label[l]), 100.0 * by_label[l] / (grand or 1))
                for l, k in zip(LABELS, KEYS)))
    bars = "".join(
        '<div class="bar"><div><span class="dot" style="--c:{c}"></span>{g}</div>'
        '<div class="t" title="{tip}">{segs}</div>'
        '<div class="num">{cost} <span class="sub">({share:.0f}%)</span></div></div>'.format(
            c=color[g], g=e(g), segs=segs(a, top), cost=usd(a["cost"]), share=100.0 * a["cost"] / (grand or 1),
            tip=" · ".join("{} {}".format(l, usd(a["$" + l])) for l in LABELS))
        for g, a in agg.items())
    legend = "Breakdown:" + "".join('<i style="background:{}"></i>{}'.format(COLORS[l], l) for l in LABELS)
    tokens = ('<tr><th>Work</th><th class="num">Prompts</th>' + "".join('<th class="num">{}</th>'.format(l) for l in LABELS)
              + '<th class="num">Cost</th></tr>' + "".join(
                  '<tr><td><span class="dot" style="--c:{}"></span>{}</td><td class="num">{}</td>{}<td class="num">{}</td></tr>'.format(
                      color[g], e(g), a["prompts"], "".join('<td class="num">{:,}</td>'.format(a[k]) for k in KEYS), usd(a["cost"]))
                  for g, a in agg.items()))
    timeline = "".join('<i title="{} · {} · {}" style="background:{};height:{}"></i>'.format(
        e(r["group"]), usd(c), e(r["purpose"]), color[r["group"]], pct(max(c, peak * 0.04), peak))
        for r, c in zip(rows, costs))
    details = []
    for g, a in agg.items():
        trs = "".join(
            '<tr><td class="sub">{}</td><td class="p">{}</td><td class="num hide-sm">{:,}</td>'
            '<td class="num hide-sm">{:,}</td><td class="num hide-sm">{:,}</td><td class="num">{}</td></tr>'.format(
                r["ts"][5:16].replace("T", " "), e(r["purpose"]), r["output_tokens"],
                r["cache_creation_input_tokens"], r["cache_read_input_tokens"], usd(c) if cost_parts(r) else "?")
            for r, c in zip(rows, costs) if r["group"] == g)
        details.append(
            '<details><summary><span class="dot" style="--c:{}"></span><h3>{}</h3>'
            '<span class="sub">{} prompts</span><b class="num">{}</b></summary>'
            '<table><tr><th>Time</th><th>Prompt</th><th class="num hide-sm">output</th>'
            '<th class="num hide-sm">cache write</th><th class="num hide-sm">cache read</th><th class="num">Cost</th></tr>{}</table></details>'.format(
                color[g], e(g), a["prompts"], usd(a["cost"]), trs))
    page = HTML
    for k, v in (("{project}", e(os.path.basename(cwd))),
                 ("{span}", "{} ~ {}".format(rows[0]["ts"][:16].replace("T", " "), rows[-1]["ts"][:16].replace("T", " "))),
                 ("{stats}", stats), ("{mix}", mix), ("{bars}", bars), ("{legend}", legend),
                 ("{tokens}", tokens), ("{timeline}", timeline), ("{details}", "".join(details))):
        page = page.replace(k, v)
    out = os.path.join(d, "report.html")
    with open(out, "w") as f:
        f.write(page)
    print(out)
    if sys.platform == "darwin" and not os.environ.get("TOKEN_TRACKER_NO_OPEN"):
        subprocess.run(["open", out])


if __name__ == "__main__":
    cmd, args = sys.argv[1], " ".join(sys.argv[2:]).strip()
    if cmd == "stop":
        cmd_stop()
    elif cmd == "feature":
        cmd_feature(args)
    elif cmd == "report":
        cmd_report(args or None)
    elif cmd == "html":
        cmd_html()
