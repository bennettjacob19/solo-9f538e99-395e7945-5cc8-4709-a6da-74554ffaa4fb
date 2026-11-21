"""Shortest-library-list solver built on the ``ld`` semantics simulator.

Given the main object, the ordered archive list (max six) and a set of hook
function names, the solver:

1. Simulates the *original* full link and rejects the request when GNU ld
   would fail (unresolved strong symbol, multiple definition, ...).
2. Records every hook's original provider: either the main object or a
   concrete ``library(member)`` -- the exact object ld binds the hook to,
   including weak-to-strong rebinding effects.
3. Enumerates library subsets by increasing size (libraries may only be
   deleted as whole groups; surviving libraries keep their original relative
   order), keeps only subsets that still link and whose hooks are still
   provided by the *same* object as in the original link, and returns the
   shortest one.
4. Breaks ties by the lexicographically smallest tuple of original library
   indices.  ``itertools.combinations`` already yields index tuples in
   lexicographic order, so the first feasible tuple of a given size wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from .linker import (
    Archive,
    ArchiveMember,
    LinkResult,
    ObjectSyms,
    simulate,
)


@dataclass
class HookResolution:
    name: str
    provider: str           # "main.o" or "libx.a(member.o)"
    binding: str            # "strong" | "weak" | "common"
    source: str             # "main" | "library"
    library_index: int | None = None
    library_name: str | None = None
    member_name: str | None = None


@dataclass
class TrimPlan:
    ok: bool
    # Indices (into the original order) of the libraries to keep.
    keep_indices: list[int] = field(default_factory=list)
    keep_libraries: list[str] = field(default_factory=list)
    removed_libraries: list[str] = field(default_factory=list)
    hooks: list[HookResolution] = field(default_factory=list)
    # Members extracted under the chosen minimal link.
    extracted_members: list[str] = field(default_factory=list)
    weak_unresolved: list[str] = field(default_factory=list)
    candidates_checked: int = 0
    error: str | None = None
    error_kind: str | None = (
        None  # "link_failed" | "hook_unresolved" | "hook_rebound"
    )


def _split_provider(label: str) -> tuple[str, str]:
    open_paren = label.index("(")
    return label[:open_paren], label[open_paren + 1: label.rindex(")")]


def _member_label(member: ArchiveMember) -> str:
    return f"{member.library_name}({member.member_name})"


def _hook_resolution(
    name: str,
    result: LinkResult,
    main_label: str,
    archives: list[Archive],
) -> HookResolution | None:
    """Locate the provider of ``name`` in a completed (successful) link."""
    provider = result.providers.get(name)
    if provider is None:
        return None
    binding = result.bindings.get(name, "strong")
    if provider == main_label:
        return HookResolution(
            name=name,
            provider=provider,
            binding=binding,
            source="main",
        )
    lib_name, member_name = _split_provider(provider)
    library = next((a for a in archives if a.name == lib_name), None)
    return HookResolution(
        name=name,
        provider=provider,
        binding=binding,
        source="library",
        library_index=library.index if library is not None else None,
        library_name=lib_name,
        member_name=member_name,
    )


def solve(
    main_object: ObjectSyms,
    archives: list[Archive],
    hooks: list[str],
    main_label: str = "main.o",
) -> TrimPlan:
    """Compute the shortest safe library list for the requested hooks."""
    n = len(archives)
    names = [a.name for a in archives]

    # ---- Step 1: the original full link must succeed ----------------------
    original = simulate(main_object, archives, main_label=main_label)
    if not original.ok:
        return TrimPlan(
            ok=False,
            error=f"original manifest does not link: {original.error}",
            error_kind="link_failed",
        )

    # ---- Step 2: every hook must resolve; record its original provider ----
    original_hooks: dict[str, HookResolution] = {}
    for hook in hooks:
        resolution = _hook_resolution(hook, original, main_label, archives)
        if resolution is None:
            return TrimPlan(
                ok=False,
                error=(
                    f"hook '{hook}' cannot be resolved in the original link: "
                    "no object defines it"
                ),
                error_kind="hook_unresolved",
            )
        original_hooks[hook] = resolution

    # ---- Step 3: subsets by increasing size, lexicographic within a size ---
    best: tuple[int, ...] | None = None
    best_result: LinkResult | None = None
    checked = 0
    for size in range(0, n + 1):
        for combo in combinations(range(n), size):
            checked += 1
            subset = [archives[i] for i in combo]
            result = simulate(main_object, subset, main_label=main_label)
            if not result.ok:
                continue
            providers = result.providers
            if all(
                providers.get(hook) == original_hooks[hook].provider
                for hook in hooks
            ):
                best, best_result = combo, result
                break
        if best is not None:
            break

    if best is None:  # pragma: no cover - full set always qualifies
        return TrimPlan(
            ok=False,
            error="no library subset preserves the hooks (internal inconsistency)",
            error_kind="hook_rebound",
        )

    keep_set = set(best)
    assert best_result is not None
    return TrimPlan(
        ok=True,
        keep_indices=list(best),
        keep_libraries=[names[i] for i in best],
        removed_libraries=[names[i] for i in range(n) if i not in keep_set],
        hooks=[original_hooks[h] for h in hooks],
        extracted_members=[_member_label(m) for m in best_result.extracted],
        weak_unresolved=list(best_result.unresolved_weak),
        candidates_checked=checked,
    )
