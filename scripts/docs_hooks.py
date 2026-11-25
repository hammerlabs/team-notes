"""MkDocs hook: README.md and docs/*.md keep links written for a file browser, and they work in the site too.

In the repository a Markdown file links by path relative to itself: README.md
links `docs/API_CONTRACT.md` and `tests/test_isolation.py`, and a page in docs/
would link `../README.md`. GitHub-style rendering follows those as they are.
In the site the same files live elsewhere: the README is `index.md`,
tests/README.md is `tests/index.md`, `docs/X` is `X`, and a source file is a
generated reference page. This hook rewrites each relative link target from
the repository layout to the site layout, so one text serves both. Link text
is not changed. A link it cannot map is left alone, and the strict build
reports it.
"""

import posixpath
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# `[text](target)` and `![alt](target)`, with an optional `#anchor`. Targets with
# a scheme (`https:`), an absolute path (`/docs`) or only an anchor are not touched.
LINK = re.compile(r"(\]\()(?![a-z][a-z0-9+.-]*:|/|#)([^)\s#]+)(#[^)\s]*)?\)")


def site_path(repo_path: str) -> str | None:
    """Where a repository file appears in the site (paths relative to docs_dir), or None."""
    if repo_path == "README.md":
        return "index.md"
    if repo_path == "tests/README.md":
        return "tests/index.md"
    if repo_path.startswith("docs/"):
        return repo_path.removeprefix("docs/")
    if repo_path.startswith("app/") and repo_path.endswith(".py"):
        module = repo_path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        return f"reference/{module}.md"  # named as in docs_pages.py
    if repo_path.startswith("tests/") and repo_path.endswith(".py"):
        return f"tests/{posixpath.basename(repo_path).removesuffix('.py')}.md"
    return None


def on_page_markdown(markdown: str, page: Any, **_: Any) -> str:
    src = page.file.src_uri  # the page's path in the site, e.g. "index.md", "SEED_DATA.md"
    repo_file = {"index.md": "README.md", "tests/index.md": "tests/README.md"}.get(src, f"docs/{src}")
    if not (ROOT / repo_file).is_file():
        return markdown  # a generated reference page: no hand-written links

    def rewrite(match: re.Match[str]) -> str:
        prefix, target, anchor = match.group(1), match.group(2), match.group(3) or ""
        mapped = site_path(posixpath.normpath(posixpath.join(posixpath.dirname(repo_file), target)))
        if mapped is None:
            return match.group(0)
        return f"{prefix}{posixpath.relpath(mapped, posixpath.dirname(src) or '.')}{anchor})"

    return LINK.sub(rewrite, markdown)
