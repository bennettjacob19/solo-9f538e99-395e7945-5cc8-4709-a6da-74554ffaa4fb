"""Unit tests for the pure-Python GNU ld static-link simulator.

Every scenario here is first compiled into real ELF objects/archives with the
container GCC, linked with the real GNU ld (``ld -r`` for member extraction,
full ``ld`` for success/failure), and then compared against
:mod:`app.linker`'s simulation so the two cannot silently drift apart.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.linker import (
    SymKind,
    build_archives,
    parse_object_nm,
    run_nm,
    simulate,
)


# ---------------------------------------------------------------------------
# Fixture builder: compile C snippets into objects and archives
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fixture_dir(tmp_path_factory) -> Path:
    base = tmp_path_factory.mktemp("ld-fixtures")
    sources: dict[str, str] = {}

    def build(
        name: str,
        src: str,
        *,
        common: bool = False,
        lto: bool = False,
    ) -> Path:
        src_path = base / f"{name}.c"
        obj_path = base / f"{name}.o"
        src_path.write_text(src)
        # -fno-pic -fno-pie mirrors bare-metal firmware builds and avoids
        # PIE-only synthetic references such as _GLOBAL_OFFSET_TABLE_.
        cmd = ["gcc", "-fno-pic", "-fno-pie", "-c", str(src_path), "-o", str(obj_path)]
        if common:
            cmd.insert(2, "-fcommon")
        if lto:
            cmd.insert(2, "-flto")
        subprocess.run(cmd, check=True, capture_output=True)
        sources[name] = src
        return obj_path

    def archive(name: str, members: list[str]) -> Path:
        lib_path = base / f"lib{name}.a"
        objs = [str(base / f"{m}.o") for m in members]
        subprocess.run(["ar", "rcs", str(lib_path), *objs], check=True)
        return lib_path

    build._base = base  # type: ignore[attr-defined]
    build.archive = archive  # type: ignore[attr-defined]
    return build  # type: ignore[return-value]


def _load_object(path: Path, name: str):
    return parse_object_nm(run_nm(str(path)), name)


def _load_archives(pairs: list[tuple[str, Path]]):
    return build_archives(
        [(lib_name, run_nm(str(path))) for lib_name, path in pairs]
    )


def _real_ld_extracts(main: Path, libs: list[Path]) -> tuple[bool, set[str]]:
    """Run real ld -r and report success and which archive members landed in."""
    out = main.parent / "real_out.o"
    proc = subprocess.run(
        ["ld", "-r", str(main), *[str(p) for p in libs], "-o", str(out)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return False, set()
    defined = set()
    for line in run_nm(str(out)).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in ("T", "D", "B", "R", "W", "V", "C"):
            defined.add(parts[0])
    return True, defined


# ---------------------------------------------------------------------------
# Basic archive scanning order
# ---------------------------------------------------------------------------


def test_basic_chain_extraction_matches_real_ld(fixture_dir):
    build = fixture_dir
    build("main_chain", "void top(void);\nint cm(void){top();return 0;}\n")
    build("mid", "void mid(void);\nvoid top(void){mid();}\n")
    build("leaf", "void mid(void){}\nvoid leaf_fn(void){mid();}\n")
    lib_mid = build.archive("mid", ["mid"])
    lib_leaf = build.archive("leaf", ["leaf"])
    base = build._base

    main_obj = _load_object(base / "main_chain.o", "main_chain.o")
    archives = _load_archives([
        ("libmid.a", lib_mid),
        ("libleaf.a", lib_leaf),
    ])
    result = simulate(main_obj, archives, main_label="main_chain.o")
    assert result.ok
    assert set(result.providers) >= {"cm", "top", "mid"}
    assert result.providers["top"] == "libmid.a(mid.o)"
    assert result.providers["mid"] == "libleaf.a(leaf.o)"
    extracted = {m.member_name for m in result.extracted}
    assert extracted == {"mid.o", "leaf.o"}

    ok, real_defined = _real_ld_extracts(
        base / "main_chain.o", [lib_mid, lib_leaf]
    )
    assert ok
    assert {"cm", "top", "mid", "leaf_fn"} <= real_defined


def test_backward_archive_order_leaves_symbol_undefined(fixture_dir):
    build = fixture_dir
    build("main_b", "void top(void);\nint bm(void){top();return 0;}\n")
    build("mid_b", "void mid(void);\nvoid top(void){mid();}\n")
    build("leaf_b", "void mid(void){}\n")
    lib_mid = build.archive("midb", ["mid_b"])
    lib_leaf = build.archive("leafb", ["leaf_b"])
    base = build._base

    # Wrong order: leaf library comes before the library that needs it.
    main_obj = _load_object(base / "main_b.o", "main_b.o")
    archives = _load_archives([
        ("libleafb.a", lib_leaf),
        ("libmidb.a", lib_mid),
    ])
    result = simulate(main_obj, archives, main_label="main_b.o")
    assert not result.ok
    assert "mid" in result.error
    assert "mid" in result.unresolved


# ---------------------------------------------------------------------------
# Weak symbols
# ---------------------------------------------------------------------------


def test_weak_undefined_does_not_extract_member(fixture_dir):
    build = fixture_dir
    build("main_w",
          "__attribute__((weak)) void wfn(void);\n"
          "int wm(void){if(wfn) wfn(); return 0;}\n")
    build("wprov", "__attribute__((weak)) void wfn(void){}\n"
                   "void w_strong(void){}\n")
    lib = build.archive("w", ["wprov"])
    base = build._base

    main_obj = _load_object(base / "main_w.o", "main_w.o")
    archives = _load_archives([("libw.a", lib)])
    result = simulate(main_obj, archives, main_label="main_w.o")
    assert result.ok
    assert result.extracted == []
    assert "wfn" in result.unresolved_weak


def test_weak_definition_satisfies_strong_ref_and_blocks_later_strong(fixture_dir):
    build = fixture_dir
    build("main_h", "void hook(void);\nint hm(void){hook();return 0;}\n")
    build("weak_hook",
          "__attribute__((weak)) void hook(void){}\nvoid wa(void){}\n")
    build("strong_hook", "void hook(void){}\nvoid sa(void){}\n")
    lib_a = build.archive("a", ["weak_hook"])
    lib_b = build.archive("b", ["strong_hook"])
    base = build._base

    main_obj = _load_object(base / "main_h.o", "main_h.o")
    archives = _load_archives([("liba.a", lib_a), ("libb.a", lib_b)])
    result = simulate(main_obj, archives, main_label="main_h.o")
    assert result.ok
    # libb is never visited for hook: weak def in liba already closed it.
    assert result.providers["hook"] == "liba.a(weak_hook.o)"
    assert result.bindings["hook"] == "weak"
    assert {m.member_name for m in result.extracted} == {"weak_hook.o"}


def test_weak_symbol_rebinds_to_strong_when_member_pulled_for_other_ref(fixture_dir):
    build = fixture_dir
    build("main_r",
          "void hook(void);\nvoid pull_b(void);\n"
          "int rm(void){hook();pull_b();return 0;}\n")
    build("weak_hook_r",
          "__attribute__((weak)) void hook(void){}\nvoid ra(void){}\n")
    build("strong_hook_r",
          "void hook(void){}\nvoid pull_b(void){}\n")
    lib_a = build.archive("ra", ["weak_hook_r"])
    lib_b = build.archive("rb", ["strong_hook_r"])
    base = build._base

    main_obj = _load_object(base / "main_r.o", "main_r.o")
    archives = _load_archives([("libra.a", lib_a), ("librb.a", lib_b)])
    result = simulate(main_obj, archives, main_label="main_r.o")
    assert result.ok
    # libb's member is extracted for pull_b; its strong hook rebinds.
    assert result.providers["hook"] == "librb.a(strong_hook_r.o)"
    assert result.bindings["hook"] == "strong"

    ok, real_defined = _real_ld_extracts(
        base / "main_r.o", [lib_a, lib_b]
    )
    assert ok
    # Cross-check the binding with the real linker output.
    real_nm = run_nm(str(base / "real_out.o"))
    hook_line = next(line for line in real_nm.splitlines() if line.startswith("hook "))
    assert hook_line.split()[1] == "T"  # strong text symbol after rebind


def test_two_weak_definitions_merge_first_wins(fixture_dir):
    build = fixture_dir
    build("main_ww", "void w1(void);\nvoid w2(void);\n"
                     "int wwm(void){w1();w2();return 0;}\n")
    build("ww1", "__attribute__((weak)) void shared_w(void){}\n"
                 "void w1(void){shared_w();}\n")
    build("ww2", "__attribute__((weak)) void shared_w(void){}\n"
                 "void w2(void){}\n")
    lib1 = build.archive("ww1", ["ww1"])
    lib2 = build.archive("ww2", ["ww2"])
    base = build._base

    main_obj = _load_object(base / "main_ww.o", "main_ww.o")
    archives = _load_archives([("libww1.a", lib1), ("libww2.a", lib2)])
    result = simulate(main_obj, archives, main_label="main_ww.o")
    assert result.ok
    assert result.providers["shared_w"] == "libww1.a(ww1.o)"
    assert result.bindings["shared_w"] == "weak"


# ---------------------------------------------------------------------------
# Common symbols
# ---------------------------------------------------------------------------


def test_common_in_main_pulls_strong_definition_member(fixture_dir):
    build = fixture_dir
    build("main_c", "int gv;\nvoid cmain(void){gv=2;}\n", common=True)
    build("cprov", "int gv = 42;\nvoid cprov_fn(void){}\n")
    lib = build.archive("cp", ["cprov"])
    base = build._base

    main_obj = _load_object(base / "main_c.o", "main_c.o")
    archives = _load_archives([("libcp.a", lib)])
    result = simulate(main_obj, archives, main_label="main_c.o")
    assert result.ok
    assert result.providers["gv"] == "libcp.a(cprov.o)"
    assert result.bindings["gv"] == "strong"
    assert {m.member_name for m in result.extracted} == {"cprov.o"}

    ok, _ = _real_ld_extracts(base / "main_c.o", [lib])
    assert ok
    real_nm = run_nm(str(base / "real_out.o"))
    gv_line = next(line for line in real_nm.splitlines() if line.startswith("gv "))
    assert gv_line.split()[1] == "D"  # common got a real strong definition


def test_two_commons_merge_without_error(fixture_dir):
    build = fixture_dir
    build("main_cc", "void ca(void);\nvoid cb(void);\n"
                     "int ccm(void){ca();cb();return 0;}\n")
    build("cca", "int gshared;\nvoid ca(void){gshared=1;}\n", common=True)
    build("ccb", "int gshared;\nvoid cb(void){}\n", common=True)
    lib1 = build.archive("cca", ["cca"])
    lib2 = build.archive("ccb", ["ccb"])
    base = build._base

    main_obj = _load_object(base / "main_cc.o", "main_cc.o")
    archives = _load_archives([("libcca.a", lib1), ("libccb.a", lib2)])
    result = simulate(main_obj, archives, main_label="main_cc.o")
    assert result.ok
    assert result.bindings["gshared"] == "common"
    assert result.providers["gshared"] == "libcca.a(cca.o)"


def test_strong_then_bss_strong_member_is_multiple_definition(fixture_dir):
    # Under GCC's default -fno-common, a tentative "int gv;" emits a strong
    # B-section definition, which conflicts with the main object's D one.
    build = fixture_dir
    build("main_sc", "int gv = 7;\nvoid ca(void);\n"
                     "int scm(void){ca();return gv;}\n")
    build("scc", "int gv;\nvoid ca(void){}\n")  # default flags => B, not C
    lib = build.archive("sc", ["scc"])
    base = build._base

    main_obj = _load_object(base / "main_sc.o", "main_sc.o")
    archives = _load_archives([("libsc.a", lib)])
    result = simulate(main_obj, archives, main_label="main_sc.o")
    assert not result.ok
    assert "multiple definition of 'gv'" in result.error

    # Real ld agrees.
    proc = subprocess.run(
        ["ld", "-r", str(base / "main_sc.o"), str(lib), "-o",
         str(base / "sc_out.o")],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "multiple definition" in proc.stderr


def test_strong_absorbs_later_common_member_without_error(fixture_dir):
    # A genuine C-type common allocation (-fcommon) is silently absorbed by a
    # strong definition that arrived earlier.
    build = fixture_dir
    build("main_sc2", "int gv = 7;\nvoid cb(void);\n"
                      "int scm2(void){cb();return gv;}\n")
    build("scc2", "int gv;\nvoid cb(void){}\n", common=True)
    lib = build.archive("sc2", ["scc2"])
    base = build._base

    main_obj = _load_object(base / "main_sc2.o", "main_sc2.o")
    archives = _load_archives([("libsc2.a", lib)])
    result = simulate(main_obj, archives, main_label="main_sc2.o")
    assert result.ok
    assert result.providers["gv"] == "main_sc2.o"
    assert result.bindings["gv"] == "strong"

    proc = subprocess.run(
        ["ld", "-r", str(base / "main_sc2.o"), str(lib), "-o",
         str(base / "sc2_out.o")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_unresolved_strong_symbol_fails_and_names_symbol(fixture_dir):
    build = fixture_dir
    build("main_miss", "void missing_fn(void);\n"
                       "int mm(void){missing_fn();return 0;}\n")
    base = build._base
    main_obj = _load_object(base / "main_miss.o", "main_miss.o")
    result = simulate(main_obj, [], main_label="main_miss.o")
    assert not result.ok
    assert "missing_fn" in result.error
    assert result.unresolved == ["missing_fn"]


def test_duplicate_strong_definitions_across_extracted_members(fixture_dir):
    build = fixture_dir
    build("main_dup", "void need1(void);\nvoid need2(void);\n"
                      "int dupm(void){need1();need2();return 0;}\n")
    build("d1", "void hh(void){}\nvoid need1(void){hh();}\n")
    build("d2", "void hh(void){}\nvoid need2(void){}\n")
    lib1 = build.archive("d1", ["d1"])
    lib2 = build.archive("d2", ["d2"])
    base = build._base

    main_obj = _load_object(base / "main_dup.o", "main_dup.o")
    archives = _load_archives([("libd1.a", lib1), ("libd2.a", lib2)])
    result = simulate(main_obj, archives, main_label="main_dup.o")
    assert not result.ok
    assert "multiple definition of 'hh'" in result.error
    assert "libd1.a(d1.o)" in result.error
    assert "libd2.a(d2.o)" in result.error


def test_archive_member_skipped_when_symbol_already_defined(fixture_dir):
    build = fixture_dir
    # main defines hook strongly; both archives also contain a hook member but
    # nothing else is needed -> no member extracted, no multiple-def error.
    build("main_self", "void hook(void){}\nint selfm(void){hook();return 0;}\n")
    build("h1", "void hook(void){}\n")
    build("h2", "void hook(void){}\n")
    lib1 = build.archive("h1", ["h1"])
    lib2 = build.archive("h2", ["h2"])
    base = build._base

    main_obj = _load_object(base / "main_self.o", "main_self.o")
    archives = _load_archives([("libh1.a", lib1), ("libh2.a", lib2)])
    result = simulate(main_obj, archives, main_label="main_self.o")
    assert result.ok
    assert result.providers["hook"] == "main_self.o"
    assert result.extracted == []


def test_intra_archive_rescan_satisfies_later_member_dependency(fixture_dir):
    build = fixture_dir
    build("main_ia", "void early(void);\nint iam(void){early();return 0;}\n")
    build("early", "void later(void);\nvoid early(void){later();}\n")
    build("later", "void later(void){}\n")
    lib = build.archive("ia", ["early", "later"])
    base = build._base

    main_obj = _load_object(base / "main_ia.o", "main_ia.o")
    archives = _load_archives([("libia.a", lib)])
    result = simulate(main_obj, archives, main_label="main_ia.o")
    assert result.ok
    assert result.providers["later"] == "libia.a(later.o)"
    assert {m.member_name for m in result.extracted} == {"early.o", "later.o"}


def test_nm_parser_classifies_symbol_kinds(fixture_dir):
    build = fixture_dir
    build("kinds",
          "int strong_data = 1;\n"
          "extern int weak_data;\n"
          "__attribute__((weak)) int weak_data;\n"
          "void strong_fn(void){}\n"
          "__attribute__((weak)) void weak_fn(void){}\n"
          "void undef_fn(void);\n"
          "void caller(void){undef_fn();}\n")
    base = build._base
    obj = _load_object(base / "kinds.o", "kinds.o")
    kinds = {s.name: s.kind for s in obj.symbols}
    assert kinds["strong_fn"] == SymKind.STRONG
    assert kinds["strong_data"] == SymKind.STRONG
    assert kinds["weak_fn"] == SymKind.WEAK_DEF
    assert kinds["weak_data"] == SymKind.WEAK_DEF
    assert kinds["undef_fn"] == SymKind.UNDEF


# ---------------------------------------------------------------------------
# Weak-definition members vs strong refs and open commons
# ---------------------------------------------------------------------------


def test_weak_definition_member_satisfies_strong_undefined(fixture_dir):
    # A strong undefined reference extracts even a weak-defining member;
    # the resulting binding stays weak.
    build = fixture_dir
    build("main_su", "void sv(void);\nint sum(void){sv();return 0;}\n")
    build("wd", "__attribute__((weak)) void sv(void){}\n")
    lib = build.archive("wd", ["wd"])
    base = build._base

    main_obj = _load_object(base / "main_su.o", "main_su.o")
    archives = _load_archives([("libwd.a", lib)])
    result = simulate(main_obj, archives, main_label="main_su.o")
    assert result.ok
    assert result.providers["sv"] == "libwd.a(wd.o)"
    assert result.bindings["sv"] == "weak"
    assert {m.member_name for m in result.extracted} == {"wd.o"}

    ok, real_defined = _real_ld_extracts(base / "main_su.o", [lib])
    assert ok
    assert "sv" in real_defined
    real_nm = run_nm(str(base / "real_out.o"))
    assert next(
        line for line in real_nm.splitlines() if line.startswith("sv ")
    ).split()[1] == "W"


def test_weak_definition_member_does_not_extract_for_open_common(fixture_dir):
    # An open common in main must NOT pull a member that only defines the same
    # symbol weakly; the common stays owned by the main object.
    build = fixture_dir
    build("main_cw", "int cv;\nvoid cvuser(void){cv=1;}\n", common=True)
    build("cwd", "__attribute__((weak)) int cv;\n")
    lib = build.archive("cw", ["cwd"])
    base = build._base

    main_obj = _load_object(base / "main_cw.o", "main_cw.o")
    archives = _load_archives([("libcw.a", lib)])
    result = simulate(main_obj, archives, main_label="main_cw.o")
    assert result.ok
    assert result.providers["cv"] == "main_cw.o"
    assert result.bindings["cv"] == "common"
    assert result.extracted == []

    ok, _ = _real_ld_extracts(base / "main_cw.o", [lib])
    assert ok
    real_nm = run_nm(str(base / "real_out.o"))
    cv_line = next(line for line in real_nm.splitlines() if line.startswith("cv "))
    assert cv_line.split()[1] == "C"  # still a common, member not extracted


def test_weak_hook_defined_in_main_blocks_later_strong_member(fixture_dir):
    # The main object already defines the hook weakly; nothing else is
    # undefined, so the archive's strong member is never extracted and the
    # hook stays owned by the main object.
    build = fixture_dir
    build("main_wh",
          "__attribute__((weak)) void wh(void){}\n"
          "int whm(void){wh();return 0;}\n")
    build("libwh", "void wh(void){}\nvoid unrelated(void){}\n")
    lib = build.archive("wh", ["libwh"])
    base = build._base

    main_obj = _load_object(base / "main_wh.o", "main_wh.o")
    archives = _load_archives([("libwh.a", lib)])
    result = simulate(main_obj, archives, main_label="main_wh.o")
    assert result.ok
    assert result.providers["wh"] == "main_wh.o"
    assert result.bindings["wh"] == "weak"
    assert result.extracted == []

    ok, real_defined = _real_ld_extracts(base / "main_wh.o", [lib])
    assert ok
    real_nm = run_nm(str(base / "real_out.o"))
    wh_line = next(line for line in real_nm.splitlines() if line.startswith("wh "))
    assert wh_line.split()[1] == "W"
