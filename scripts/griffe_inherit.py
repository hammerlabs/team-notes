"""Griffe extension for the docs site: a method without a docstring shows the docstring of the method it overrides.

`SqlStore` implements every `Store` operation without repeating its contract, and the subclasses implement the hooks
`SqlStore` declares. Without this, their reference pages would list bare signatures. The text is copied at build time
only; the source keeps one copy. Loaded by mkdocs.yml (`extensions:`).
"""

from typing import Any

import griffe


class InheritDocstrings(griffe.Extension):
    """After the package loads, give each undocumented method its nearest base class method's docstring."""

    def on_package(self, *, pkg: griffe.Module, **kwargs: Any) -> None:
        for cls in _classes(pkg):
            try:
                bases = cls.mro()
            except ValueError:
                continue  # a base outside the loaded package: nothing to inherit from
            for name, member in cls.members.items():
                if member.is_alias or not member.is_function or member.docstring:
                    continue
                for base in bases:
                    inherited = base.members.get(name)
                    if inherited is not None and not inherited.is_alias and inherited.docstring:
                        member.docstring = griffe.Docstring(inherited.docstring.value, parent=member)
                        break


def _classes(module: griffe.Module) -> list[griffe.Class]:
    """Every class defined in `module` and its submodules (not imported aliases)."""
    found: list[griffe.Class] = []
    for member in module.members.values():
        if member.is_alias:
            continue
        if member.is_module:
            found += _classes(member)
        elif member.is_class:
            found.append(member)
    return found
