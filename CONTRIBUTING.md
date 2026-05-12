# Contributing to claude-session-watchdog

Thanks for your interest! This guide covers how to report issues, suggest features, and submit code.

## Reporting Bugs

Open a [GitHub Issue](https://github.com/your-org/claude-session-watchdog/issues/new) and include:

- **Watchdog version**: run `./scripts/watchdog.sh status` or check `VERSION` near the top of `watchdog.sh`
- **macOS version**: `sw_vers` output
- **tmux version**: `tmux -V` output
- **Steps to reproduce** -- exact commands you ran
- **Expected vs actual behavior**
- **Relevant log lines**: `./scripts/watchdog.sh log 100` (redact any sensitive paths or tokens)

## Suggesting Features

Open a GitHub Issue with the label `enhancement`. Describe the use case and why existing features don't cover it.

## Development Setup

```bash
# Clone
git clone https://github.com/your-org/claude-session-watchdog.git
cd claude-session-watchdog

# Configure (optional -- only needed for notification / LLM features)
cp .env.example .env
# Edit .env with your credentials

# Verify tests pass
python3 -m pytest tests/ -q
```

### Requirements

- **Python 3.8+** (for classifiers and notification scripts)
- **pytest** (test runner)
- **tmux** (integration tests depend on it)
- **macOS** (uses `osascript`, `md5`; Linux would need adaptations)

## Running Tests

```bash
# Full suite (135 tests)
python3 -m pytest tests/ -q

# With verbose output
python3 -m pytest tests/ -v

# Single test file
python3 -m pytest tests/test_notify.py -q
```

Tests cover notification formatting, idle classification, event logging, JSONL parsing, and the full detection pipeline via shell sourcing.

## Code Style

| Area | Style |
|---|---|
| Shell scripts (`scripts/*.sh`) | Bash, 2-space indent, `snake_case` variables |
| Python classifiers (`scripts/*.py`) | Python 3, 4-space indent, `snake_case` functions/vars |
| Templates (`scripts/*.json`) | Standard JSON, 2-space indent |

Run a quick syntax check before submitting:

```bash
bash -n scripts/watchdog.sh
python3 -m py_compile scripts/classify_idle.py
python3 -m py_compile scripts/notify.py
python3 -m py_compile scripts/llm_utils.py
```

## Pull Request Process

1. **Fork** the repository.
2. **Create a branch** from `main`:
   ```bash
   git checkout -b fix/my-bug-fix
   # or
   git checkout -b feat/my-new-feature
   ```
3. **Make changes** and add/update tests as needed.
4. **Run the full test suite** and ensure all 135 tests pass:
   ```bash
   python3 -m pytest tests/ -q
   ```
5. **Commit** with a descriptive message (see format below).
6. **Push** to your fork and open a Pull Request against `main`.

### Commit Message Format

```
type: short summary

(optional longer description)
```

**Types:**

- `feat:` -- new feature
- `fix:` -- bug fix
- `docs:` -- documentation only
- `refactor:` -- code restructuring with no behavior change
- `test:` -- adding or updating tests
- `chore:` -- maintenance, tooling, CI

Examples:

```
feat: add Slack webhook notification channel
fix: prevent false stuck detection during long compilation
docs: clarify launchd setup steps
```

## Questions?

Open a GitHub Issue with the label `question`.
