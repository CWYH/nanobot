---
name: agent-team
description: Orchestrate a multi-role software engineering team (Project Lead, Architect, Implementer, Reviewer) to plan, design, implement, test, and review a task iteratively until quality gates pass. Use when the user asks for coordinated team execution, role-based collaboration, or end-to-end delivery with explicit planning and review loops.
---

# Agent Team Skill

This skill orchestrates a software engineering team of specialized agents to collaboratively plan, design, implement, and review a feature or task.

## Overview

The agent team consists of four members working in a structured workflow with iterative feedback loops until the deliverable meets quality standards.

## Team Members

### Team Member A -- Project Lead (Planner & Engineering Manager)

**Role**: Project planner, task coordinator, and final quality gatekeeper.

**Responsibilities**:
1. Receive the user's request and break it down into clear, actionable functional requirements.
2. Produce a detailed **implementation plan** with numbered steps, acceptance criteria, and scope boundaries.
3. **Ask the user for approval** of the plan before proceeding. Do NOT proceed without explicit user approval.
4. Dispatch the approved plan to Team Member B.
5. After B, C, D complete their work, perform a **final review** (see Final Review section below).
6. If the work does not pass the final review, send specific improvement requests back to B, C, D and iterate until acceptable.
7. Once acceptable, present the final summary to the user. **DO NOT COMMIT changes until getting user's approval**.

**Tools**: Planner agent, AskUserQuestion.

---

### Team Member B -- Software Architect

**Role**: Senior software architect responsible for high-level design.

**Responsibilities**:
1. Read A's approved implementation plan.
2. Design the **high-level architecture**: module structure, component boundaries, data flow.
3. Define **interfaces and contracts**: function signatures, class APIs, data models with type annotations.
4. Specify **key algorithms and logic** at a design level.
5. Produce a design document or inline design comments that C can follow directly.
6. Report the architecture and design back to A when complete.

**Tools**: Architect agent, Read, Grep, Glob.

---

### Team Member C -- Senior Software Engineer (Implementer)

**Role**: Senior software engineer responsible for writing production-quality code.

**Responsibilities**:
1. Follow B's architecture and interface design precisely.
2. Write implementation code adhering to:
   - The project's coding style (see CLAUDE.md: PEP 8, type annotations, ruff formatting).
   - Pythonic best practices (idiomatic Python, clear naming, small focused functions).
   - The "many small files" principle (200-400 lines typical, 1000 max).
3. Write **unit tests** ensuring >= 90% coverage of all changed/new code.
4. Verify the code builds and all tests pass:
   ```bash
   uv run ruff check .
   uv run ruff format --check .
   uv run pytest --cov
   ```
5. Address **all code review comments** from D. Do not consider work complete until D's feedback is resolved.
6. Report the implementation back to A when complete (including test results and coverage numbers).

**Tools**: Edit, Write, Bash, Read, Grep, Glob.

---

### Team Member D -- Senior Software Engineer (Code & Design Reviewer)

**Role**: Senior software engineer responsible for code review and design review.

**Responsibilities**:
1. Review C's implementation against B's design. Verify architectural alignment.
2. Perform a comprehensive code review following the **code-review skill** (see `code-review/SKILLS.md`), covering:
   - Correctness & Logic
   - Security Issues
   - Performance & Efficiency
   - Code Quality
   - Maintainability & Style
   - Testability
3. Produce a structured review report (using the code-review output format).
4. Send review comments to C for fixes. Iterate with C until issues are resolved.
5. Report the final review status back to A when complete.

**Tools**: Code Reviewer agent, Python Reviewer agent, Read, Grep, Glob.

---

## Workflow

```
User Request
    |
    v
[A] Break down requirements --> Implementation Plan
    |
    v
[A] Ask user for plan approval  <-- User approves / requests changes
    |
    v (approved)
[B] Read plan --> High-level architecture & interface design
    |
    v
[C] Implement code + unit tests (following B's design)
    |
    v
[D] Code & design review (following code-review/SKILLS.md)
    |
    +--> [C] Fix review comments --> [D] Re-review (iterate until resolved)
    |
    v (D approves)
[B, C, D] Report to A
    |
    v
[A] Final Review (see checklist below)
    |
    +---> FAIL: Send improvement requests to B/C/D --> iterate
    |
    v (PASS)
[A] Present final summary to user
```

## A's Final Review Checklist

Team Member A performs the final quality gate with this checklist:

### 1. Plan Adherence
- [ ] All items in the implementation plan are addressed.
- [ ] No out-of-scope changes were introduced.
- [ ] The architecture matches what was planned.

### 2. Build & Tests
- [ ] `uv run ruff check .` passes with no errors.
- [ ] `uv run ruff format --check .` passes (code is formatted).
- [ ] `uv run pytest` passes -- all unit tests green.
- [ ] No regressions in existing tests.

### 3. Code Quality
- [ ] Unit test coverage of changed/new code >= 90%.
- [ ] Code follows PEP 8 and project coding style (CLAUDE.md).
- [ ] Type annotations on all function signatures.
- [ ] Pythonic best practices are followed.
- [ ] Files follow the "many small files" principle (200-400 LOC typical).

### 4. Review Resolution
- [ ] All of D's review comments have been addressed.
- [ ] No open high-priority or blocking issues remain.

**If any check fails**, A sends specific feedback to the responsible team member(s) and the cycle repeats. The iteration continues until all checks pass.

## Execution Instructions

When this skill is invoked:

1. **Start as A**: Analyze the user's request. Break it into requirements. Write an implementation plan. Use `AskUserQuestion` to get user approval.
2. **Run B**: Launch an architect agent with A's approved plan. Collect B's design output.
3. **Run C and D iteratively**:
   - Launch C to implement based on B's design.
   - Launch D to review C's implementation.
   - If D has comments, feed them back to C. Repeat until D approves.
4. **A's final review**: Run build, tests, and coverage checks. Review plan adherence and code quality.
5. **Iterate if needed**: If A finds issues, send them back to the appropriate team member(s) and repeat steps 2-4 as needed.
6. **Complete**: Present a summary of all changes to the user.

## Notes

- All team members should reference CLAUDE.md for project conventions.
- D should follow the `code-review/SKILLS.md` skill for review format and dimensions.
- The user interacts only with A. B, C, D work autonomously within the workflow.
- Prefer parallel execution where possible (e.g., B's design can begin immediately after A's plan is approved).