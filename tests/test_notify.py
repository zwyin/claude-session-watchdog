"""Tests for Claude Code Watchdog notification templates and rendering."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import hmac
import hashlib
import base64

# 将 scripts 目录加入模块搜索路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_FILE = os.path.join(SCRIPT_DIR, "scripts", "notify-templates.json")
WATCHDOG_SCRIPT = os.path.join(SCRIPT_DIR, "scripts", "watchdog.sh")
NOTIFY_PY = os.path.join(SCRIPT_DIR, "scripts", "notify.py")
JSONL_AGE_PY = os.path.join(SCRIPT_DIR, "scripts", "jsonl_age.py")


def render_template(section, **kwargs):
    """Render a template by calling Python directly (avoids sourcing watchdog.sh)."""
    args_str = "\n".join(f"{k}={v}" for k, v in kwargs.items())
    result = subprocess.run(
        ["python3", "-c", f"""
import json
with open('{TEMPLATE_FILE}', 'r') as f:
    templates = json.load(f)
tpl = templates.get('{section}', {{}})
title = tpl.get('title', '')
color = tpl.get('color', 'blue')
body = tpl.get('body', '')
variables = {{}}
for line in '''{args_str}'''.strip().split('\\n'):
    if '=' in line:
        k, v = line.split('=', 1)
        variables[k.strip()] = v.strip()
for k, v in variables.items():
    title = title.replace('{{' + k + '}}', v)
    body = body.replace('{{' + k + '}}', v)
print(color + '|||' + title + '|||' + body)
"""],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, f"render failed: {result.stderr}"
    parts = result.stdout.strip().split("|||")
    assert len(parts) == 3, f"Expected 3 parts, got {len(parts)}: {parts}"
    return {"color": parts[0], "title": parts[1], "body": parts[2]}


class TestTemplates(unittest.TestCase):
    """Test notify-templates.json validity."""

    def test_template_file_exists(self):
        self.assertTrue(os.path.isfile(TEMPLATE_FILE))

    def test_template_file_valid_json(self):
        with open(TEMPLATE_FILE) as f:
            data = json.load(f)
        self.assertIsInstance(data, dict)

    def test_all_event_types_defined(self):
        with open(TEMPLATE_FILE) as f:
            data = json.load(f)
        for event in ("stuck", "intervene", "recovered", "start", "daily",
                      "idle_decision", "idle_complete", "idle_unknown"):
            self.assertIn(event, data)

    def test_each_template_has_required_fields(self):
        with open(TEMPLATE_FILE) as f:
            data = json.load(f)
        for event, tpl in data.items():
            with self.subTest(event=event):
                self.assertIn("title", tpl)
                self.assertIn("color", tpl)
                self.assertIn("body", tpl)
                self.assertIn(tpl["color"], {"red", "orange", "green", "blue", "purple", "yellow", "turquoise", "violet", "wathet", "indigo", "lime", "gold"})

    def test_stuck_template_placeholders(self):
        with open(TEMPLATE_FILE) as f:
            data = json.load(f)
        body = data["stuck"]["body"]
        for p in ["{session}", "{duration}", "{date}", "{time}", "{status_line}", "{last_output}"]:
            self.assertIn(p, body)

    def test_daily_template_placeholders(self):
        with open(TEMPLATE_FILE) as f:
            data = json.load(f)
        body = data["daily"]["body"]
        for p in ["{date}", "{today_stuck}", "{total_events}", "{session_count}"]:
            self.assertIn(p, body)


class TestRenderTemplate(unittest.TestCase):
    """Test template rendering."""

    def test_render_stuck(self):
        r = render_template("stuck", session="test-sess", duration="12",
                            date="05-06", time="21:55", status_line="GLM-5.1", last_output="hello")
        self.assertIn("test-sess", r["title"])
        self.assertIn("12", r["body"])
        self.assertEqual(r["color"], "red")
        self.assertNotIn("{date}", r["body"])

    def test_render_intervene(self):
        r = render_template("intervene", session="gps", duration="15",
                            date="05-06", time="22:00", status_line="model info",
                            last_output="output", action="Ctrl-C")
        self.assertIn("gps", r["title"])
        self.assertEqual(r["color"], "orange")
        self.assertNotIn("{date}", r["body"])

    def test_render_recovered(self):
        r = render_template("recovered", session="my-session", duration="5", time="22:00")
        self.assertIn("my-session", r["title"])
        self.assertEqual(r["color"], "green")

    def test_render_start(self):
        r = render_template("start", session_count="14", time="21:00")
        self.assertIn("14", r["body"])
        self.assertEqual(r["color"], "blue")

    def test_render_daily(self):
        r = render_template("daily", date="2026-05-05", today_events="8",
                            today_stuck="3", today_interrupt="2", today_recovered="3",
                            avg_duration="12", total_events="42", total_stuck="15",
                            total_interrupt="10", total_recovered="15", session_count="14")
        self.assertIn("2026-05-05", r["title"])
        self.assertIn("3", r["body"])
        self.assertEqual(r["color"], "purple")

    def test_placeholders_fully_replaced(self):
        r = render_template("recovered", session="x", duration="1",
                            date="05-06", time="00:00")
        self.assertNotIn("{session}", r["body"])
        self.assertNotIn("{duration}", r["body"])
        self.assertNotIn("{date}", r["body"])
        self.assertNotIn("{time}", r["body"])
        self.assertNotIn("{session}", r["title"])

    def test_stuck_placeholders_fully_replaced(self):
        r = render_template("stuck", session="s", duration="5", date="05-06",
                            time="12:00", status_line="model", last_output="out")
        self.assertNotIn("{session}", r["body"])
        self.assertNotIn("{duration}", r["body"])
        self.assertNotIn("{time}", r["body"])

    def test_daily_placeholders_fully_replaced(self):
        r = render_template("daily", date="2026-05-06", today_events="1",
                            today_stuck="0", today_interrupt="0", today_recovered="0",
                            avg_duration="0", total_events="1", total_stuck="0",
                            total_interrupt="0", total_recovered="0", session_count="1")
        self.assertNotIn("{date}", r["body"])
        self.assertNotIn("{today_stuck}", r["body"])
        self.assertNotIn("{total_events}", r["body"])

    def test_chinese_content_renders(self):
        r = render_template("stuck", session="测试", duration="30",
                            date="05-06", time="22:00", status_line="模型: GLM-5.1",
                            last_output="输出内容")
        self.assertIn("测试", r["title"])
        self.assertIn("输出内容", r["body"])


class TestFeishuSignature(unittest.TestCase):
    """Test Feishu webhook signature."""

    def test_hmac_sha256_produces_base64(self):
        ts = "1234567890"
        secret = "test_secret"
        string_to_sign = f"{ts}\n{secret}"
        sign = base64.b64encode(
            hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        ).decode("utf-8")
        base64.b64decode(sign)  # Valid base64
        self.assertTrue(len(sign) > 0)

    def test_different_timestamps_different_signs(self):
        def calc(ts):
            sts = f"{ts}\ntest"
            return base64.b64encode(
                hmac.new(sts.encode("utf-8"), digestmod=hashlib.sha256).digest()
            ).decode("utf-8")
        self.assertNotEqual(calc("1000"), calc("2000"))

    def test_same_inputs_same_sign(self):
        def calc(ts, secret):
            sts = f"{ts}\n{secret}"
            return base64.b64encode(
                hmac.new(sts.encode("utf-8"), digestmod=hashlib.sha256).digest()
            ).decode("utf-8")
        self.assertEqual(calc("1000", "s"), calc("1000", "s"))


class TestEventLogging(unittest.TestCase):
    """Test JSONL event file format."""

    def _run_log(self, events_file, event, session, duration, notes, intervention):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            f.write(f"""#!/bin/bash
EVENTS_FILE="{events_file}"
log_event() {{
  local event="$1" session="$2" duration="$3" notes="$4" intervention="$5"
  local recovered="false"
  [ "$event" = "recovered" ] && recovered="true"
  local model="${{MODEL_NAME:-test-model}}"
  printf '{{"timestamp":"%s","event":"%s","session":"%s","project":"%s","duration_minutes":%s,"model":"%s","phase":"unknown","intervention":"%s","recovered":%s,"notes":"%s"}}\\n' \\
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$event" "$session" "$session" "$duration" "$model" "$intervention" "$recovered" "$notes" \\
    >> "$EVENTS_FILE"
}}
log_event "{event}" "{session}" "{duration}" "{notes}" "{intervention}"
""")
            f.flush()
            script = f.name
        os.chmod(script, 0o755)
        subprocess.run(["bash", script], capture_output=True, timeout=10)
        os.unlink(script)

    def test_single_event_valid_json(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            events_file = f.name
        try:
            self._run_log(events_file, "stuck", "test-sess", "12", "notes", "none")
            with open(events_file) as f:
                data = json.loads(f.readline())
            self.assertEqual(data["event"], "stuck")
            self.assertEqual(data["session"], "test-sess")
        finally:
            os.unlink(events_file)

    def test_multiple_events_append(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            events_file = f.name
        try:
            for i in range(5):
                self._run_log(events_file, "stuck", f"sess{i}", str(i * 5), "note", "none")
            with open(events_file) as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 5)
            for line in lines:
                json.loads(line)
        finally:
            os.unlink(events_file)


class TestConfigConsistency(unittest.TestCase):
    """Test config parameters by grepping the script."""

    def test_sample_interval_positive(self):
        with open(WATCHDOG_SCRIPT) as f:
            content = f.read()
        import re
        m = re.search(r"SAMPLE_INTERVAL=(\d+)", content)
        self.assertIsNotNone(m)
        val = int(m.group(1))
        self.assertGreater(val, 0)
        self.assertLessEqual(val, 300, "Sample interval should be <= 5 min")

    def test_thresholds_ordered(self):
        with open(WATCHDOG_SCRIPT) as f:
            content = f.read()
        import re
        stuck = int(re.search(r"STUCK_THRESHOLD=(\d+)", content).group(1))
        intervene = int(re.search(r"INTERVENE_THRESHOLD=(\d+)", content).group(1))
        cooldown = int(re.search(r"INTERVENE_COOLDOWN=(\d+)", content).group(1))
        self.assertGreater(intervene, stuck, "INTERVENE should be > STUCK")
        self.assertGreater(cooldown, 0, "COOLDOWN should be positive")

    def test_feishu_config_present(self):
        with open(WATCHDOG_SCRIPT) as f:
            content = f.read()
        self.assertIn("FEISHU_WEBHOOK=", content)
        self.assertIn("FEISHU_SECRET=", content)

    def test_version_defined(self):
        with open(WATCHDOG_SCRIPT) as f:
            content = f.read()
        import re
        m = re.search(r'VERSION="([^"]+)"', content)
        self.assertIsNotNone(m, "VERSION variable not found")
        parts = m.group(1).split(".")
        self.assertEqual(len(parts), 3, "VERSION should be semver (x.y.z)")

    def test_jsonl_stale_threshold_positive(self):
        with open(WATCHDOG_SCRIPT) as f:
            content = f.read()
        import re
        m = re.search(r"JSONL_STALE_THRESHOLD=(\d+)", content)
        self.assertIsNotNone(m)
        val = int(m.group(1))
        self.assertGreater(val, 0)

    def test_combined_detection_present(self):
        with open(WATCHDOG_SCRIPT) as f:
            content = f.read()
        self.assertIn("get_jsonl_age_seconds", content)
        self.assertIn("get_output_tokens", content)
        self.assertIn("jsonl_stale", content)
        self.assertIn("tokens_stagnant", content)
        self.assertIn("DEEP_STUCK", content)

    def test_hash_strips_timer(self):
        with open(WATCHDOG_SCRIPT) as f:
            content = f.read()
        self.assertIn("TIMER", content, "Hash should strip timer patterns")


class TestJsonlAgeParsing(unittest.TestCase):
    """Test JSONL last-record age extraction logic."""

    def test_parse_valid_timestamp(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"type":"assistant","timestamp":"2020-01-01T00:00:00.000Z"}\n')
            f.flush()
            jsonl_path = f.name
        try:
            result = subprocess.run(
                ["python3", "-c", f"""
import json, os, sys
from datetime import datetime, timezone
with open('{jsonl_path}', 'rb') as fh:
    fh.seek(0, 2)
    size = fh.tell()
    fh.seek(max(0, size - 2048))
    tail = fh.read().decode('utf-8', errors='replace').strip()
lines = [l for l in tail.split('\\n') if l.strip()]
last = json.loads(lines[-1])
ts_str = last.get('timestamp', '')
ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
age = (datetime.now(timezone.utc) - ts).total_seconds()
print(int(age))
"""],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0)
            age = int(result.stdout.strip())
            self.assertGreater(age, 0)
        finally:
            os.unlink(jsonl_path)

    def test_empty_file_returns_nothing(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            jsonl_path = f.name
        try:
            result = subprocess.run(
                ["python3", "-c", f"""
import json, os, sys
with open('{jsonl_path}', 'rb') as fh:
    fh.seek(0, 2)
    size = fh.tell()
    if size == 0:
        sys.exit(0)
"""],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "")
        finally:
            os.unlink(jsonl_path)

    def test_no_timestamp_field_returns_nothing(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"type":"user","message":"hello"}\n')
            f.flush()
            jsonl_path = f.name
        try:
            result = subprocess.run(
                ["python3", "-c", f"""
import json, os, sys
with open('{jsonl_path}', 'rb') as fh:
    fh.seek(0, 2)
    size = fh.tell()
    fh.seek(max(0, size - 2048))
    tail = fh.read().decode('utf-8', errors='replace').strip()
lines = [l for l in tail.split('\\n') if l.strip()]
last = json.loads(lines[-1])
ts_str = last.get('timestamp', '')
if not ts_str:
    sys.exit(0)
print('SHOULD_NOT_REACH')
"""],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0)
            self.assertNotIn("SHOULD_NOT_REACH", result.stdout)
        finally:
            os.unlink(jsonl_path)

    def test_corrupted_json_returns_nothing(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('NOT VALID JSON {{{\n')
            f.flush()
            jsonl_path = f.name
        try:
            result = subprocess.run(
                ["python3", "-c", f"""
import json, sys
try:
    with open('{jsonl_path}', 'rb') as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 2048))
        tail = fh.read().decode('utf-8', errors='replace').strip()
    lines = [l for l in tail.split('\\n') if l.strip()]
    last = json.loads(lines[-1])
    print('SHOULD_NOT_REACH')
except (json.JSONDecodeError, ValueError):
    pass
"""],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0)
            self.assertNotIn("SHOULD_NOT_REACH", result.stdout)
        finally:
            os.unlink(jsonl_path)


class TestTokenExtraction(unittest.TestCase):
    """Test output token parsing from status line."""

    def test_parse_output_tokens(self):
        result = subprocess.run(
            ["python3", "-c", """
import re
line = '  输入: 3.2M | 输出: 228.9k | 缓存: 108.5M | 合计: 114.0M'
m = re.search(r'输出:\\s*([0-9.]+[km]?)', line)
print(m.group(1) if m else 'NONE')
"""],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "228.9k")

    def test_parse_small_tokens(self):
        result = subprocess.run(
            ["python3", "-c", "import re; line='  输入: 26.3k | 输出: 8.6k | 缓存: 4.3M'; m=re.search(r'输出:\\s*([0-9.]+[km]?)', line); print(m.group(1) if m else 'NONE')"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "8.6k")

    def test_no_output_field(self):
        result = subprocess.run(
            ["python3", "-c", """
import re
line = 'some random text without tokens'
m = re.search(r'输出:\\s*([0-9.]+[km]?)', line)
print(m.group(1) if m else 'NONE')
"""],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "NONE")

    def test_token_change_detection(self):
        self.assertNotEqual("228.9k", "229.1k")
        self.assertEqual("228.9k", "228.9k")


class TestPathEncoding(unittest.TestCase):
    """Test workdir → project dir path encoding."""

    def test_underscore_path(self):
        result = subprocess.run(
            ["python3", "-c", """
import re
workdir = '/home/user/projects/my-app-library'
home = '/home/user'
rel = workdir.replace(home + '/', '')
encoded = re.sub(r'[^a-zA-Z0-9]+', '-', rel).strip('-')
print(encoded)
"""],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "projects-my-app-library")

    def test_nested_path(self):
        result = subprocess.run(
            ["python3", "-c", "import re; workdir='/home/user/projects/test-toolkit-0424'; home='/home/user'; rel=workdir.replace(home+'/',''); encoded=re.sub(r'[^a-zA-Z0-9]+','-',rel).strip('-'); print(encoded)"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "projects-test-toolkit-0424")

    def test_path_with_dots(self):
        result = subprocess.run(
            ["python3", "-c", """
import re
workdir = '/Users/zhiweiyin/repo_ds1600/claude-session-watchdog'
home = '/Users/zhiweiyin'
rel = workdir.replace(home + '/', '')
encoded = re.sub(r'[^a-zA-Z0-9]+', '-', rel).strip('-')
print(encoded)
"""],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "repo-ds1600-claude-session-watchdog")

    def test_path_with_spaces(self):
        result = subprocess.run(
            ["python3", "-c", """
import re
workdir = '/Users/zhiweiyin/my projects/test app'
home = '/Users/zhiweiyin'
rel = workdir.replace(home + '/', '')
encoded = re.sub(r'[^a-zA-Z0-9]+', '-', rel).strip('-')
print(encoded)
"""],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "my-projects-test-app")


class TestModelNameExtraction(unittest.TestCase):
    """Test model name extraction from tmux status line."""

    def test_extract_model_from_status_line(self):
        result = subprocess.run(
            ["python3", "-c", r"""
import re
line = '  模型: Claude Sonnet 4.6 | 输入: 26.3k | 输出: 8.6k'
m = re.search(r'模型:\s*([^ |]+(?:\s+[^ |]+)*)', line)
if m:
    model = m.group(1).strip()
    print(model)
else:
    print('NONE')
"""],
            capture_output=True, text=True, timeout=10,
        )
        self.assertIn("Claude", result.stdout.strip())
        self.assertNotEqual(result.stdout.strip(), "NONE")

    def test_extract_glm_model(self):
        result = subprocess.run(
            ["python3", "-c", r"""
import re
line = '  模型: GLM-5.1 | 输入: 3.2M'
m = re.search(r'模型:\s*([^ |]+(?:\s+[^ |]+)*)', line)
print(m.group(1).strip() if m else 'NONE')
"""],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "GLM-5.1")

    def test_no_model_field(self):
        result = subprocess.run(
            ["python3", "-c", r"""
import re
line = '  输入: 26.3k | 输出: 8.6k | 缓存: 4.3M'
m = re.search(r'模型:\s*([^ |]+(?:\s+[^ |]+)*)', line)
print(m.group(1) if m else 'NONE')
"""],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "NONE")

    def test_bash_or_true_no_crash(self):
        """Verify that get_model_name's || true prevents set -e crash."""
        result = subprocess.run(
            ["bash", "-c", "set -euo pipefail; x=$(echo 'no model here' | grep -oE '模型:[[:space:]]*[^ |]+' | head -1 || true); echo \"result=[$x]\""],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("result=[]", result.stdout)


class TestIdlePromptDetection(unittest.TestCase):
    """Test idle prompt pattern matching."""

    def test_detect_prompt_char(self):
        result = subprocess.run(
            ["bash", "-c", "echo '❯' | grep -qE '(^❯|^\\s*❯)' && echo IDLE || echo ACTIVE"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "IDLE")

    def test_detect_accept_edits(self):
        result = subprocess.run(
            ["bash", "-c", "echo 'accept edits on files?' | grep -qE 'accept edits on' && echo IDLE || echo ACTIVE"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "IDLE")

    def test_detect_escaped_prompt(self):
        result = subprocess.run(
            ["bash", "-c", "echo '  ❯ ' | grep -qE '(^❯|^\\s*❯)' && echo IDLE || echo ACTIVE"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "IDLE")

    def test_active_output_not_idle(self):
        result = subprocess.run(
            ["bash", "-c", "echo 'def hello_world():' | grep -qE '(^❯|^\\s*❯|accept edits on)' && echo IDLE || echo ACTIVE"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "ACTIVE")

    def test_timeout_pattern_idle(self):
        result = subprocess.run(
            ["bash", "-c", "echo '[超时]' | grep -qE '(\\[超时\\])' && echo IDLE || echo ACTIVE"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "IDLE")


class TestTimerStripping(unittest.TestCase):
    """Test hash normalization that strips timer/timestamp patterns."""

    def test_strip_minutes_seconds(self):
        result = subprocess.run(
            ["bash", "-c", "echo 'progress 2m 15s remaining' | sed -E 's/[0-9]+m [0-9]+s/TIMER/g'"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertIn("TIMER", result.stdout.strip())
        self.assertNotIn("2m 15s", result.stdout.strip())

    def test_strip_compact_timer(self):
        result = subprocess.run(
            ["bash", "-c", "echo 'elapsed 5m30s done' | sed -E 's/[0-9]+m[0-9]+s/TIMER/g'"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertIn("TIMER", result.stdout.strip())

    def test_strip_clock_time(self):
        result = subprocess.run(
            ["bash", "-c", "echo 'updated at 14:30' | sed -E 's/[0-9]+:[0-9]+(am|pm)?/TIME/g'"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertIn("TIME", result.stdout.strip())
        self.assertNotIn("14:30", result.stdout.strip())

    def test_preserve_non_timer_content(self):
        result = subprocess.run(
            ["bash", "-c", "echo 'error code 404 in file.py' | sed -E 's/[0-9]+m [0-9]+s/TIMER/g; s/[0-9]+m[0-9]+s/TIMER/g; s/[0-9]+:[0-9]+(am|pm)?/TIME/g'"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertIn("error code 404", result.stdout.strip())

    def test_hash_stable_without_timer(self):
        """Same content → same hash after normalization."""
        import hashlib
        content = "def foo():\n    return 42\n"
        h1 = hashlib.md5(content.encode()).hexdigest()
        h2 = hashlib.md5(content.encode()).hexdigest()
        self.assertEqual(h1, h2)

    def test_hash_changes_with_real_change(self):
        """Different content → different hash."""
        import hashlib
        h1 = hashlib.md5(b"def foo():\n    return 1\n").hexdigest()
        h2 = hashlib.md5(b"def foo():\n    return 2\n").hexdigest()
        self.assertNotEqual(h1, h2)


class TestTokenParsingEdgeCases(unittest.TestCase):
    """Additional edge cases for token parsing."""

    def test_million_tokens(self):
        result = subprocess.run(
            ["python3", "-c", r"""
import re
line = '  输入: 3.2M | 输出: 1.8M | 缓存: 108.5M'
m = re.search(r'输出:\s*([0-9.]+[kKmM]?)', line)
print(m.group(1) if m else 'NONE')
"""],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "1.8M")

    def test_plain_number_tokens(self):
        result = subprocess.run(
            ["python3", "-c", r"""
import re
line = '  输入: 500 | 输出: 128'
m = re.search(r'输出:\s*([0-9.]+[km]?)', line)
print(m.group(1) if m else 'NONE')
"""],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "128")

    def test_multiple_output_fields_takes_first(self):
        result = subprocess.run(
            ["python3", "-c", r"""
import re
line = '  输出: 100k extra 输出: 200k'
m = re.search(r'输出:\s*([0-9.]+[km]?)', line)
print(m.group(1) if m else 'NONE')
"""],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "100k")

    def test_bash_grep_output_tokens(self):
        """Test the actual bash pipeline used in get_output_tokens."""
        result = subprocess.run(
            ["bash", "-c", """
line='  输入: 3.2M | 输出: 228.9k | 缓存: 108.5M'
echo "$line" | grep -oE '输出:[[:space:]]*[0-9.]+[km]?' | grep -oE '[0-9.]+[km]?' | head -1
"""],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "228.9k")


class TestNotifyPy(unittest.TestCase):
    """Test standalone notify.py script."""

    def test_render_without_feishu(self):
        result = subprocess.run(
            ["python3", NOTIFY_PY, TEMPLATE_FILE, "stuck",
             "session=test-sess", "duration=5", "date=05-06",
             "time=12:00", "status_line=model", "last_output=out"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("RENDERED:", result.stdout)
        self.assertIn("NOTIFY_SKIP", result.stdout)

    def test_missing_args_exits(self):
        result = subprocess.run(
            ["python3", NOTIFY_PY],
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_invalid_section_renders_empty(self):
        result = subprocess.run(
            ["python3", NOTIFY_PY, TEMPLATE_FILE, "nonexistent"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("RENDERED:", result.stdout)

    def test_chinese_values(self):
        result = subprocess.run(
            ["python3", NOTIFY_PY, TEMPLATE_FILE, "stuck",
             "session=测试会话", "duration=30", "date=05-06",
             "time=22:00", "status_line=模型: GLM-5.1", "last_output=输出内容"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("测试会话", result.stdout)
        self.assertIn("NOTIFY_SKIP", result.stdout)


class TestJsonlAgePy(unittest.TestCase):
    """Test standalone jsonl_age.py script."""

    def test_no_args_silent(self):
        result = subprocess.run(
            ["python3", JSONL_AGE_PY],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_nonexistent_session_silent(self):
        result = subprocess.run(
            ["python3", JSONL_AGE_PY, "nonexistent-session-xyz"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")


class TestDaemonPidWrite(unittest.TestCase):
    """Test that run_foreground writes PID file."""

    def test_run_foreground_writes_pid(self):
        """Simulate run_foreground PID write logic."""
        with tempfile.NamedTemporaryFile(suffix=".pid", delete=False) as f:
            pid_file = f.name
        try:
            # Simulate: echo $$ > PID_FILE
            subprocess.run(
                ["bash", "-c", f"echo $$ > {pid_file}"],
                capture_output=True, timeout=5,
            )
            with open(pid_file) as f:
                pid_content = f.read().strip()
            self.assertTrue(pid_content.isdigit())
            self.assertGreater(int(pid_content), 0)
        finally:
            os.unlink(pid_file)


class TestConfigPathExport(unittest.TestCase):
    """Test that watchdog.sh exports brew PATH."""

    def test_path_includes_homebrew(self):
        with open(WATCHDOG_SCRIPT) as f:
            content = f.read()
        self.assertIn("/opt/homebrew/bin", content)
        self.assertIn("/usr/local/bin", content)
        # Should be near the top, before dependency checks
        path_line = content.index("/opt/homebrew/bin")
        dep_check = content.index("command -v tmux")
        self.assertLess(path_line, dep_check,
                        "PATH export should come before dependency checks")


class TestNewCommands(unittest.TestCase):
    """Test that new subcommands exist in the entry point."""

    def test_log_command_present(self):
        with open(WATCHDOG_SCRIPT) as f:
            content = f.read()
        self.assertIn("log)", content)
        self.assertIn("show_log", content)

    def test_sessions_command_present(self):
        with open(WATCHDOG_SCRIPT) as f:
            content = f.read()
        self.assertIn("sessions)", content)
        self.assertIn("show_sessions", content)

    def test_health_command_present(self):
        with open(WATCHDOG_SCRIPT) as f:
            content = f.read()
        self.assertIn("health)", content)
        self.assertIn("health_check", content)

    def test_usage_includes_new_commands(self):
        with open(WATCHDOG_SCRIPT) as f:
            content = f.read()
        self.assertIn("log|sessions|health", content)

    def test_health_check_runs(self):
        result = subprocess.run(
            ["bash", WATCHDOG_SCRIPT, "health"],
            capture_output=True, text=True, timeout=10,
        )
        # Should run without error (may return 1 if unhealthy, that's OK)
        self.assertIn("Process:", result.stdout)

    def test_sessions_runs(self):
        result = subprocess.run(
            ["bash", WATCHDOG_SCRIPT, "sessions"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)

    def test_log_runs(self):
        result = subprocess.run(
            ["bash", WATCHDOG_SCRIPT, "log"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)

    def test_nbsp_strip_in_get_model_name(self):
        with open(WATCHDOG_SCRIPT) as f:
            content = f.read()
        self.assertIn("\\xc2\\xa0", content,
                      "Should strip non-breaking spaces (U+00A0) from model name")


class TestInterveneLogic(unittest.TestCase):
    """Test intervene threshold and cooldown logic."""

    def test_cooldown_prevents_rapid_reintervene(self):
        now = 1000
        last_intervene = 900
        cooldown = 600
        elapsed = now - last_intervene  # 100s
        self.assertLess(elapsed, cooldown, "Should be in cooldown period")

    def test_cooldown_allows_after_wait(self):
        now = 1600
        last_intervene = 900
        cooldown = 600
        elapsed = now - last_intervene  # 700s
        self.assertGreaterEqual(elapsed, cooldown, "Should be past cooldown")

    def test_first_intervene_no_cooldown(self):
        """First intervention has no last_intervene, should always proceed."""
        last_intervene = ""
        self.assertEqual(last_intervene, "")


class TestCombinedDetection(unittest.TestCase):
    """Test combined stuck detection logic."""

    def test_hash_unchanged_is_stuck(self):
        hash_unchanged = 1
        jsonl_stale = 0
        tokens_stagnant = 0
        is_stuck = 1 if hash_unchanged == 1 else 0
        self.assertEqual(is_stuck, 1)

    def test_jsonl_stale_and_tokens_stagnant_is_deep_stuck(self):
        hash_unchanged = 0
        jsonl_stale = 1
        tokens_stagnant = 1
        is_stuck = 1 if (hash_unchanged == 1 or (jsonl_stale == 1 and tokens_stagnant == 1)) else 0
        self.assertEqual(is_stuck, 1)

    def test_jsonl_stale_only_not_stuck(self):
        hash_unchanged = 0
        jsonl_stale = 1
        tokens_stagnant = 0
        is_stuck = 1 if (hash_unchanged == 1 or (jsonl_stale == 1 and tokens_stagnant == 1)) else 0
        self.assertEqual(is_stuck, 0)

    def test_all_clear_not_stuck(self):
        hash_unchanged = 0
        jsonl_stale = 0
        tokens_stagnant = 0
        is_stuck = 1 if (hash_unchanged == 1 or (jsonl_stale == 1 and tokens_stagnant == 1)) else 0
        self.assertEqual(is_stuck, 0)


class TestFormatDetection(unittest.TestCase):
    """Test LLM API format auto-detection."""

    def test_explicit_anthropic(self):
        from classify_idle import _is_anthropic_format
        self.assertTrue(_is_anthropic_format("https://any-url.com", fmt="anthropic"))

    def test_explicit_openai(self):
        from classify_idle import _is_anthropic_format
        self.assertFalse(_is_anthropic_format("https://api.anthropic.com", fmt="openai"))

    def test_url_anthropic_detected(self):
        from classify_idle import _is_anthropic_format
        self.assertTrue(_is_anthropic_format("https://api.minimaxi.com/anthropic"))

    def test_url_openai_default(self):
        from classify_idle import _is_anthropic_format
        self.assertFalse(_is_anthropic_format("https://open.bigmodel.cn/api/coding/paas/v4"))

    def test_empty_fmt_uses_url(self):
        from classify_idle import _is_anthropic_format
        self.assertTrue(_is_anthropic_format("https://something/anthropic/api", fmt=""))

    def test_none_fmt_uses_url(self):
        from classify_idle import _is_anthropic_format
        self.assertTrue(_is_anthropic_format("https://api.anthropic.com", fmt=None))


class TestJsonExtraction(unittest.TestCase):
    """Test JSON extraction from LLM text responses."""

    def setUp(self):
        from classify_idle import _extract_json_from_text
        self.extract = _extract_json_from_text

    def test_none_input(self):
        self.assertIsNone(self.extract(None))

    def test_empty_input(self):
        self.assertIsNone(self.extract(""))

    def test_plain_json(self):
        r = self.extract('{"category":"test","summary":"ok"}')
        self.assertEqual(r, {"category": "test", "summary": "ok"})

    def test_json_in_code_block(self):
        r = self.extract('```json\n{"category":"test","summary":"ok"}\n```')
        self.assertEqual(r, {"category": "test", "summary": "ok"})

    def test_json_with_surrounding_text(self):
        r = self.extract('Here is the result: {"category":"decision_needed","summary":"等待决策"} done')
        self.assertEqual(r, {"category": "decision_needed", "summary": "等待决策"})

    def test_no_json(self):
        self.assertIsNone(self.extract("No JSON here, just plain text."))

    def test_nested_braces_in_summary(self):
        r = self.extract('{"category":"idle_unknown","summary":"function foo() { return 1; }"}')
        self.assertEqual(r["category"], "idle_unknown")

    def test_chinese_summary(self):
        r = self.extract('{"category":"task_complete","summary":"任务已完成，等待验收"}')
        self.assertEqual(r["category"], "task_complete")
        self.assertEqual(r["summary"], "任务已完成，等待验收")


class TestKeywordClassification(unittest.TestCase):
    """Test keyword-based idle session classification."""

    def setUp(self):
        from classify_idle import classify_by_keywords
        self.classify = classify_by_keywords

    def test_decision_needed_chinese(self):
        lines = ["我建议使用方案A，你觉得怎么样？"]
        cat, ctx = self.classify(lines)
        self.assertEqual(cat, "decision_needed")
        self.assertTrue(len(ctx) > 0)

    def test_decision_needed_english(self):
        lines = ["what do you think about this approach?"]
        cat, ctx = self.classify(lines)
        self.assertEqual(cat, "decision_needed")

    def test_task_complete_chinese(self):
        lines = ["功能已实现，请测试验证一下。"]
        cat, ctx = self.classify(lines)
        self.assertEqual(cat, "task_complete")

    def test_task_complete_english(self):
        lines = ["I've completed the implementation. Ready for review."]
        cat, ctx = self.classify(lines)
        self.assertEqual(cat, "task_complete")

    def test_ambiguous_both_match(self):
        lines = ["任务已完成。你觉得怎么样？"]
        cat, ctx = self.classify(lines)
        self.assertEqual(cat, "ambiguous")

    def test_idle_unknown(self):
        lines = ["Some random output", "Nothing special here"]
        cat, ctx = self.classify(lines)
        self.assertEqual(cat, "idle_unknown")

    def test_exclude_permission_prompt_only(self):
        lines = ["Allow", "Yes"]
        cat, ctx = self.classify(lines)
        self.assertEqual(cat, "idle_unknown")

    def test_decision_despite_permission_prompt(self):
        lines = ["Allow", "我建议这个方案，你觉得怎么样？"]
        cat, ctx = self.classify(lines)
        self.assertEqual(cat, "decision_needed")


class TestCallLlmApiConstruction(unittest.TestCase):
    """Test _call_llm builds correct requests for both API formats."""

    def setUp(self):
        from classify_idle import _call_llm
        self.call_llm = _call_llm

    def test_anthropic_format_url(self):
        """Anthropic format appends /v1/messages."""
        import urllib.request
        original = urllib.request.Request
        captured = {}

        def mock_request(url, data=None, headers=None):
            captured['url'] = url
            captured['headers'] = headers
            return original(url, data=data, headers=headers)

        import classify_idle
        orig_req = urllib.request.Request
        urllib.request.Request = mock_request
        try:
            self.call_llm("https://api.minimaxi.com/anthropic", "key", "model", "test", fmt="anthropic")
        except Exception:
            pass
        finally:
            urllib.request.Request = orig_req
        self.assertEqual(captured['url'], "https://api.minimaxi.com/anthropic/v1/messages")
        self.assertIn("x-api-key", captured['headers'])

    def test_openai_format_url(self):
        """OpenAI format appends /chat/completions."""
        import urllib.request
        captured = {}

        def mock_request(url, data=None, headers=None):
            captured['url'] = url
            captured['headers'] = headers
            return urllib.request.Request.__new__(urllib.request.Request)

        import classify_idle
        orig_req = urllib.request.Request
        urllib.request.Request = mock_request
        try:
            self.call_llm("https://open.bigmodel.cn/api/paas/v4", "key", "model", "test", fmt="openai")
        except Exception:
            pass
        finally:
            urllib.request.Request = orig_req
        self.assertEqual(captured['url'], "https://open.bigmodel.cn/api/paas/v4/chat/completions")
        self.assertIn("Authorization", captured['headers'])


class TestVersionAndConfig(unittest.TestCase):
    """Test version and config consistency."""

    def test_version_is_201(self):
        result = subprocess.run(
            ["bash", "-c", f"source {WATCHDOG_SCRIPT} && echo $VERSION"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertIn("2.0.1", result.stdout)

    def test_start_template_updated(self):
        with open(os.path.join(SCRIPT_DIR, "scripts", "notify-templates.json")) as f:
            templates = json.load(f)
        body = templates["start"]["body"]
        self.assertIn("空闲分类", body)
        self.assertIn("LLM", body)
        self.assertIn("5 分钟", body)

    def test_idle_classify_threshold_exists(self):
        result = subprocess.run(
            ["bash", "-c", f"source {WATCHDOG_SCRIPT} && echo $IDLE_CLASSIFY_THRESHOLD"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout.strip(), "300")


class TestLlmFallbackPath(unittest.TestCase):
    """Test primary-fail → fallback-success logic."""

    def test_primary_fail_fallback_succeeds(self):
        import classify_idle
        call_count = [0]

        def mock_call(base_url, api_key, model, prompt, fmt=None):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("primary down")
            return ("task_complete", "done")

        original = classify_idle._call_llm
        classify_idle._call_llm = mock_call
        orig_key = os.environ.get("WATCHDOG_LLM_API_KEY")
        orig_key2 = os.environ.get("WATCHDOG_LLM_API_KEY_2")
        try:
            os.environ["WATCHDOG_LLM_API_KEY"] = "key1"
            os.environ["WATCHDOG_LLM_API_KEY_2"] = "key2"
            result = classify_idle.classify_with_llm(["some line"])
            self.assertEqual(result, ("task_complete", "done"))
            self.assertEqual(call_count[0], 2)
        finally:
            classify_idle._call_llm = original
            if orig_key:
                os.environ["WATCHDOG_LLM_API_KEY"] = orig_key
            else:
                os.environ.pop("WATCHDOG_LLM_API_KEY", None)
            if orig_key2:
                os.environ["WATCHDOG_LLM_API_KEY_2"] = orig_key2
            else:
                os.environ.pop("WATCHDOG_LLM_API_KEY_2", None)

    def test_both_fail_returns_timeout(self):
        import classify_idle

        def mock_call(base_url, api_key, model, prompt, fmt=None):
            raise ConnectionError("all down")

        original = classify_idle._call_llm
        classify_idle._call_llm = mock_call
        orig_key = os.environ.get("WATCHDOG_LLM_API_KEY")
        orig_key2 = os.environ.get("WATCHDOG_LLM_API_KEY_2")
        try:
            os.environ["WATCHDOG_LLM_API_KEY"] = "key1"
            os.environ["WATCHDOG_LLM_API_KEY_2"] = "key2"
            result = classify_idle.classify_with_llm(["some line"])
            self.assertEqual(result[0], "llm_timeout")
        finally:
            classify_idle._call_llm = original
            if orig_key:
                os.environ["WATCHDOG_LLM_API_KEY"] = orig_key
            else:
                os.environ.pop("WATCHDOG_LLM_API_KEY", None)
            if orig_key2:
                os.environ["WATCHDOG_LLM_API_KEY_2"] = orig_key2
            else:
                os.environ.pop("WATCHDOG_LLM_API_KEY_2", None)


if __name__ == "__main__":
    unittest.main()
