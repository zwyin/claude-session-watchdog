# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [2.0.5] - 2026-05-11

### Fixed

- Eliminate log doubling by removing stdout echo from `log()` function
- Suppress remaining SIGPIPE signals in notification pipeline
- Prevent empty Feishu notification cards with body validation and sanitization
- Sanitize OSC (Operating System Command) escape sequences from captured pane output
- Fix sed typo that broke all event logging

## [2.0.4] - 2026-05-09

### Added

- V3 LLM prompt returning structured `confidence` and `trigger` fields in notifications
- Context logging for LLM audit trail
- LLM-only classification as default mode

### Fixed

- Dual-process startup race condition causing duplicate daemon processes
- Shared LLM module (`llm_utils.py`) not loading correctly in subprocess
- Launchd-first startup ordering -- daemon mode now preferred over background start
- Notification deduplication for rapidly repeated stuck events
- Event name mapping inconsistency
- Stale test data causing false test failures
- Export `.env` variables for `python3` subprocess (LLM classification was silently timing out)
- Morning/evening report per-session detail formatting
- Idle notification formatting -- preserve newlines, rename `trigger` to `reasoning`
- Complete `trigger` to `reasoning` rename, clean dead code

## [2.0.3] - 2026-05-07

### Added

- Enlarged capture window for idle session analysis
- LLM audit mechanism for reviewing past detection events

### Fixed

- Code review fixes for stale docstrings and duplicate regex patterns

## [2.0.2] - 2026-05-07

### Added

- Morning report (08:00) and evening report (22:00) with per-session breakdowns
- Idle session classification with keyword-based pattern matching
- V3 prompt with confidence and trigger fields

### Fixed

- Code review fixes for report formatting and test coverage
- `stop_daemon` now kills all watchdog processes, not just the recorded PID
- Resolve `{confidence}/{trigger}` placeholder bug and JSON parsing failure
- Remove false `COMPLETE_PATTERNS` entry that caused misclassification

## [2.0.1] - 2026-05-06

### Added

- Dual-format LLM support (Anthropic and OpenAI-compatible endpoints) via `WATCHDOG_LLM_FORMAT` config
- Modular Python scripts for classification and notification (`classify_idle.py`, `notify.py`, `llm_utils.py`)
- 81 tests (later expanded to 108), 8-type notification test suite with `[测试]` tag
- New CLI commands: `sessions`, `health`, `log`, `test-notify`, `daily-summary`
- Idle session classification and smart notification

### Fixed

- `read -r` truncation in idle classification -- `summary`/`last_lines` now captured correctly
- JSON extraction and exclude logic in idle classifier
- HMAC signing, double-log, JSON injection, EXIT trap issues
- Cross-platform `md5`/`stat` compatibility
- Increase LLM `max_tokens` to 2000 and timeout to 60s

## [2.0.0] - 2026-05-06

### Added

- **Triple detection**: screen-content hash (timer-noise filtered), JSONL session-log last record, and output-token stagnation -- all three must agree before declaring stuck
- **Auto-intervention**: sends Ctrl-C followed by a continue prompt after 15 minutes of inactivity
- **Feishu / Lark notifications**: HMAC-signed webhook with 8 template types
- **macOS local notifications**: native `osascript` alerts as zero-config fallback
- Background daemon mode with PID tracking and process locking
- `launchd` auto-start support
- Open-source ready with MIT license

## [1.x] - 2026-05 (pre-release)

### Added

- Basic stuck detection via screen-content hash comparison
- Hash-based monitoring with timer-noise filtering
- Single detection pass and simple logging
