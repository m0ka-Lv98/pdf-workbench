from __future__ import annotations

import re
import subprocess
from pathlib import Path

ISSUE_DIR = Path(__file__).resolve().parents[1] / "docs" / "issues"
FRONT_MATTER = re.compile(r"\A---\n(?P<meta>.*?)\n---\n(?P<body>.*)\Z", re.DOTALL)


def run(*args: str) -> str:
    result = subprocess.run(args, check=True, capture_output=True, text=True, encoding="utf-8")
    return result.stdout.strip()


def parse_issue(path: Path) -> tuple[str, list[str], str]:
    content = path.read_text(encoding="utf-8")
    match = FRONT_MATTER.match(content)
    if match is None:
        raise ValueError(f"Invalid issue file: {path}")

    metadata: dict[str, str] = {}
    for line in match.group("meta").splitlines():
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()

    title = metadata["title"]
    labels = [label.strip() for label in metadata.get("labels", "").split(",") if label.strip()]
    return title, labels, match.group("body").strip()


def existing_titles() -> set[str]:
    output = run(
        "gh",
        "issue",
        "list",
        "--state",
        "all",
        "--limit",
        "200",
        "--json",
        "title",
        "--jq",
        ".[].title",
    )
    return set(output.splitlines()) if output else set()


def main() -> None:
    known_titles = existing_titles()
    for path in sorted(ISSUE_DIR.glob("*.md")):
        title, labels, body = parse_issue(path)
        if title in known_titles:
            print(f"skip: {title}")
            continue

        command = ["gh", "issue", "create", "--title", title, "--body", body]
        for label in labels:
            command.extend(["--label", label])
        try:
            url = run(*command)
        except subprocess.CalledProcessError:
            # Labels may not exist yet. Retry without labels so issue creation is not blocked.
            url = run("gh", "issue", "create", "--title", title, "--body", body)
        print(f"created: {url}")


if __name__ == "__main__":
    main()
