"""Edit one field of a Kubernetes manifest, changing nothing else.

The obvious way to do this is to parse the YAML, mutate the object, and dump it
back. That produces a correct file and a useless commit: the dumper rewrites the
whole document, drops every comment, reorders keys, and normalises quoting, so
the diff a reviewer sees is three hundred lines when the change was one number.

The entire value of routing an agent's actions through git is that a human can
read the diff before it reaches the cluster. A change nobody can review is the
thing this project exists to avoid, so the edit is done on the text.

That means a small indentation-aware path tracker instead of a parser. It
handles the shapes Kubernetes workload manifests actually use — nested mappings
and lists of named objects — and refuses anything it does not understand rather
than guessing. Refusing is safe here: the action simply is not proposed, which
is the same outcome as a missing manifest.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ManifestError", "FieldEdit", "set_field", "find_document", "read_field"]


class ManifestError(ValueError):
    """The manifest is not shaped the way the edit needs."""


@dataclass(frozen=True)
class FieldEdit:
    """The result of an edit: new text, and what changed, for the commit body."""

    text: str
    line: int
    path: tuple[str, ...]
    before: str
    after: str

    def describe(self) -> str:
        return f"{'.'.join(self.path)}: {self.before} -> {self.after}"


def _split(line: str) -> tuple[int, bool, str]:
    """Return (indent, is_list_item, content) for a line."""
    stripped = line.lstrip(" ")
    indent = len(line) - len(stripped)
    if stripped.startswith("- "):
        # A list item's content sits two columns further in, which is where its
        # sibling keys will be indented to.
        return indent + 2, True, stripped[2:]
    if stripped.rstrip() == "-":
        return indent + 2, True, ""
    return indent, False, stripped


def _key_of(content: str) -> tuple[str, str] | None:
    """Split 'key: value' into its parts, or None if the line is not a mapping."""
    if not content or content.startswith("#"):
        return None
    key, sep, value = content.partition(":")
    if not sep or not key.strip() or " " in key.strip():
        return None
    value = value.strip()
    for marker in (" #", "\t#"):
        if marker in value:
            value = value.split(marker, 1)[0].rstrip()
            break
    return key.strip(), value


def _walk(lines: list[str]):
    """Yield (index, indent, path, key, value) for every mapping line.

    List elements are addressed by their ``name`` value, matching how
    Kubernetes identifies containers, ports and volumes. An unnamed element
    falls back to its position.
    """
    stack: list[tuple[int, str]] = []   # (indent at which children sit, key)
    list_index: dict[int, int] = {}     # indent -> next positional index
    pending_item: int | None = None     # indent of an item awaiting its name

    for i, raw in enumerate(lines):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent, is_item, content = _split(raw)

        # A sibling list element closes the previous one, so items pop at equal
        # depth while plain keys do not.
        limit = (lambda d: d >= indent) if is_item else (lambda d: d > indent)
        while stack and limit(stack[-1][0]):
            stack.pop()

        if is_item:
            # A new element of the list keyed by the enclosing entry.
            parsed = _key_of(content)
            if parsed and parsed[0] == "name" and parsed[1]:
                label = parsed[1]
            else:
                label = str(list_index.get(indent, 0))
            list_index[indent] = list_index.get(indent, 0) + 1
            stack.append((indent, label))
            pending_item = indent
            if parsed:
                key, value = parsed
                yield i, indent, tuple(k for _, k in stack) + (key,), key, value
            continue

        parsed = _key_of(content)
        if parsed is None:
            continue
        key, value = parsed
        if pending_item is not None and indent <= pending_item - 2:
            pending_item = None
        yield i, indent, tuple(k for _, k in stack) + (key,), key, value
        if value == "":
            # A key with no inline value opens a block; its children are deeper.
            stack.append((indent + 1, key))
            list_index.pop(indent + 2, None)


def find_document(text: str, kind: str, name: str, namespace: str) -> tuple[int, int] | None:
    """Locate one document inside a possibly multi-document file.

    Returns the (start, end) line range, or None. Multi-document files are the
    norm in GitOps repositories, and editing the wrong document in one would be
    a silent, dangerous mistake.
    """
    lines = text.splitlines()
    bounds: list[int] = [0]
    for i, line in enumerate(lines):
        if line.rstrip() == "---" and i:
            bounds.append(i + 1)
    bounds.append(len(lines))

    for start, end in zip(bounds, bounds[1:]):
        chunk = lines[start:end]
        found_kind = found_name = found_ns = None
        for _, indent, path, key, value in _walk(chunk):
            if path == ("kind",):
                found_kind = value
            elif path == ("metadata", "name"):
                found_name = value
            elif path == ("metadata", "namespace"):
                found_ns = value
        if found_kind == kind and found_name == name and (found_ns or "default") == namespace:
            return start, end
    return None


def read_field(text: str, path: tuple[str, ...], within: tuple[int, int] | None = None) -> str | None:
    """Current value at ``path``, or None if it is not present."""
    lines = text.splitlines()
    start, end = within or (0, len(lines))
    for _, _, found, _, value in _walk(lines[start:end]):
        if found == path:
            return value
    return None


def set_field(
    text: str,
    path: tuple[str, ...],
    value: str,
    within: tuple[int, int] | None = None,
) -> FieldEdit:
    """Replace the value at ``path``, preserving every other byte of the file.

    Raises rather than creating the field when it is absent. An agent inventing
    a resource limit that the manifest never declared is a change of a different
    kind from adjusting one that exists, and it is not a change this is allowed
    to make unattended.
    """
    lines = text.splitlines(keepends=True)
    bare = text.splitlines()
    start, end = within or (0, len(bare))

    for offset, _, found, key, current in _walk(bare[start:end]):
        if found != path:
            continue
        i = start + offset
        line = lines[i]
        newline = ""
        while line.endswith(("\n", "\r")):
            newline = line[-1] + newline
            line = line[:-1]

        head, _, tail = line.partition(":")
        # Keep any trailing comment on the line; it is often the reason the
        # value is what it is.
        comment = ""
        if "#" in tail:
            body, _, after = tail.partition("#")
            if body.strip() == current:
                # Preserve the original run of spaces: shifting a comment left
                # by one column is churn in a diff meant to show one change.
                gap = len(body) - len(body.rstrip())
                comment = " " * gap + "#" + after
        lines[i] = f"{head}: {value}{comment}{newline}"
        return FieldEdit(
            text="".join(lines), line=i + 1, path=path, before=current, after=value
        )

    raise ManifestError(f"no field {'.'.join(path)} in this manifest")
