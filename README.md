# mine-sessions

Analyze your Claude Code session transcripts. Find workflow patterns, friction points, and setup improvements.

## Install

```
/plugin install https://github.com/taxfix/mine-sessions-plugin
```

## Use

```
/mine-sessions
```

## What it does

- Mines all your Claude Code session transcripts (~30s)
- Dispatches agents to deep-read every conversation (~3-5 min)
- Generates an HTML report with:
  - Time allocation breakdown
  - Workflow patterns you repeat
  - Friction points ranked by time wasted
  - Setup coaching: what to add to CLAUDE.md, skills, hooks, rules
  - Automation opportunities with estimated time savings
