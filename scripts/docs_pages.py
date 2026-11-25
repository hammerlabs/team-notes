"""Generate the docs site's pages that are not files under docs/. Run by mkdocs-gen-files at build time.

- `index.md`: the README, read at build time so README.md stays the one
  source, with a short note about the site inserted right after its first
  heading. docs_hooks.py points its links at the site's pages; link text is
  left as written.
- `reference/`: one page per module under app/, rendered by mkdocstrings
  from the docstrings and grouped in the nav by GROUPS below, plus an index
  page listing the groups (so `/site/reference/` is a page, not a 404).
- `tests/`: tests/README.md as the section's index page, then one page per
  module under tests/, so each test's stated purpose can be browsed. Test
  pages show only documented members (tests, fixtures, helpers), not
  dataclass fields or constants; so that this hides nothing, the build fails
  if a test or fixture has no docstring.

Pages come from the files on disk, so a new module gets a page without
editing anything; an app module that matches no group lands in "Other".
"""

import ast
from pathlib import Path

import mkdocs_gen_files

ROOT = Path(__file__).resolve().parent.parent

# Nav groups for app/ modules, in nav order. A module belongs to the first
# group with an entry equal to its dotted name or a parent package of it.
GROUPS = {
    "App factory and config": [
        "app",
        "app.main",
        "app.config",
        "app.deps",
        "app.errors",
        "app.body_limit",
        "app.clock",
    ],
    "Store: interface and implementations": ["app.store"],
    "Routers": ["app.routers"],
    "Notifier": ["app.notify"],
    "Security": ["app.security"],
    "Schemas and models": ["app.schemas", "app.models"],
    "Seed data": ["app.seed"],
}

HOME_NOTE = """
> This site is built from the repository by `mkdocs build` and served by the app at `/site/`.
> The "Running app" links ([test page](/), [API explorer](/docs), [ReDoc](/redoc)) open the app's
> own routes on the same port, so they work only when this site is served by the running app.
"""

REFERENCE_INTRO = """\
# API reference

Generated from the docstrings of every module under `app/`, grouped as in the navigation. Start with
`app.store.interface` (what storage promises) and `app.store.sql` (how each rule is one conditional write).

"""

# Test pages: members without a docstring (dataclass fields, constants such as ROUNDS) are not listed.
TEST_PAGE_OPTIONS = "    options:\n      show_if_no_docstring: false\n"


def module_name(path: Path) -> str:
    """`app/store/__init__.py` -> `app.store`; `app/store/sql.py` -> `app.store.sql`."""
    parts = path.relative_to(ROOT).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def group_of(module: str) -> str:
    for group, prefixes in GROUPS.items():
        if any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes):
            return group
    return "Other"


def write_page(path: str, identifier: str, edit_path: Path, options: str = "") -> None:
    with mkdocs_gen_files.open(path, "w") as page:
        page.write(f"::: {identifier}\n{options}")
    mkdocs_gen_files.set_edit_path(path, edit_path.relative_to(ROOT))


def undocumented_tests(source: Path) -> list[str]:
    """Names of test functions and fixtures in `source` that have no docstring."""
    missing = []
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or ast.get_docstring(node):
            continue
        is_fixture = any("fixture" in ast.unparse(decorator) for decorator in node.decorator_list)
        if node.name.startswith("test_") or is_fixture:
            missing.append(f"{source.name}::{node.name}")
    return missing


def with_note_after_title(readme: str, note: str) -> str:
    """The README with `note` inserted after its first line if that is a `# ` title, else before everything."""
    first, _, rest = readme.partition("\n")
    return f"{first}\n{note}{rest}" if first.startswith("# ") else note.lstrip("\n") + "\n" + readme


# Home: the README.
with mkdocs_gen_files.open("index.md", "w") as home:
    home.write(with_note_after_title((ROOT / "README.md").read_text(encoding="utf-8"), HOME_NOTE))
mkdocs_gen_files.set_edit_path("index.md", "README.md")

# API reference. Page paths are flat (`reference/app.store.sql.md`), so a
# link to a source file maps to its page without knowing the groups.
grouped: dict[str, list[str]] = {group: [] for group in [*GROUPS, "Other"]}
for source in sorted((ROOT / "app").rglob("*.py")):
    module = module_name(source)
    write_page(f"reference/{module}.md", module, source)
    grouped[group_of(module)].append(module)
with mkdocs_gen_files.open("reference/index.md", "w") as index:
    index.write(REFERENCE_INTRO)
    for group, modules in grouped.items():
        if modules:
            index.write(f"## {group}\n\n" + "".join(f"- [`{m}`]({m}.md)\n" for m in modules) + "\n")
with mkdocs_gen_files.open("reference/SUMMARY.md", "w") as nav:
    nav.write("* [Overview](index.md)\n")
    nav.writelines(
        f"* {group}\n" + "".join(f"    * [{m}]({m}.md)\n" for m in modules)
        for group, modules in grouped.items()
        if modules
    )

# Tests: shared fixtures and helpers first, then the test modules. mkdocs.yml's
# handler options show every member, including `_private` helpers and tests
# without a docstring.
tests = sorted((ROOT / "tests").glob("*.py"), key=lambda p: (p.stem.startswith("test_"), p.stem))
missing = [name for source in tests for name in undocumented_tests(source)]
if missing:
    raise SystemExit(
        "Tests and fixtures need a docstring (the docs site lists only documented members): " + ", ".join(missing)
    )
with mkdocs_gen_files.open("tests/index.md", "w") as index:
    index.write((ROOT / "tests" / "README.md").read_text(encoding="utf-8"))
mkdocs_gen_files.set_edit_path("tests/index.md", "tests/README.md")
for source in tests:
    write_page(f"tests/{source.stem}.md", source.stem, source, TEST_PAGE_OPTIONS)
with mkdocs_gen_files.open("tests/SUMMARY.md", "w") as nav:
    nav.write("* [Overview](index.md)\n")
    nav.writelines(f"* [{source.stem}]({source.stem}.md)\n" for source in tests)
