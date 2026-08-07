"""Post changelog updates to Discord via webhook."""

import json
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE / "data" / "discord_config.json"
CHANGELOG_PATH = BASE / "docs" / "changelog.md"


def _load_config():
    """Load Discord config, prompting for webhook URL if not set."""
    if CONFIG_PATH.exists():
        config = json.loads(CONFIG_PATH.read_text())
        if config.get("webhook_url"):
            return config

    print("Discord webhook URL not configured.")
    print("To get one: Discord channel → Edit → Integrations → Webhooks → New Webhook → Copy URL")
    url = input("Paste webhook URL: ").strip()
    if not url.startswith("https://discord.com/api/webhooks/"):
        print("Invalid webhook URL. Should start with https://discord.com/api/webhooks/")
        sys.exit(1)

    config = {"webhook_url": url}
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2))
    print(f"Saved to {CONFIG_PATH}")
    return config


def _parse_latest_session():
    """Parse the most recent session entry from changelog.md.

    Returns dict with: session (str), date (str), sections [{title, items}]
    """
    text = CHANGELOG_PATH.read_text()

    # Find the first ## Session heading
    session_pattern = re.compile(r"^## (Session \d+(?:\w*)) \((\d{4}-\d{2}-\d{2})\)", re.MULTILINE)
    matches = list(session_pattern.finditer(text))
    if not matches:
        return None

    start = matches[0].start()
    end = matches[1].start() if len(matches) > 1 else len(text)
    session_text = text[start:end].strip()

    session_name = matches[0].group(1)
    session_date = matches[0].group(2)

    # Parse subsections (### headings)
    sections = []
    section_pattern = re.compile(r"^### (.+)$", re.MULTILINE)
    section_matches = list(section_pattern.finditer(session_text))

    for i, sm in enumerate(section_matches):
        title = sm.group(1)
        sec_start = sm.end()
        sec_end = section_matches[i + 1].start() if i + 1 < len(section_matches) else len(session_text)
        body = session_text[sec_start:sec_end].strip()

        # Extract bullet items
        items = []
        for raw_line in body.split("\n"):
            indent = len(raw_line) - len(raw_line.lstrip())
            line = raw_line.strip()
            if indent >= 2:
                continue  # Skip indented sub-bullets
            if line.startswith("- **"):
                # Extract the bold title and description
                # Format: - **Title** — Description
                # or:     - **Title** (`files`) — Description
                m = re.match(r"- \*\*(.+?)\*\*\s*(?:\([^)]+\)\s*)?[—–-]\s*(.+)", line)
                if m:
                    desc = m.group(2)
                    items.append({"title": m.group(1), "desc": desc})
                else:
                    m2 = re.match(r"- \*\*(.+?)\*\*\s*(.*)", line)
                    if m2:
                        items.append({"title": m2.group(1), "desc": m2.group(2)})
            elif line.startswith("- "):
                desc = line[2:]
                items.append({"title": None, "desc": desc})

        if items:
            sections.append({"title": title, "items": items})

    return {"session": session_name, "date": session_date, "sections": sections}


def _format_embed(parsed):
    """Format a parsed session as a Discord embed."""
    description_parts = []

    for section in parsed["sections"]:
        description_parts.append(f"**{section['title']}**")
        for item in section["items"]:
            if item["title"]:
                desc = item["desc"]
                if len(desc) > 300:
                    desc = desc[:297] + "..."
                description_parts.append(f"• **{item['title']}** — {desc}")
            else:
                desc = item["desc"]
                if len(desc) > 300:
                    desc = desc[:297] + "..."
                description_parts.append(f"• {desc}")
        description_parts.append("")  # blank line between sections

    description = "\n".join(description_parts).strip()

    # Discord embed limit is 4096 chars for description
    if len(description) > 4000:
        description = description[:3997] + "..."

    embed = {
        "title": f"📋 Stats++ Update — {parsed['session']}",
        "description": description,
        "color": 0x3498db,  # blue accent
        "footer": {"text": f"Released {parsed['date']}"},
    }
    return embed


def _post_webhook(config, embed):
    """POST an embed to the Discord webhook."""
    payload = json.dumps({"embeds": [embed]}).encode("utf-8")
    req = urllib.request.Request(
        config["webhook_url"],
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Stats++ Bot (https://github.com/tfalsone/statsplusplus)",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req)
        if resp.status == 204:
            print("✓ Posted to Discord successfully.")
        else:
            print(f"Discord responded with status {resp.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"✗ Discord error {e.code}: {body}")
        sys.exit(1)


def post_latest():
    """Parse the latest changelog session and post it to Discord."""
    config = _load_config()
    parsed = _parse_latest_session()
    if not parsed:
        print("No session entries found in changelog.md")
        sys.exit(1)

    print(f"Posting: {parsed['session']} ({parsed['date']})")
    print(f"  {len(parsed['sections'])} sections, "
          f"{sum(len(s['items']) for s in parsed['sections'])} items")

    embed = _format_embed(parsed)
    _post_webhook(config, embed)


def post_custom(message):
    """Post a custom message to Discord."""
    config = _load_config()
    payload = json.dumps({"content": message}).encode("utf-8")
    req = urllib.request.Request(
        config["webhook_url"],
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Stats++ Bot (https://github.com/tfalsone/statsplusplus)",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req)
        if resp.status == 204:
            print("✓ Posted to Discord successfully.")
        else:
            print(f"Discord responded with status {resp.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"✗ Discord error {e.code}: {body}")
        sys.exit(1)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Post updates to Discord")
    sub = parser.add_subparsers(dest="cmd")

    latest_parser = sub.add_parser("latest", help="Post the latest changelog session")
    latest_parser.add_argument("--title", help="Override the embed title (default: session name)")
    preview_parser = sub.add_parser("preview", help="Preview what would be posted (no send)")
    preview_parser.add_argument("--title", help="Override the embed title")
    msg_parser = sub.add_parser("message", help="Post a custom message")
    msg_parser.add_argument("text", help="Message text")

    args = parser.parse_args()

    if args.cmd == "latest":
        config = _load_config()
        parsed = _parse_latest_session()
        if not parsed:
            print("No session entries found in changelog.md")
            sys.exit(1)
        embed = _format_embed(parsed)
        if args.title:
            embed["title"] = f"📋 {args.title}"
        print(f"Posting: {embed['title']}")
        print(f"  {len(parsed['sections'])} sections, "
              f"{sum(len(s['items']) for s in parsed['sections'])} items")
        _post_webhook(config, embed)
    elif args.cmd == "preview":
        parsed = _parse_latest_session()
        if not parsed:
            print("No session entries found in changelog.md")
            sys.exit(1)
        embed = _format_embed(parsed)
        if args.title:
            embed["title"] = f"📋 {args.title}"
        print(f"Title: {embed['title']}")
        print(f"Color: #{embed['color']:06x}")
        print(f"Footer: {embed['footer']['text']}")
        print(f"Description ({len(embed['description'])} chars):")
        print("---")
        print(embed['description'])
    elif args.cmd == "message":
        post_custom(args.text)
    else:
        parser.print_help()
