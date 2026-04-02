---
name: mine-sessions
description: Analyze Claude Code session transcripts, deep-read conversations, and generate a visual productivity report with workflow patterns, friction points, and automation recommendations. Works for any role.
---

# Mine Sessions Skill

Analyze Claude Code session transcripts. Extract patterns, friction, and setup gaps. Produce an actionable report with concrete fixes.

**Role-agnostic.** No fluff, no flattery. Every finding must be backed by session evidence. Every recommendation must be specific and actionable (exact file, exact rule, exact command).

## Step 0: Auto-Scope

Run a quick pre-check to size the dataset:

```bash
find ~/.claude/projects/ -name "*.jsonl" -not -path "*/subagents/*" | while read f; do
  wc -c < "$f"
done | awk '{n++; s+=$1} END {print n, int(s/1024/1024)}'
```

| Sessions | Total Size | Action |
|----------|-----------|--------|
| < 200 | < 500MB | **Full analysis** — process everything, no questions asked |
| 200-500 | 500MB-2GB | Ask: "You have N sessions (XGB). Analyze all, or last 30/90 days?" |
| > 500 | > 2GB | Default to last 90 days. Offer "all" if user insists. |

Apply date scoping via mtime: `find ... -mtime -30` for last 30 days.

## Step 1: Quantitative Mining

Create a timestamped output directory so previous runs are never overwritten:

```bash
OUTPUT_DIR=~/.claude/session_analysis/run_$(date +%Y%m%d_%H%M%S)
mkdir -p "$OUTPUT_DIR"
python3 ~/.claude/scripts/mine_sessions.py --dashboard --output-dir "$OUTPUT_DIR"
```

Also symlink `latest` for easy access:
```bash
ln -sfn "$OUTPUT_DIR" ~/.claude/session_analysis/latest
```

Produces `sessions.json`, `analysis.json`, and `dashboard.html` in the timestamped directory. All subsequent steps use `$OUTPUT_DIR`.

## Step 2: Filter Transcripts

Convert JSONL transcripts into readable text for agent consumption:

```bash
mkdir -p ~/.claude/session_analysis/filtered

# Find the project dir with the most transcripts
PROJECT_DIR=$(python3 -c "
import os
base = os.path.expanduser('~/.claude/projects')
best, best_n = '', 0
for d in os.listdir(base):
    dp = os.path.join(base, d)
    if os.path.isdir(dp):
        n = sum(1 for f in os.listdir(dp) if f.endswith('.jsonl'))
        if n > best_n: best, best_n = dp, n
print(best)
")

for f in "$PROJECT_DIR"/*.jsonl; do
  fname=$(basename "$f" .jsonl)
  out="$HOME/.claude/session_analysis/filtered/${fname}.txt"
  python3 ~/.claude/scripts/filter_transcript.py "$f" > "$out" 2>/dev/null
  [ $(wc -c < "$out" | tr -d ' ') -lt 500 ] && rm "$out"
done
```

## Step 3: Batch for Deep Reading

Split into batches sized for the target model's context window:

```python
# Run this in ~/.claude/session_analysis/filtered/
import os

files = sorted([f for f in os.listdir('.') if f.endswith('.txt')],
               key=lambda f: os.path.getsize(f), reverse=True)

batches, current, size = [], [], 0
MAX = 2_000_000  # ~2MB per batch (Opus has 1M tokens ≈ 4MB text, leave room for output + tools)

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

## Step 4: Dispatch Deep-Read Agents

For each batch, dispatch a background agent. Launch ALL in parallel.

**Agent prompt template:**

> Read `~/.claude/session_analysis/batch_N.txt`. For EACH session write:
> - **Task**: What specifically was done (be precise — names, files, tools, stakeholders)
> - **Flow**: Step-by-step workflow (what tools in what order)
> - **Friction**: Where time was wasted, what failed, what was retried
> - **Insight**: What this reveals about the user's work patterns and role
> - **Setup Gap**: Could this session have been faster/smoother with a better CLAUDE.md rule, a skill, a hook, an agent, an MCP, a plugin, or a different communication style? Be specific.
> 
> Also look for these patterns across ALL sessions in the batch:
> - **User behavior to improve**: vague prompts that caused back-and-forth, missing context that Claude had to ask for, repeated corrections that should be encoded as rules
> - **Missing automation**: manual workflows that a skill/command/agent/hook could handle
> - **Setup recommendations**: CLAUDE.md rules, memory entries, output styles, or plugins that would prevent recurring friction
> 
> Be picky on user behavior — only flag patterns that repeat across 2+ sessions and would genuinely save time if changed.
> 
> 5-8 lines per session + a "Setup Recommendations" section at the end covering the full batch. Research only — no edits.

**Model selection:**
- Batch > 1MB → `model: "opus"` (only Opus can handle large batches)
- Batch < 500KB → `model: "sonnet"` (faster, cheaper for small batches)
- Between → `model: "opus"` to be safe
- Maximum 10 concurrent agents

## Step 5: Synthesis Agent

Once all deep-read agents complete, dispatch ONE synthesis agent:

> You are synthesizing findings from N analysis agents that each read batches of Claude Code session transcripts. Read all agent output files and produce a unified analysis:
>
> 1. **Session Catalog** — every session with: topic, task type, duration, one-line summary. Group by work type.
> 2. **Workflow Archetypes** — the 5-7 distinct patterns the user follows. For each: name, step sequence, frequency, average duration, friction points.
> 3. **Friction Analysis** — for each friction point: exact root cause, sessions affected, time wasted, what the fix looks like.
> 4. **Stakeholder Patterns** — who does the user serve? What do they ask for? What format? What could be self-serve?
> 5. **Tribal Knowledge** — implicit knowledge the user applies repeatedly that could be encoded into skills, rules, or memory. Be specific (table names, column rules, process rules).
> 6. **Automation Matrix** — every repeated manual task: frequency, time per occurrence, difficulty (easy/medium/hard), what to build (skill/command/agent/hook/scheduled), estimated weekly savings.
> 7. **Setup Coaching** — concrete recommendations to improve the user's Claude Code setup:
>    - **User behavior fixes** — patterns where the user could be more precise/efficient in how they prompt Claude. Only include if the pattern repeats 2+ times and clearly caused wasted time. Frame as coaching, not criticism. Example: "In 4 sessions, vague initial prompts like 'check this' required 2-3 clarification rounds. Starting with the model name + what to check would save ~5 min/session."
>    - **CLAUDE.md / rules changes** — specific lines to add to CLAUDE.md or rules files based on repeated corrections or knowledge that Claude kept getting wrong. Show the exact rule text.
>    - **Skills / commands to create** — repeated multi-step workflows that should be a single command. Describe what it does and the trigger pattern.
>    - **Agents to create** — complex autonomous tasks that would benefit from their own context window.
>    - **Hooks to add** — automated behaviors (pre-commit checks, session start validations, etc.) based on recurring manual checks.
>    - **MCPs / plugins to install** — tools the user doesn't have but would benefit from based on their workflow. Check what's available.
>    - **Memory entries to add** — corrections or knowledge that the user stated but was never saved to memory.
>    - **Communication style adjustments** — if the user's output style or Claude's response pattern causes friction (too verbose, too terse, wrong abstraction level for stakeholders).

Pass the agent output file paths to the synthesis agent.

## Step 6: Generate Report

Read the synthesis output + `analysis.json`. Generate `~/.claude/session_analysis/final_report.html`.

**Report sections — adapt to what the data reveals:**
1. **Profile** — who the user is, what they do, based on session evidence (not assumptions)
2. **Overview cards** — sessions, active hours, automation coverage, top friction metric
3. **Time allocation** — donut chart + table by task category
4. **Workflow archetypes** — patterns with step sequences and real examples from sessions
5. **Friction analysis** — ranked by time wasted, with root causes and evidence
6. **Tribal knowledge** — domain rules that should be encoded into skills/memory
7. **Automation matrix** — opportunities ranked by weekly time savings
8. **Setup Coaching** — the most actionable section:
   - User behavior improvements (with specific examples from sessions)
   - CLAUDE.md / rules changes (show the exact text to add)
   - Skills, commands, agents, hooks to create (with descriptions)
   - MCPs or plugins to install
   - Memory entries to add
   - Communication style tweaks
   No sugarcoating. If the user's prompts are vague, say so. If a rule is missing, show exactly what to add. Every item must cite the sessions where the problem occurred.
9. **Strategic recommendations** — what to build, delegate, or stop doing
10. **Roadmap** — phased implementation plan

**Report format:**
- Single self-contained HTML file
- Chart.js CDN for charts
- Dark theme (#0a0c10 background)
- Sortable tables
- Sticky navigation
- Open in browser when done: `open file://~/.claude/session_analysis/final_report.html`

## Output

- `~/.claude/session_analysis/sessions.json` — per-session quantitative data
- `~/.claude/session_analysis/analysis.json` — aggregates and patterns
- `~/.claude/session_analysis/final_report.html` — visual report (opens in browser)
- `~/.claude/session_analysis/filtered/` — filtered transcripts (intermediate)
- `~/.claude/session_analysis/batch_*.txt` — batch files (can delete after)

## Performance

- Quantitative mining: <30 seconds for 200 transcripts
- Deep-read agents: ~3 min wall time (parallel)
- Synthesis: ~2-3 min
- Report generation: <1 min
- **Total: ~5-8 minutes end-to-end**

## Tips

- Run monthly for trend tracking
- The report is most useful when followed by action — pick the top 3 automation opportunities and build them
- Compare reports month-over-month to see if infra investment is paying off (time allocation shift)
- The tribal knowledge section is the highest-leverage output — encode those rules into skills and memory immediately
