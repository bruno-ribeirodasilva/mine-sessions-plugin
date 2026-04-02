---
name: mine-sessions
description: Analyze Claude Code session transcripts to find workflow patterns, friction points, and setup improvements. Produces an evidence-based HTML report with actionable fixes.
---

# Mine Sessions

Analyze Claude Code session transcripts. Every finding backed by evidence. Every recommendation specific and actionable.

No fluff. No flattery. Cite sessions, quote the user, show the numbers.

## Step 0: Auto-Scope

```bash
find ~/.claude/projects/ -name "*.jsonl" -not -path "*/subagents/*" | while read f; do
  wc -c < "$f"
done | awk '{n++; s+=$1} END {print n, int(s/1024/1024)}'
```

| Sessions | Size | Action |
|----------|------|--------|
| < 200 | < 500MB | Full analysis, no questions |
| 200-500 | 500MB-2GB | Ask: all, last 30, or last 90 days? |
| > 500 | > 2GB | Default last 90 days |

## Step 1: Quantitative Mining

```bash
OUTPUT_DIR=~/.claude/session_analysis/run_$(date +%Y%m%d_%H%M%S)
mkdir -p "$OUTPUT_DIR"
python3 scripts/mine_sessions.py --dashboard --output-dir "$OUTPUT_DIR"
ln -sfn "$OUTPUT_DIR" ~/.claude/session_analysis/latest
```

Note: `mine_sessions.py` and `filter_transcript.py` must be at `scripts/` in the plugin directory OR at `~/.claude/scripts/`. Check both locations.

## Step 2: Filter Transcripts

```bash
mkdir -p "$OUTPUT_DIR/filtered"

# Iterate ALL project dirs — not just the biggest one
for PROJECT_DIR in $(python3 -c "
import os
base = os.path.expanduser('~/.claude/projects')
for d in os.listdir(base):
    dp = os.path.join(base, d)
    if os.path.isdir(dp) and any(f.endswith('.jsonl') for f in os.listdir(dp)):
        print(dp)
"); do
  for f in "$PROJECT_DIR"/*.jsonl; do
    fname=$(basename "$f" .jsonl)
    out="$OUTPUT_DIR/filtered/${fname}.txt"
    python3 scripts/filter_transcript.py "$f" > "$out" 2>/dev/null || \
    python3 ~/.claude/scripts/filter_transcript.py "$f" > "$out" 2>/dev/null
    [ $(wc -c < "$out" | tr -d ' ') -lt 500 ] && rm "$out"
  done
done
```

## Step 3: Batch

```python
import os
files = sorted([f for f in os.listdir('.') if f.endswith('.txt')],
               key=lambda f: os.path.getsize(f), reverse=True)
batches, current, size = [], [], 0
MAX = 2_000_000
for f in files:
    s = os.path.getsize(f)
    if size + s > MAX and current:
        batches.append(current); current, size = [], 0
    current.append(f); size += s
if current: batches.append(current)
for i, batch in enumerate(batches):
    with open(f'../batch_{i}.txt', 'w') as out:
        for f in batch:
            out.write(f'=== SESSION: {f} ===\n')
            out.write(open(f).read() + '\n')
    print(f'Batch {i}: {len(batch)} sessions, {os.path.getsize(f"../batch_{i}.txt")//1024}KB')
```

## Step 4: Deep-Read Agents

Dispatch one agent per batch. ALL in parallel. Use `model: "opus"` for batches > 500KB, `model: "sonnet"` for smaller. Max 10 agents.

### Agent prompt — THIS IS CRITICAL FOR QUALITY

> Read every session in the batch file. For each session, extract:
>
> **Per session (10-15 lines each — be thorough):**
> - **What**: precise task description with model names, table names, column names, stakeholder names, PR numbers
> - **Workflow**: exact tool sequence (e.g., "Jira CLI → Slack MCP read thread → Snowflake query → Read model SQL → Edit schema.yml → dbt compile → git commit → gh pr create")
> - **Time signals**: any indication of duration, iteration count, or back-and-forth rounds
> - **Friction**: what failed, what was retried, what took longer than it should. Include error messages if visible.
> - **User corrections**: every time the user said "no", "wrong", "not that", "use X instead of Y" — capture BOTH the wrong thing and the right thing. These are gold.
> - **User quotes**: capture verbatim frustrations or strong reactions ("this is the massive problem of querying random tables", "dude you are in circles", "i cant be fixing everything 10 times")
> - **Setup gap**: what CLAUDE.md rule, skill, hook, agent, MCP, or memory entry would have prevented this friction? Be specific — name the file and the rule text.
>
> **Across all sessions in the batch (at the end):**
> - **Repeated corrections**: rules the user stated 2+ times that should be encoded
> - **Workflow patterns**: sequences of tools/steps that repeat across sessions
> - **Stakeholders served**: who asked for what, in what format
> - **Automation candidates**: manual multi-step work that a skill/command could replace
> - **Missing tools**: MCPs, plugins, or CLIs the user would benefit from
> - **Prompting patterns**: how the user starts sessions, how they give instructions — what works, what causes confusion

### Quality bar for deep-read agents

A good per-session analysis looks like this:

> **Session abc123 — BPI-1175: Company KPI row instability debug**
> Task: Investigated 1.86M row count swings in `company_kpi_actual_daily`. Root cause: `NULL = NULL` evaluates to FALSE in 9 FULL OUTER JOIN columns, causing 932K rows to duplicate. Fix: `equal_null()` on all 9 columns across 5 joins.
> Flow: Slack thread (Yara's report) → Jira CLI → worktree → Snowflake QUERY_HISTORY (3 months of row counts) → NULL analysis on join keys → implemented fix → PR #1879 with ci:full-refresh → dev build → Slack draft
> Friction: First dev build ran without --defer, producing invalid comparison (had to retract PR comment). Second build with --defer still hit stale dev tables. User stopped third build ("are you crazy? dont run more models 200x"). Context window exhausted twice.
> User corrections: "I wouldn't like you to jump into the conclusion... start a proper debug plan" — Claude jumped to root cause immediately, user forced systematic investigation.
> Setup gap: A rule in analytics-engineering.md: "When debugging data issues, always list hypotheses first and validate with queries before proposing fixes. Never jump to conclusions."

A BAD analysis looks like: "Debugged a data issue. Used Snowflake. Found a fix." — this is useless.

## Step 5: Synthesis Agent

Once all deep-read agents complete, dispatch ONE synthesis agent that reads ALL agent output files.

### Synthesis prompt

> Read all agent output files. Produce a unified analysis with these sections. Every finding must cite specific sessions and include numbers.
>
> **1. Session Catalog**
> Table with: session_id, ticket/topic, task type, one-line summary. Group by type.
>
> **2. Workflow Archetypes** (5-7 patterns)
> For each: name, exact step sequence, session count, average duration estimate, where friction occurs, and one detailed example session.
>
> **3. Friction Analysis** (ranked by total time wasted)
> For each friction point:
> - Root cause (specific, not vague)
> - Sessions affected (list them)
> - Estimated total time wasted (with math: N sessions × M minutes each)
> - User quotes if available
> - Specific fix: exact file, exact rule, exact command. Not "consider improving" but "add this line to CLAUDE.md: ..."
>
> **4. Tribal Knowledge**
> Every implicit rule the user applied or corrected Claude about. Grouped by domain. Each must include:
> - The rule (e.g., "Always use revenue_combined, not revenue_taxfix")
> - Why (e.g., "revenue_taxfix is Taxfix-only, misses Steuerbot/TaxScouts")
> - Where observed (session IDs)
>
> **5. Automation Matrix**
> Table ranked by weekly time savings:
> | Task | Frequency | Time each | What to build | Weekly savings |
> Each "what to build" must be specific: skill name, what it does, which tools it uses.
>
> **6. Setup Coaching**
> Concrete changes to the user's Claude Code setup. For each:
> - **What to change**: exact file + exact content to add/modify
> - **Why**: the pattern that caused friction (cite sessions)
> - **Impact**: estimated time saved
>
> Categories:
> - CLAUDE.md rules to add (show exact text)
> - Skills/commands to create (describe trigger + what it does)
> - Hooks to add (describe event + action)
> - MCPs/plugins to install (name + why)
> - Memory entries to add (show exact content)
> - User prompting improvements (be direct, cite examples)
>
> **7. Stakeholder Map**
> Who the user serves, what they ask for, delivery format, frequency, self-serve potential.

## Step 6: Generate Report

**YOU (the main Claude session) generate the report.** Do NOT delegate to a subagent. This lets the user see it, react, and ask for changes.

Read the synthesis output + `analysis.json`. Write the HTML to `$OUTPUT_DIR/final_report.html`.

### Report structure

```
1. Header — user profile (who they are, based on evidence)
2. Overview cards — sessions, hours, automation coverage, biggest friction metric
3. Time allocation — Chart.js donut + detailed table with % and real descriptions
4. Workflow archetypes — each in a card with step sequence, example, friction callout
5. Friction heatmap — table ranked by time wasted, with evidence boxes containing user quotes
6. Tribal knowledge — categorized lists (domain rules, tool rules, process rules)
7. Automation matrix — ranked table with frequency, time, what to build, savings
8. Setup coaching — the most actionable section:
   - Exact CLAUDE.md additions (show as code blocks)
   - Skills to create (name + description)
   - Hooks to add
   - MCPs/plugins to install
   - User behavior improvements (with session citations)
9. Roadmap — what to do this week, next week, this month
```

### Report design

- Self-contained HTML, Chart.js from CDN
- Dark theme: background #0a0c10, cards #161b22, borders #30363d
- Color coding: red (#f85149) for problems, yellow (#d29922) for warnings, green (#3fb950) for good
- Evidence boxes: dark background (#1c2128) with left border, containing quotes or data
- User quotes in italic with blue left border
- Sortable tables
- Sticky navigation
- Open in browser when done

### After generating

Show the user a summary of key findings in the chat. Ask: "Report is open in your browser. Want me to dig deeper on any section?"

## Step 7: Save

Save report to `$OUTPUT_DIR/final_report.html` and open in browser.

## Performance

- Mining: <30s
- Deep-read agents: ~3 min (parallel)
- Synthesis: ~2-3 min
- Report: ~1 min (you write it directly)
- **Total: ~5-8 minutes**
