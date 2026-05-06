"""Send Feishu card notification from template.

Usage: notify.py <template_file> <section> <key1=val1> [key2=val2] ...

Reads the template section, substitutes {key} placeholders, signs the
request with HMAC-SHA256, and POSTs to Feishu webhook.
"""

import json
import hmac
import hashlib
import base64
import time
import urllib.request
import urllib.parse
import os
import sys


def main():
    if len(sys.argv) < 3:
        print("Usage: notify.py <template_file> <section> [key=value ...]", file=sys.stderr)
        sys.exit(1)

    template_file = sys.argv[1]
    section = sys.argv[2]

    # Read template
    with open(template_file, "r") as f:
        templates = json.load(f)
    tpl = templates.get(section, {})
    title = tpl.get("title", "")
    color = tpl.get("color", "blue")
    body = tpl.get("body", "")

    # Parse key=value arguments
    variables = {}
    for arg in sys.argv[3:]:
        arg = arg.strip()
        if "=" in arg:
            k, v = arg.split("=", 1)
            variables[k.strip()] = v.strip()

    # Substitute placeholders
    for k, v in variables.items():
        title = title.replace("{" + k + "}", v)
        body = body.replace("{" + k + "}", v)

    print(f"RENDERED: color={color} title={title}")

    # Send to Feishu
    webhook = os.environ.get("FEISHU_WEBHOOK", "")
    secret = os.environ.get("FEISHU_SECRET", "")
    if not webhook or not secret:
        print("NOTIFY_SKIP: no feishu config")
        return

    ts = str(int(time.time()))
    string_to_sign = f"{ts}\n{secret}"
    sign = base64.b64encode(
        hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    ).decode("utf-8")
    url = f"{webhook}?timestamp={ts}&sign={urllib.parse.quote(sign)}"

    payload = json.dumps({
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
        },
    })

    req = urllib.request.Request(url, data=payload.encode("utf-8"),
                                headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        if data.get("code") == 0:
            print(f"NOTIFY_OK: {title}")
        else:
            print(f"NOTIFY_FAIL: {data}")
    except Exception as e:
        print(f"NOTIFY_ERROR: {e}")


if __name__ == "__main__":
    main()
