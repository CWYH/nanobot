---
name: code-review
description: Review Python code for bugs, security issues, performance problems, and design quality. Use when asked to review code, review a PR, review a branch, audit a codebase or module, or when the user says "review", "code review", "review this", "check this code", "audit", or "look over this". Supports three modes - PR/branch diff review, ad-hoc file review, and codebase-wide audit. Produces line-level findings and a categorized summary report.
---

# Code Review

## Workflow

Determine the review mode, then follow the corresponding workflow:

1. **PR / branch diff**: User asks to review a PR, branch, or recent changes
2. **Ad-hoc file review**: User points to specific files or directories
3. **Codebase audit**: User asks to review an entire module or project

### Mode 1: PR / Branch Diff

1. Run `git diff main...HEAD` (or the specified base branch) to collect changed files
2. For each changed file, read the full file for context (not just the diff hunks)
3. Review changes against the checklist in [references/python-checklist.md](references/python-checklist.md)
4. Produce output (see Output Format below)

### Mode 2: Ad-hoc File Review

1. Read each specified file in full
2. Review against the checklist
3. Produce output

### Mode 3: Codebase Audit

1. Identify all Python files in the target directory tree
2. Prioritize: entry points, public APIs, and recently modified files first
3. Review each file against the checklist
4. Produce output with a top-level summary

## Review Process

For each file or diff hunk:

1. **Understand intent** -- read surrounding code before judging a change
2. **Check against categories** in [references/python-checklist.md](references/python-checklist.md): Correctness, Security, Performance, Design, Async, API
3. **Classify severity** for each finding:
   - `critical` -- Bugs, security vulnerabilities, data loss risks
   - `warning` -- Performance issues, potential bugs under edge cases, maintainability concerns
   - `suggestion` -- Style improvements, minor refactors, nice-to-haves
4. **Skip noise** -- Do not flag things that are clearly intentional, idiomatic, or where the existing approach is reasonable. Avoid false positives over catching everything.

## Output Format

Produce two sections: inline findings, then summary.

### Part 1: Inline Findings

For each finding, use this format:

```
### <filepath>:<line_number>

**[severity]** <category>

<description of the issue>

Suggested fix:
\`\`\`python
<corrected code>
\`\`\`
```

Example:

```
### src/api/auth.py:42

**[critical]** Security

`password` parameter is logged in plaintext via `logger.debug(f"Login attempt: {password}")`.

Suggested fix:
\`\`\`python
logger.debug("Login attempt for user: %s", username)
\`\`\`
```

Group findings by file. Order files alphabetically. Within each file, order by line number.

### Part 2: Summary Report

After all inline findings, produce:

```
## Review Summary

**Files reviewed**: <count>
**Findings**: <critical_count> critical, <warning_count> warnings, <suggestion_count> suggestions

### Critical Issues
- <one-line summary with file:line reference>

### Warnings
- <one-line summary with file:line reference>

### Suggestions
- <one-line summary with file:line reference>

### Overall Assessment
<1-2 paragraph assessment of code quality, patterns observed, and top recommendations>
```

If there are no findings in a severity category, omit that section.

## Guidelines

- Be direct and specific. "This might cause issues" is not useful. "This raises `KeyError` when `config` lacks a `timeout` key" is.
- Always provide a suggested fix for critical and warning findings.
- For PR reviews, focus on the changed code. Flag pre-existing issues only if the change makes them worse.
- Do not flag style issues that are consistent with the rest of the codebase.
- When unsure whether something is a bug, state the assumption clearly: "If `x` can be None here, this raises `AttributeError`."
