# token-tracker

**English** | [한국어](README.ko.md)

A Claude Code plugin that records token usage for every prompt, lets Claude group your prompts into **work units** (features and bugs) automatically, and shows tokens and cost per work unit.

Sample report: [tests/sample_report.html](tests/sample_report.html) (download and open it in a browser)

## Install

```
/plugin marketplace add kwakyongwoo/token-tracker
/plugin install token-tracker@token-tracker
```

Try it locally without installing: `claude --plugin-dir /path/to/token-tracker`

Requires `python3` (standard library only) and the `claude` CLI.

### Update

Refresh the marketplace first, then update the plugin, then restart Claude Code:

```
claude plugin marketplace update token-tracker
claude plugin update token-tracker@token-tracker
```

## Usage

Work as usual; recording is automatic.

| Command | What it does |
|---|---|
| `/token-tracker:report` | Table per work unit (input / output / cache write / cache read / cost) |
| `/token-tracker:report <work name>` | Per-prompt breakdown of one work unit |
| `/token-tracker:report-html` | Builds an HTML report and opens it in the browser |
| `/token-tracker:feature <name>` | (Optional) Pin a work name instead of automatic grouping. `off` to unpin |

## How it works

- **Recording**: when a reply finishes, a `Stop` hook reads the turn's usage and model from the transcript and appends one line to `<project>/.claude/token-usage/log.jsonl`. The folder gets its own `.gitignore`, so it is never committed to your project's git.
- **Grouping**: when you run a report, only prompts not yet grouped are sent to `claude -p --model sonnet`. Results are cached in `groups.json` and never reclassified. Work-unit names follow the language of your prompts. To fix a grouping, edit the name in `groups.json`; delete the file to regroup everything.
- **Cost**: estimated at API list prices (`PRICES` in `scripts/tracker.py`). Cache writes are priced at 1.25x input for the 5-minute TTL and 2x for the 1-hour TTL. This differs from subscription (Pro/Max) billing; use it to compare work units.

Environment variables: `TOKEN_TRACKER_MODEL` (grouping model, default `sonnet`), `TOKEN_TRACKER_NO_OPEN=1` (don't open the HTML report automatically).

## Limitations

- Tokens used by subagents are not included (they live in separate transcripts).
- Records from versions before 0.1.0 have no model, so their cost shows as "unknown model".

## Grouping eval

```
python3 tests/eval_grouping.py 4   # groups 63 sample prompts 4 times and compares with the answer key
```

With sonnet: 10 work units in all 4 runs (answer key: 10), average F1 93%.
