# Python Code Review Checklist

## Correctness

- Off-by-one errors in loops, slicing, range
- Mutable default arguments (`def f(x=[])`)
- Unhandled exceptions that silently swallow errors (`except: pass`, bare `except Exception`)
- Race conditions in async code (shared state without locks, missing `await`)
- Resource leaks (files, connections, sockets not closed / not using context managers)
- Boolean logic errors (De Morgan's law violations, short-circuit evaluation assumptions)
- `is` vs `==` confusion (especially `is None` vs `== None`, interned int gotchas)
- Iteration over dict while mutating it
- Shadowing builtins (`id`, `type`, `list`, `dict`, `input`, `open`)
- Missing `return` in branches that should produce a value

## Security

- SQL injection (string formatting in queries instead of parameterized)
- Command injection via `subprocess.call(shell=True)` with user input
- Path traversal (`../` in user-supplied file paths, missing `Path.resolve()` checks)
- Unsafe deserialization (`pickle.loads`, `yaml.load` without `Loader=SafeLoader`)
- Hardcoded secrets, tokens, passwords
- SSRF through unvalidated URLs in `httpx`/`requests` calls
- Missing input validation at system boundaries
- Overly broad file permissions
- Use of `eval()` or `exec()` with external input
- Logging sensitive data (passwords, tokens, PII)

## Performance

- N+1 query patterns (loop of individual DB/API calls)
- Quadratic string concatenation in loops (use `"".join()`)
- Unnecessary list materialization (`list(generator)` when iteration suffices)
- Missing `__slots__` on high-volume dataclasses
- Sync blocking calls inside async functions (should use `asyncio.to_thread()`)
- Repeated identical computations that should be cached
- Unbounded in-memory collections (missing size limits, pagination)
- Creating new `httpx.AsyncClient` per request instead of reusing

## Design & Maintainability

- Functions doing too many things (> 50 lines is a smell)
- Deep nesting (> 3 levels of if/for/try)
- Magic numbers / strings without named constants
- Dead code (unreachable branches, unused imports, commented-out code)
- Inconsistent error handling strategy within the same module
- Missing type hints on public API functions
- Circular imports
- God classes (class with 10+ methods or 200+ lines)
- Premature abstraction (interface with single implementation)
- Copy-paste duplication (3+ identical blocks)

## Async-Specific (Python asyncio)

- Missing `await` on coroutine calls (coroutine created but never awaited)
- `asyncio.sleep(0)` used as yield point without understanding implications
- Fire-and-forget tasks without error handling (`asyncio.create_task` without storing reference)
- Blocking I/O in async context (`open()`, `time.sleep()`, synchronous `requests`)
- Mixing threading and asyncio without proper synchronization
- `CancelledError` not properly re-raised after cleanup

## API & Interface

- Breaking changes to public APIs without version bump
- Inconsistent naming conventions within the same module
- Missing or misleading docstrings on public functions
- Return type inconsistency (sometimes returns None, sometimes raises)
- Accepting overly broad types (`Any`) where specific types are known
- Non-obvious parameter ordering (positional args that are easy to swap)
