"""Accept / reject a proposed rule change. The dashboard's only write path.

Hard constraint #3 gives the Analyst exactly one job and forbids it another:
it may *propose* a rule change into ``config/rules.proposed.yaml``, and it may
never apply one. Applying is a human action, and this module is the mechanism
that human uses.

Why the patch is spliced textually instead of round-tripped through PyYAML
-------------------------------------------------------------------------
``rules.yaml`` is more comment than code. The classifier's precedence rules,
the alert grammar, the worked example for denial-reason bucketing -- all of it
is documentation an operator relies on six months from now. ``yaml.safe_load``
followed by ``yaml.safe_dump`` would silently delete every one of those lines
and reorder the service classes, and *file order is evaluation order*: that
alone would change which jobs the business takes.

So the patch is inserted into the existing text and nothing else is touched.
The result is then parsed and checked before it is allowed to replace the file:

* it must still be valid YAML and still be a mapping,
* every top-level key that was there before must still be there,
* the patch content must actually be present in the reparsed document,
* the service-class order that existed before must be preserved as a prefix.

If any check fails, nothing is written. And a timestamped copy of the file is
taken *before* every write regardless, so the previous version is always one
``copy`` away.

A conflicting patch -- one that redefines a service class or an alert id that
already exists -- is refused rather than merged. Overwriting a block would
destroy the comments attached to it, and "the machine rewrote the rule that
decides which jobs are worth taking" is the exact failure mode this whole
design exists to prevent. The operator gets the fragment and edits by hand.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..core.config_loader import CONFIG
from ..core.paths import STATE_DIR

__all__ = [
    "RulesWriteError",
    "ALLOWED_PATCH_TARGETS",
    "BACKUP_DIR",
    "accept_proposal",
    "reject_proposal",
    "apply_patch_to_text",
    "backup_file",
    "list_backups",
]


class RulesWriteError(RuntimeError):
    """The patch could not be applied safely. Nothing was written."""


#: Sections of rules.yaml a proposal is allowed to touch. ``version`` is
#: excluded on purpose: it is the human's label for a change, not the
#: Analyst's, and rules_version() already changes on every edit via the hash.
ALLOWED_PATCH_TARGETS = frozenset(
    {
        "service_classes",
        "acceptance_policy",
        "alerts",
        "denial_reason_normalization",
        "dashboard",
    }
)

BACKUP_DIR: Path = STATE_DIR / "rules_backups"

#: A top-level key: no indentation, not a comment, not a list item.
_TOP_LEVEL_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)\s*:(.*)$")

_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


# ==========================================================================
# Backups
# ==========================================================================


def backup_file(path: Path) -> Path:
    """Copy ``path`` into state/rules_backups/ with a UTC timestamp.

    Called before *every* write, accept or reject, success or later failure.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = BACKUP_DIR / f"{path.stem}.{stamp}{path.suffix}"
    counter = 1
    while target.exists():
        target = BACKUP_DIR / f"{path.stem}.{stamp}.{counter}{path.suffix}"
        counter += 1
    shutil.copy2(path, target)
    return target


def list_backups(limit: int = 20) -> list[dict[str, Any]]:
    """Most recent backups, newest first. For the Rules view footer."""
    if not BACKUP_DIR.exists():
        return []
    entries = []
    for path in BACKUP_DIR.iterdir():
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": path.name,
                "path": str(path),
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime),
            }
        )
    entries.sort(key=lambda entry: entry["modified"], reverse=True)
    return entries[:limit]


def _atomic_write(path: Path, text: str) -> None:
    """Write via a sibling temp file + os.replace so a crash cannot truncate."""
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temp, path)


# ==========================================================================
# Text splicing
# ==========================================================================


def _top_level_keys(lines: list[str]) -> list[tuple[str, int]]:
    """``[(key, line index)]`` for every top-level mapping key in the file."""
    found: list[tuple[str, int]] = []
    for index, line in enumerate(lines):
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        match = _TOP_LEVEL_KEY.match(line)
        if match:
            found.append((match.group(1), index))
    return found


def _section_bounds(lines: list[str], key: str) -> tuple[int, int] | None:
    """``(key line index, last content line index)`` for a top-level section.

    The end deliberately skips trailing blank lines and comments: in rules.yaml
    a long comment block sits between ``alerts`` and the key it documents, and
    appending "after the section" must not mean "into the next section's
    documentation".
    """
    keys = _top_level_keys(lines)
    for position, (name, start) in enumerate(keys):
        if name != key:
            continue
        stop = keys[position + 1][1] if position + 1 < len(keys) else len(lines)
        end = start
        for index in range(stop - 1, start - 1, -1):
            stripped = lines[index].strip()
            if stripped and not stripped.startswith("#"):
                end = index
                break
        return start, end
    return None


def _child_indent(lines: list[str], start: int, end: int) -> int:
    """Indent width used by the section's children, defaulting to 2."""
    for index in range(start + 1, end + 1):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent > 0:
            return indent
    return 2


def _dump_fragment(value: Any) -> str:
    """YAML for a fragment, block style for mappings, flow style for leaf lists.

    ``default_flow_style=None`` gives ``match_any: [tow, recovery]`` -- the
    style rules.yaml already uses -- while keeping nested mappings readable.
    """
    return yaml.safe_dump(
        value,
        sort_keys=False,
        default_flow_style=None,
        allow_unicode=True,
        width=88,
    ).rstrip("\n")


def _indent_block(text: str, indent: int) -> list[str]:
    pad = " " * indent
    return [(pad + line) if line.strip() else "" for line in text.split("\n")]


def _existing_child_keys(lines: list[str], start: int, end: int, indent: int) -> list[str]:
    """Mapping keys defined directly under a section."""
    pattern = re.compile(r"^ {%d}([A-Za-z_][A-Za-z0-9_.-]*)\s*:" % indent)
    keys = []
    for index in range(start + 1, end + 1):
        match = pattern.match(lines[index])
        if match:
            keys.append(match.group(1))
    return keys


def _insert_into_mapping(
    lines: list[str], section: str, fragment: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Insert new keys under an existing mapping section."""
    bounds = _section_bounds(lines, section)
    if bounds is None:
        return _append_new_section(lines, section, fragment), list(fragment)
    start, end = bounds
    indent = _child_indent(lines, start, end)
    existing = _existing_child_keys(lines, start, end, indent)

    conflicts = [key for key in fragment if key in existing]
    if conflicts:
        raise RulesWriteError(
            f"{section}: {', '.join(sorted(conflicts))} already defined in rules.yaml. "
            "Refusing to overwrite an existing block -- edit config/rules.yaml by hand "
            "so the comments attached to it survive."
        )

    # Directive keys (a leading underscore, e.g. `_default`) stay last.
    insert_at = end + 1
    directive = re.compile(r"^ {%d}_[A-Za-z0-9_]*\s*:" % indent)
    for index in range(start + 1, end + 1):
        if directive.match(lines[index]):
            insert_at = index
            break

    block: list[str] = []
    for key, value in fragment.items():
        block.extend(_indent_block(_dump_fragment({key: value}), indent))

    return lines[:insert_at] + block + lines[insert_at:], list(fragment)


def _insert_into_sequence(
    lines: list[str], section: str, items: list[Any]
) -> tuple[list[str], list[str]]:
    """Append items to an existing sequence section, handling ``key: []``."""
    bounds = _section_bounds(lines, section)
    if bounds is None:
        return _append_new_section(lines, section, items), [_item_label(i) for i in items]
    start, end = bounds
    lines = list(lines)

    match = _TOP_LEVEL_KEY.match(lines[start])
    inline = (match.group(2) if match else "").strip()
    if inline in ("[]", "[ ]"):
        # `denial_reason_normalization: []` -> open the block up.
        lines[start] = f"{section}:"
        end = start
        indent = 2
    else:
        indent = _child_indent(lines, start, end)
        if indent == 0:
            indent = 2

    existing_ids = set()
    for line in lines[start : end + 1]:
        id_match = re.match(r"^\s*-\s*id\s*:\s*(.+?)\s*$", line)
        if id_match:
            existing_ids.add(id_match.group(1).strip().strip("\"'"))

    labels = []
    block: list[str] = []
    for item in items:
        if isinstance(item, dict) and str(item.get("id") or "") in existing_ids:
            raise RulesWriteError(
                f"{section}: id '{item['id']}' already exists in rules.yaml. "
                "Refusing to overwrite -- edit config/rules.yaml by hand."
            )
        block.extend(_indent_block(_dump_fragment([item]), indent))
        labels.append(_item_label(item))

    insert_at = end + 1
    return lines[:insert_at] + block + lines[insert_at:], labels


def _item_label(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("id") or next(iter(item), "item"))
    return str(item)


def _append_new_section(lines: list[str], section: str, value: Any) -> list[str]:
    """Add a section that rules.yaml does not have yet, at the end of the file."""
    block = _dump_fragment({section: value}).split("\n")
    tail = list(lines)
    while tail and not tail[-1].strip():
        tail.pop()
    return tail + ["", *block]


def _deep_contains(container: Any, subset: Any) -> bool:
    """True if every leaf of ``subset`` is present in ``container``."""
    if isinstance(subset, dict):
        if not isinstance(container, dict):
            return False
        return all(key in container and _deep_contains(container[key], value) for key, value in subset.items())
    if isinstance(subset, list):
        if not isinstance(container, list):
            return False
        return all(any(_deep_contains(candidate, item) for candidate in container) for item in subset)
    return container == subset


def apply_patch_to_text(text: str, patch: dict[str, Any]) -> tuple[str, list[str]]:
    """Splice ``patch`` into rules.yaml text. Returns ``(new text, labels)``.

    Pure function -- it touches no files, which is what makes it testable and
    what lets the caller validate the result before committing to it.
    """
    if not isinstance(patch, dict) or not patch:
        raise RulesWriteError("Proposal has no patch fragment to apply.")

    unknown = sorted(set(patch) - ALLOWED_PATCH_TARGETS)
    if unknown:
        raise RulesWriteError(
            "Patch targets a section that may not be written from the dashboard: "
            + ", ".join(unknown)
            + ". Allowed: "
            + ", ".join(sorted(ALLOWED_PATCH_TARGETS))
            + "."
        )

    original = yaml.safe_load(text) or {}
    if not isinstance(original, dict):
        raise RulesWriteError("config/rules.yaml is not a YAML mapping; refusing to edit it.")
    original_top = list(original)
    original_classes = list((original.get("service_classes") or {}))

    lines = text.split("\n")
    labels: list[str] = []
    for section, value in patch.items():
        if isinstance(value, dict):
            lines, added = _insert_into_mapping(lines, section, value)
        elif isinstance(value, list):
            lines, added = _insert_into_sequence(lines, section, value)
        else:
            raise RulesWriteError(
                f"Patch for '{section}' must be a mapping or a list, got {type(value).__name__}."
            )
        labels.extend(f"{section}.{item}" for item in added)

    new_text = "\n".join(lines)
    if not new_text.endswith("\n"):
        new_text += "\n"

    # ---- validation: nothing is written unless all of this holds ------
    try:
        reparsed = yaml.safe_load(new_text)
    except yaml.YAMLError as exc:
        raise RulesWriteError(f"Patched rules.yaml is not valid YAML: {exc}") from exc
    if not isinstance(reparsed, dict):
        raise RulesWriteError("Patched rules.yaml did not reparse as a mapping.")

    missing = [key for key in original_top if key not in reparsed]
    if missing:
        raise RulesWriteError(f"Patch would drop existing section(s): {', '.join(missing)}.")

    if not _deep_contains(reparsed, patch):
        raise RulesWriteError(
            "Patch did not survive the round trip -- the fragment is not present in the "
            "reparsed file. Nothing was written."
        )

    new_classes = list(reparsed.get("service_classes") or {})
    if original_classes and new_classes[: len(original_classes)] != original_classes:
        # File order is evaluation order; a reordering silently changes which
        # jobs classify as what.
        preserved = [name for name in new_classes if name in original_classes]
        if preserved != original_classes:
            raise RulesWriteError(
                "Patch would reorder service_classes. File order is evaluation order, "
                "so this is refused."
            )

    return new_text, labels


# ==========================================================================
# Proposal file
# ==========================================================================


def _leading_comment_block(text: str) -> str:
    """The documentation header at the top of a config file, verbatim."""
    header: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            break
        header.append(line)
    while header and not header[-1].strip():
        header.pop()
    return "\n".join(header)


def _write_proposals(path: Path, document: dict[str, Any]) -> None:
    """Rewrite rules.proposed.yaml, keeping its documentation header."""
    header = _leading_comment_block(path.read_text(encoding="utf-8"))
    body = yaml.safe_dump(
        document,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=88,
    )
    text = (header + "\n\n" if header else "") + body
    _atomic_write(path, text)


def _load_proposals(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise RulesWriteError("config/rules.proposed.yaml is not a YAML mapping.")
    document.setdefault("version", 1)
    proposals = document.get("proposals")
    if proposals is None:
        document["proposals"] = []
    elif not isinstance(proposals, list):
        raise RulesWriteError("config/rules.proposed.yaml: 'proposals' must be a list.")
    return document


def _find_proposal(document: dict[str, Any], proposal_id: str) -> dict[str, Any]:
    for entry in document["proposals"]:
        if isinstance(entry, dict) and str(entry.get("id") or "") == proposal_id:
            return entry
    raise RulesWriteError(f"No proposal with id '{proposal_id}'.")


def _stamp_review(entry: dict[str, Any], status: str, reviewer: str) -> None:
    entry["status"] = status
    entry["reviewed_by"] = reviewer
    entry["reviewed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat() + "Z"


def _validate_id(proposal_id: str) -> str:
    proposal_id = (proposal_id or "").strip()
    if not _VALID_ID.match(proposal_id):
        raise RulesWriteError("Invalid proposal id.")
    return proposal_id


# ==========================================================================
# Public actions
# ==========================================================================


def accept_proposal(proposal_id: str, reviewer: str = "dashboard") -> dict[str, Any]:
    """Splice a proposal's patch into rules.yaml and mark it accepted.

    Order matters: rules.yaml is written first. If that fails, the proposal is
    left pending and can be retried. If the proposal-file update then fails,
    the operator is told explicitly that the rule *is* live but the proposal
    still reads pending -- an inconsistency you can see beats one you cannot.
    """
    proposal_id = _validate_id(proposal_id)
    proposed_path = CONFIG.path_for("rules_proposed")
    rules_path = CONFIG.path_for("rules")

    document = _load_proposals(proposed_path)
    entry = _find_proposal(document, proposal_id)

    status = str(entry.get("status") or "pending").casefold()
    if status != "pending":
        raise RulesWriteError(f"Proposal '{proposal_id}' is already {status}.")

    patch = entry.get("patch")
    if not isinstance(patch, dict) or not patch:
        raise RulesWriteError(f"Proposal '{proposal_id}' carries no patch fragment.")

    current = rules_path.read_text(encoding="utf-8")
    new_text, labels = apply_patch_to_text(current, patch)

    rules_backup = backup_file(rules_path)
    _atomic_write(rules_path, new_text)

    warning = None
    try:
        proposals_backup = backup_file(proposed_path)
        _stamp_review(entry, "accepted", reviewer)
        _write_proposals(proposed_path, document)
    except Exception as exc:  # pragma: no cover - filesystem dependent
        proposals_backup = None
        warning = (
            f"rules.yaml was updated successfully, but rules.proposed.yaml could not be "
            f"marked accepted ({type(exc).__name__}: {exc}). Edit its status by hand."
        )

    CONFIG.reload()

    return {
        "ok": True,
        "action": "accepted",
        "proposal_id": proposal_id,
        "applied": labels,
        "rules_backup": str(rules_backup),
        "proposals_backup": str(proposals_backup) if proposals_backup else None,
        "rules_version": CONFIG.rules_version(),
        "warning": warning,
        "message": (
            f"Applied {', '.join(labels) if labels else 'patch'} to rules.yaml. "
            "Run `python -m towbook_agent backfill` to reclassify stored requests "
            "against the new rules."
        ),
    }


def reject_proposal(proposal_id: str, reviewer: str = "dashboard") -> dict[str, Any]:
    """Mark a proposal rejected. rules.yaml is not touched."""
    proposal_id = _validate_id(proposal_id)
    proposed_path = CONFIG.path_for("rules_proposed")

    document = _load_proposals(proposed_path)
    entry = _find_proposal(document, proposal_id)

    status = str(entry.get("status") or "pending").casefold()
    if status != "pending":
        raise RulesWriteError(f"Proposal '{proposal_id}' is already {status}.")

    backup = backup_file(proposed_path)
    _stamp_review(entry, "rejected", reviewer)
    _write_proposals(proposed_path, document)
    CONFIG.reload("rules_proposed")

    return {
        "ok": True,
        "action": "rejected",
        "proposal_id": proposal_id,
        "applied": [],
        "rules_backup": None,
        "proposals_backup": str(backup),
        "rules_version": CONFIG.rules_version(),
        "warning": None,
        "message": f"Proposal '{proposal_id}' rejected. rules.yaml unchanged.",
    }
