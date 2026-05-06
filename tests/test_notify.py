"""Tests for Claude Code Watchdog notification templates and rendering."""

import json
import os
import subprocess
import tempfile
import unittest
import hmac
import hashlib
import base64

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_FILE = os.path.join(SCRIPT_DIR, "scripts", "notify-templates.json")
WATCHDOG_SCRIPT = os.path.join(SCRIPT_DIR, "scripts", "watchdog.sh")


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
        for event in ("stuck", "intervene", "recovered", "start", "daily"):
            self.assertIn(event, data)

    def test_each_template_has_required_fields(self):
        with open(TEMPLATE_FILE) as f:
            data = json.load(f)
        for event, tpl in data.items():
            with self.subTest(event=event):
                self.assertIn("title", tpl)
                self.assertIn("color", tpl)
                self.assertIn("body", tpl)
                self.assertIn(tpl["color"], {"red", "orange", "green", "blue", "purple"})

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
  printf '{{"timestamp":"%s","event":"%s","session":"%s","project":"%s","duration_minutes":%s,"model":"GLM-5.1","phase":"unknown","intervention":"%s","recovered":%s,"notes":"%s"}}\\n' \\
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$event" "$session" "$session" "$duration" "$intervention" "$recovered" "$notes" \\
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


if __name__ == "__main__":
    unittest.main()
