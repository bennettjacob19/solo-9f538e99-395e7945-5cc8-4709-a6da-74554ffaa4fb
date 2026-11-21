"""GNU ``ld`` static (archive) linking semantics, simulated in pure Python.

The firmware release pipeline feeds this module:

* one *main* relocatable object (the ``.o`` produced by ``/compile``), and
* up to six ordinary static libraries (``.a`` archives), in the exact order
  of the original link command.

Only the subset of ``ld`` behaviour that matters for bare-metal static links
is modelled.  The model was cross-checked against GNU ld 2.40 with real
object/archive fixtures (see ``tests/test_linker_sim.py``); every rule below
mirrors an observed ``ld`` behaviour:

1. Inputs are scanned left to right.  The main object is always included; an
   archive is only visited when its turn comes.
2. When an archive is visited, members that define a symbol which is *still
   open* -- a strong undefined reference (``U``) or an unbound common
   allocation (``C``) -- are extracted; the archive is rescanned until no
   more members qualify.  Earlier archives are never revisited, so a
   dependency pointing backwards stays undefined.  A common symbol in the
   main object therefore pulls a member that gives it a strong definition.
3. A *weak undefined* reference (``nm`` type ``w``/``v``) never extracts an
   archive member.  It is silently satisfied by any defined (weak or strong)
   symbol of the same name encountered later.
4. Local symbols (lowercase section types other than ``w``/``v``) are
   invisible outside their own object and never appear in an archive index.
5. Symbol binding once objects are included:

   * a strong definition overrides an earlier weak definition or common
     allocation ("rebinding");
   * a weak definition never overrides an earlier strong definition;
   * common (``C``) allocations merge with each other and are overridden by
     any strong definition;
   * two strong definitions of one symbol are a ``multiple definition``
     error, naming both providers.
6. After every input has been scanned, remaining **strong** undefined
   references make the link fail (weak undefined references are allowed to
   stay unresolved, matching a bare-metal link).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from enum import Enum


# ---------------------------------------------------------------------------
# Symbol table model
# ---------------------------------------------------------------------------


class SymKind(str, Enum):
    """Binding/visibility of a symbol inside a relocatable object."""

    STRONG = "strong"          # defined global: T, D, B, R, A, ... (upper case)
    WEAK_DEF = "weak_def"      # defined weak: W / V (upper case)
    COMMON = "common"          # common allocation: C (tentative definition)
    UNDEF = "undef"            # strong undefined reference: U
    WEAK_UNDEF = "weak_undef"  # weak undefined reference: w / v (lower case)
    LOCAL = "local"            # local symbol, never visible outside the object


# Upper-case nm type letters that describe a *defined* global symbol.
_DEFINED_GLOBAL_TYPES = {
    "T", "D", "B", "R", "A", "G", "S", "W", "V", "I", "N", "P",
}
# Lower-case nm type letters for defined locals (t, d, b, r, g, s, n, ...).
_LOCAL_TYPES = {"t", "d", "b", "r", "g", "s", "n", "a"}


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: SymKind


@dataclass
class ObjectSyms:
    """Parsed symbol table of one relocatable object."""

    name: str  # file name (main object or archive member)
    symbols: list[Symbol] = field(default_factory=list)

    def defined_globals(self) -> list[Symbol]:
        """Symbols this object contributes to the global symbol table."""
        return [
            s for s in self.symbols
            if s.kind in (SymKind.STRONG, SymKind.WEAK_DEF, SymKind.COMMON)
        ]

    def strong_undefined(self) -> list[str]:
        return [s.name for s in self.symbols if s.kind == SymKind.UNDEF]

    def weak_undefined(self) -> list[str]:
        return [s.name for s in self.symbols if s.kind == SymKind.WEAK_UNDEF]


def _classify(type_letter: str) -> SymKind:
    if type_letter in ("W", "V"):
        return SymKind.WEAK_DEF
    if type_letter in ("w", "v"):
        return SymKind.WEAK_UNDEF
    if type_letter == "U":
        return SymKind.UNDEF
    if type_letter in ("C", "c"):  # 'c' is unused in practice but keep parity
        return SymKind.COMMON
    if type_letter in _DEFINED_GLOBAL_TYPES:
        return SymKind.STRONG
    if type_letter in _LOCAL_TYPES:
        return SymKind.LOCAL
    # Unknown letter: treat as local so it can never affect link resolution.
    return SymKind.LOCAL


def _symbol_from_line(line: str) -> Symbol | None:
    """Parse one ``nm -P`` line: ``name type [value [size]]``.

    Symbol names may contain exotic characters, so only the first two
    whitespace-separated tokens are trusted.
    """
    parts = line.split()
    if len(parts) < 2 or len(parts[1]) != 1:
        return None
    return Symbol(name=parts[0], kind=_classify(parts[1]))


def parse_object_nm(nm_text: str, object_name: str) -> ObjectSyms:
    """Parse the ``nm -P`` output of a single standalone object file."""
    symbols: list[Symbol] = []
    for line in nm_text.splitlines():
        line = line.strip()
        if not line or line.endswith(":"):
            continue
        sym = _symbol_from_line(line)
        if sym is not None:
            symbols.append(sym)
    return ObjectSyms(name=object_name, symbols=symbols)


def parse_archive_nm(
    nm_text: str, library_index: int, library_name: str
) -> "Archive":
    """Parse the per-member symbol tables contained in one ``.a`` file."""
    members: list[ArchiveMember] = []
    index: dict[str, list[ArchiveMember]] = {}
    current: ObjectSyms | None = None
    current_member: ArchiveMember | None = None

    for raw_line in nm_text.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if line.endswith(":") and "[" in line:
            # Archive member header, e.g. "libfoo.a[bar.o]:"
            member_name = line[line.index("[") + 1: line.rindex("]")]
            current = ObjectSyms(name=member_name)
            current_member = ArchiveMember(
                library_index=library_index,
                library_name=library_name,
                member_name=member_name,
                symbols=current,
            )
            members.append(current_member)
            continue
        if current is None or current_member is None:
            # Symbols before the first member header: ignore (e.g. armap is
            # only printed with --print-armap and is not part of -P output).
            continue
        sym = _symbol_from_line(line)
        if sym is None:
            continue
        current.symbols.append(sym)
        if sym.kind in (SymKind.STRONG, SymKind.WEAK_DEF, SymKind.COMMON):
            index.setdefault(sym.name, []).append(current_member)

    return Archive(
        index=library_index, name=library_name, members=members, defined_index=index
    )


# ---------------------------------------------------------------------------
# Link simulation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveMember:
    """A member object inside one of the uploaded archives."""

    library_index: int   # position of the library in the original order (0-based)
    library_name: str
    member_name: str     # unique within its library (guaranteed by the API)
    symbols: ObjectSyms


@dataclass(frozen=True)
class Archive:
    index: int
    name: str
    members: list[ArchiveMember]
    # symbol name -> members defining it globally (archive member order)
    defined_index: dict[str, list[ArchiveMember]]


@dataclass
class LinkError(Exception):
    """A link failure, with a message that names the offending symbol."""

    reason: str
    symbol: str | None = None
    providers: tuple[str, ...] = ()

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.reason


@dataclass
class LinkResult:
    ok: bool
    # Provider ("main.o" or "libx.a(member.o)") for every defined global
    # symbol in the final link.
    providers: dict[str, str]
    # Archive members that were actually extracted, in extraction order.
    extracted: list[ArchiveMember]
    # Binding ("strong" | "weak" | "common") of every symbol in providers.
    bindings: dict[str, str] = field(default_factory=dict)
    # Strong undefined symbols left after the full scan (empty on success).
    unresolved: list[str] = field(default_factory=list)
    # Weak undefined references that never found any definition.
    unresolved_weak: list[str] = field(default_factory=list)
    error: str | None = None


# Binding strength ordering used when definitions meet.
_STRENGTH = {SymKind.WEAK_DEF: 1, SymKind.COMMON: 2, SymKind.STRONG: 3}


class _LinkEngine:
    """Stateful accumulator implementing the ld rules from the module docstring."""

    def __init__(self, main: ObjectSyms, archives: list[Archive], main_label: str) -> None:
        self._main = main
        self._archives = archives
        self._main_label = main_label
        # Symbol name -> provider label for every symbol currently defined.
        self._provider: dict[str, str] = {}
        self._strength: dict[str, int] = {}
        self._extracted: list[ArchiveMember] = []
        self._extracted_ids: set[int] = set()
        self._strong_undef: set[str] = set()
        self._weak_undef: set[str] = set()
        # Names whose current definition is merely a common allocation: they
        # stay "open" and still pull members offering a real definition.
        self._open_common: set[str] = set()

    # -- symbol bookkeeping -------------------------------------------------

    def _add_undefined(self, name: str, weak: bool) -> None:
        if name in self._provider:
            return  # already defined somewhere earlier
        if weak:
            self._weak_undef.add(name)
        else:
            self._strong_undef.add(name)
            # A strong reference upgrades a pending weak one.
            self._weak_undef.discard(name)

    def _define(self, name: str, kind: SymKind, label: str) -> None:
        new_strength = _STRENGTH[kind]
        existing = self._strength.get(name)
        if existing is None:
            # First definition: any pending undefined reference is now met.
            self._provider[name] = label
            self._strength[name] = new_strength
            self._strong_undef.discard(name)
            self._weak_undef.discard(name)
            if kind == SymKind.COMMON:
                self._open_common.add(name)
            return
        if existing == 3 and new_strength == 3:
            raise LinkError(
                reason=(
                    f"multiple definition of '{name}': "
                    f"{self._provider[name]} and {label}"
                ),
                symbol=name,
                providers=(self._provider[name], label),
            )
        if new_strength > existing:
            # Strong beats common beats weak: rebind to the new provider.
            self._provider[name] = label
            self._strength[name] = new_strength
        # equal or weaker: keep the earlier provider (two weak defs / two
        # commons merge silently and the first one wins).
        if self._strength[name] >= 3:
            # Bound to a strong definition: no longer open.
            self._open_common.discard(name)

    def _include_object(self, obj: ObjectSyms, label: str) -> None:
        # Definitions are processed first so an object that both defines and
        # references a symbol is self-sufficient.
        for sym in obj.defined_globals():
            self._define(sym.name, sym.kind, label)
        for name in obj.strong_undefined():
            self._add_undefined(name, weak=False)
        for name in obj.weak_undefined():
            self._add_undefined(name, weak=True)

    def _include_member(self, member: ArchiveMember) -> None:
        if id(member) in self._extracted_ids:
            return
        self._extracted_ids.add(id(member))
        self._extracted.append(member)
        self._include_object(
            member.symbols,
            f"{member.library_name}({member.member_name})",
        )

    # -- archive scanning ---------------------------------------------------

    def _scan_archive(self, archive: Archive) -> None:
        """Repeatedly walk members until no new member qualifies.

        Extraction triggers (matching GNU ld):

        * a member defining a symbol that is a strong undefined reference is
          extracted regardless of whether its definition is strong or weak;
        * an open common allocation only pulls members that give it a
          *strong* definition or another common allocation -- a weak
          definition does not qualify (verified against GNU ld 2.40).

        Weak undefined references never trigger extraction (ld rule 3).
        """
        while True:
            progress = False
            strong_undef = self._strong_undef
            open_common = self._open_common
            for member in archive.members:
                if id(member) in self._extracted_ids:
                    continue
                qualifies = False
                for sym in member.symbols.defined_globals():
                    if sym.name in strong_undef:
                        qualifies = True
                        break
                    if sym.name in open_common and sym.kind in (
                        SymKind.STRONG,
                        SymKind.COMMON,
                    ):
                        qualifies = True
                        break
                if qualifies:
                    self._include_member(member)
                    progress = True
            if not progress:
                return

    @staticmethod
    def _binding_label(strength: int) -> str:
        return {1: "weak", 2: "common", 3: "strong"}[strength]

    def run(self) -> LinkResult:
        def current_bindings() -> dict[str, str]:
            return {
                name: self._binding_label(strength)
                for name, strength in self._strength.items()
            }
        try:
            self._include_object(self._main, self._main_label)
            for archive in self._archives:
                self._scan_archive(archive)
        except LinkError as exc:
            return LinkResult(
                ok=False,
                providers=dict(self._provider),
                extracted=list(self._extracted),
                bindings=current_bindings(),
                unresolved=sorted(self._strong_undef),
                unresolved_weak=sorted(self._weak_undef),
                error=exc.reason,
            )

        if self._strong_undef:
            names = ", ".join(sorted(self._strong_undef))
            return LinkResult(
                ok=False,
                providers=dict(self._provider),
                extracted=list(self._extracted),
                bindings=current_bindings(),
                unresolved=sorted(self._strong_undef),
                unresolved_weak=sorted(self._weak_undef),
                error=f"undefined reference to: {names}",
            )
        return LinkResult(
            ok=True,
            providers=dict(self._provider),
            extracted=list(self._extracted),
            bindings=current_bindings(),
            unresolved=[],
            unresolved_weak=sorted(self._weak_undef),
        )


# ---------------------------------------------------------------------------
# Public helpers: binutils invocation
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """``nm``/``readelf`` failed on an uploaded artifact."""


def run_nm(path: str, nm_bin: str = "nm") -> str:
    """Return ``nm -P`` text for an object or an archive."""
    proc = subprocess.run(
        [nm_bin, "-P", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise ToolError(
            f"nm failed on {path}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def read_elf_header(path: str, readelf_bin: str = "readelf") -> str:
    proc = subprocess.run(
        [readelf_bin, "-h", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise ToolError(
            f"readelf -h failed on {path}: "
            f"{proc.stderr.strip() or 'not an ELF file'}"
        )
    return proc.stdout


def read_elf_sections(path: str, readelf_bin: str = "readelf") -> str:
    proc = subprocess.run(
        [readelf_bin, "-S", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise ToolError(
            f"readelf -S failed on {path}: "
            f"{proc.stderr.strip() or 'not an ELF file'}"
        )
    return proc.stdout


def build_archives(
    archive_nm_texts: list[tuple[str, str]],
) -> list[Archive]:
    """Turn ``[(library_name, nm_text), ...]`` (already in link order) into archives."""
    return [
        parse_archive_nm(text, i, lib_name)
        for i, (lib_name, text) in enumerate(archive_nm_texts)
    ]


def simulate(
    main_object: ObjectSyms,
    archives: list[Archive],
    main_label: str = "main.o",
) -> LinkResult:
    """Run the static-link simulation and return the full result."""
    return _LinkEngine(main_object, archives, main_label).run()
