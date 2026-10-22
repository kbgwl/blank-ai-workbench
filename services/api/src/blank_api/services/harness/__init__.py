"""Project harness: versioned agent instructions plus an offline optimizer.

The package has four independent pieces, each usable on its own:

* ``spec``    — the c1-c7 component surface, validation, rendering and diffing
* ``library`` — per-domain experience library carried into the next task
* ``score``   — the five-part task score shared by every evaluator
* ``optimize``— Propose / Probe / Compose: generate candidates, measure them,
  keep the winner

Nothing here imports the runtime, so the offline tools can be exercised without
starting the API.
"""

from blank_api.services.harness.spec import (
    HARNESS_DIR,
    SURFACE,
    TOTAL_CHAR_LIMIT,
    HarnessError,
    component_for,
    diff_tree,
    load_workspace_tree,
    normalize_rel,
    read_tree,
    render,
    summarize,
    validate,
    write_tree,
)

__all__ = [
    "HARNESS_DIR",
    "SURFACE",
    "TOTAL_CHAR_LIMIT",
    "HarnessError",
    "component_for",
    "diff_tree",
    "load_workspace_tree",
    "normalize_rel",
    "read_tree",
    "render",
    "summarize",
    "validate",
    "write_tree",
]
