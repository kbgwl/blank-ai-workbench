"""Prompt-file argument expansion — the ``qW`` equivalent from Claude Code.

Claude's slash commands and skills share one substitution engine:

* ``$ARGUMENTS`` — the whole argument string;
* ``$ARGUMENTS[n]`` / ``$n`` — the n-th shell word (0-based);
* ``$name`` — the n-th word, where *n* is the position of ``name`` in the
  declared ``arguments`` list (longest name wins when one is a prefix of
  another);
* ``\\$`` — a literal ``$`` (the backslash is consumed);
* when the body has no placeholder at all, the raw arguments are appended as
  ``\\nARGUMENTS: <args>``.

Two sentinels keep the pass order honest: substituted values have their ``$``
replaced with ``\uFFFF`` so a value containing ``$ARGUMENTS`` is never expanded
again, and ``\uFFFE`` wraps the value so the final restore only touches copies
that came from us. Both sentinels are neutralised (→ ``\uFFFD``) in the source
text first, so a prompt cannot smuggle them in.
"""

from __future__ import annotations

import re
import shlex

SENTINEL = "\uFFFF"
WRAPPER = "\uFFFE"
REPLACEMENT = "\uFFFD"


def split_args(raw: str) -> list[str]:
    """Shell-style word split; a malformed quote falls back to whitespace."""
    text = str(raw or "")
    if not text.strip():
        return []
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    return [part for part in parts if part]


def argument_names(value) -> tuple[str, ...]:
    """Parse frontmatter ``arguments``: YAML list or whitespace string.

    Claude drops empty entries and entries that are all digits (a positional
    name would be ambiguous with ``$0``-style placeholders).
    """
    if value is None:
        return ()

    def usable(item) -> bool:
        return (
            isinstance(item, str)
            and bool(item.strip())
            and not item.strip().isdigit()
        )

    if isinstance(value, list):
        return tuple(item.strip() for item in value if usable(item))
    if isinstance(value, str):
        return tuple(item for item in value.split() if usable(item))
    return ()


def expand_arguments(
    text: str,
    args: str | None,
    *,
    names: tuple[str, ...] | list[str] = (),
) -> str:
    """Carry out Claude's ``qW(content, args, append=true, names)``."""
    if args is None:
        return text
    text = str(text).replace(SENTINEL, REPLACEMENT).replace(WRAPPER, REPLACEMENT)

    def value(raw) -> str:
        cooked = str(raw or "").replace(SENTINEL, REPLACEMENT).replace(WRAPPER, REPLACEMENT)
        return WRAPPER + cooked.replace("$", SENTINEL) + WRAPPER

    argv = split_args(args)
    declared = sorted(
        ((str(name), index) for index, name in enumerate(names) if name),
        key=lambda item: -len(item[0]),
    )
    keys = [r"\d", "ARGUMENTS"] + [
        re.escape(name) + r"(?!\[|\w)" for name, _ in declared
    ]
    text = re.sub(
        r"(?<!\\)\\\$(?=" + "|".join(keys) + ")",
        SENTINEL,
        text,
    )

    used = False
    for name, index in declared:
        pattern = re.compile(r"\$" + re.escape(name) + r"(?!\[|\w)")

        def replace_named(match, index=index):
            nonlocal used
            used = True
            return value(argv[index] if index < len(argv) else "")

        text = pattern.sub(replace_named, text)

    def replace_indexed(match):
        nonlocal used
        index = int(match[1], 10)
        if index >= len(argv):
            # Out of range: keep the literal text, but protect the ``$`` from
            # the later ``$ARGUMENTS`` pass (Claude does the same).
            return SENTINEL + match[0][1:]
        used = True
        return value(argv[index])

    text = re.sub(r"\$ARGUMENTS\[(\d+)\]", replace_indexed, text)

    def replace_positional(match):
        nonlocal used
        index = int(match[1], 10)
        if index >= len(argv):
            return match[0]
        used = True
        return value(argv[index])

    text = re.sub(r"\$(\d+)(?!\w)", replace_positional, text)
    if "$ARGUMENTS" in text:
        used = True
        text = text.replace("$ARGUMENTS", value(args))
    if not used and args:
        text = text + "\nARGUMENTS: " + value(args)
    return text.replace(SENTINEL, "$").replace(WRAPPER, "")


def render_prompt(
    instructions: str,
    args: str | None,
    *,
    names: tuple[str, ...] | list[str] = (),
    project_dir: str = "",
    session_id: str = "",
    skill_dir: str = "",
    effort: str = "",
    plugin_root: str = "",
    plugin_data: str = "",
) -> str:
    """Expand one prompt file: arguments first, then Claude's placeholders."""
    clean = str(args) if args is not None and str(args) != "" else None
    text = expand_arguments(instructions, clean, names=names)
    return substitute_paths(
        text,
        project_dir=project_dir,
        session_id=session_id,
        skill_dir=skill_dir,
        effort=effort,
        plugin_root=plugin_root,
        plugin_data=plugin_data,
    )


def substitute_paths(
    text: str,
    *,
    project_dir: str = "",
    session_id: str = "",
    skill_dir: str = "",
    effort: str = "",
    plugin_root: str = "",
    plugin_data: str = "",
) -> str:
    """Replace Claude's directory/session/effort/plugin placeholders.

    ``$CLAUDE_SKILL_DIR`` only applies to a file whose name is ``SKILL.md``
    (Claude passes a base directory for those and nothing for plain commands).

    ``${CLAUDE_EFFORT}`` carries this invocation's *resolved* effort, not the
    raw request: upstream substitutes the value after the model cap and the
    thinking-disabled clamp have been applied, so a body written for Claude
    that says "think at ${CLAUDE_EFFORT}" gets the level actually granted.

    ``${CLAUDE_PLUGIN_ROOT}`` is expanded whenever a plugin root is provided
    (upstream ``C9`` always rewrites it). ``${CLAUDE_PLUGIN_DATA}`` is only
    expanded when plugin_data is non-empty — upstream gates it on
    ``n.source`` (plugin-sourced components only), so a non-plugin body that
    mentions it keeps the literal text.
    """
    result = str(text)
    if skill_dir:
        result = result.replace("${CLAUDE_SKILL_DIR}", _posix(skill_dir))
    if project_dir:
        result = result.replace("${CLAUDE_PROJECT_DIR}", _posix(project_dir))
    if plugin_root:
        result = result.replace("${CLAUDE_PLUGIN_ROOT}", _posix(plugin_root))
    if plugin_data:
        result = result.replace("${CLAUDE_PLUGIN_DATA}", _posix(plugin_data))
    if session_id:
        result = result.replace("${CLAUDE_SESSION_ID}", str(session_id))
    if effort:
        result = result.replace("${CLAUDE_EFFORT}", str(effort))
    return result


def _posix(path: str) -> str:
    return str(path).replace("\\", "/")
