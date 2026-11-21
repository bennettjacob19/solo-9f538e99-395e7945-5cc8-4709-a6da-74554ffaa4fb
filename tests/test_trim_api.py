"""End-to-end HTTP tests for the ``POST /trim`` firmware trimming API."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest


def _gcc(base: Path, name: str, src: str, *, lto: bool = False) -> Path:
    (base / f"{name}.c").write_text(src)
    obj = base / f"{name}.o"
    cmd = ["gcc", "-fno-pic", "-fno-pie", "-c", str(base / f"{name}.c"), "-o", str(obj)]
    if lto:
        cmd.insert(2, "-flto")
    subprocess.run(cmd, check=True, capture_output=True)
    return obj


def _ar(base: Path, lib: str, members: list[str]) -> Path:
    path = base / f"lib{lib}.a"
    subprocess.run(
        ["ar", "rcs", str(path), *[str(base / f"{m}.o") for m in members]],
        check=True,
    )
    return path


def _post(client, main_obj: Path, libs: list[Path], hooks: str,
          main_name: str | None = None, lib_names: list[str] | None = None):
    files = [
        ("main_object", (
            main_name or main_obj.name,
            io.BytesIO(main_obj.read_bytes()),
            "application/x-object",
        ))
    ]
    names = lib_names or [p.name for p in libs]
    for path, name in zip(libs, names):
        files.append(("libraries", (name, io.BytesIO(path.read_bytes()), "application/x-archive")))
    return client.post("/trim", data={"hooks": hooks}, files=files)


@pytest.fixture()
def artifacts(tmp_path: Path):
    base = tmp_path / "artifacts"
    base.mkdir()
    # Main object: references hook (strong) and pull_b.
    main_obj = _gcc(base, "main",
                    "void hook(void);\nvoid pull_b(void);\n"
                    "int entry(void){hook();pull_b();return 0;}\n")
    # The .o paths below are consumed by _ar through their file names.
    _gcc(base, "weak_hook",
         "__attribute__((weak)) void hook(void){}\n"
         "void wa(void){}\n")
    _gcc(base, "strong_hook",
         "void hook(void){}\nvoid pull_b(void){}\n")
    _gcc(base, "dead", "void dead_fn(void){}\n")
    lib_weak = _ar(base, "weak", ["weak_hook"])
    lib_strong = _ar(base, "strong", ["strong_hook"])
    lib_dead = _ar(base, "dead", ["dead"])
    return dict(
        base=base, main=main_obj,
        weak=lib_weak, strong=lib_strong, dead=lib_dead,
    )


# ---------------------------------------------------------------------------
# Happy path / docs
# ---------------------------------------------------------------------------


def test_trim_appears_in_openapi_and_docs(client):
    spec = client.get("/openapi.json").json()
    assert "/trim" in spec["paths"]
    assert "post" in spec["paths"]["/trim"]
    docs = client.get("/docs")
    assert docs.status_code == 200


def test_trim_success_returns_shortest_list_and_hook_sources(client, artifacts):
    r = _post(
        client,
        artifacts["main"],
        [artifacts["weak"], artifacts["strong"], artifacts["dead"]],
        "hook",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["original_library_order"] == [
        "libweak.a", "libstrong.a", "libdead.a",
    ]
    # Original hook provider is the strong member (rebind); the shortest
    # preserving list is libstrong alone (index 1).
    assert body["keep_library_indices"] == [1]
    assert body["keep_libraries"] == ["libstrong.a"]
    assert body["removed_libraries"] == ["libweak.a", "libdead.a"]
    hook = body["hook_sources"][0]
    assert hook["name"] == "hook"
    assert hook["source"] == "library"
    assert hook["provider"] == "libstrong.a(strong_hook.o)"
    assert hook["binding"] == "strong"
    assert hook["library_index"] == 1
    assert hook["member_name"] == "strong_hook.o"
    assert "libstrong.a(strong_hook.o)" in body["extracted_members"]


def test_trim_hook_from_main_object(client, artifacts):
    main_hook = _gcc(
        artifacts["base"], "mhook",
        "void board_hook(void){}\n"
        "int entry2(void){board_hook();return 0;}\n",
    )
    r = _post(client, main_hook, [artifacts["dead"]], "board_hook")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["keep_library_indices"] == []
    assert body["removed_libraries"] == ["libdead.a"]
    assert body["hook_sources"][0]["source"] == "main"
    assert body["hook_sources"][0]["provider"].endswith("mhook.o")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_reject_more_than_six_libraries(client, artifacts):
    libs = [artifacts["dead"]] * 7
    # Distinct file names are required, so rename via the lib_names argument.
    names = [f"lib{i}.a" for i in range(7)]
    r = _post(client, artifacts["main"], libs, "hook", lib_names=names)
    assert r.status_code == 422
    assert "1 and 6" in r.text


def test_reject_lto_object(client, artifacts):
    lto_obj = _gcc(artifacts["base"], "lto", "int f(void){return 1;}\n", lto=True)
    r = _post(client, lto_obj, [artifacts["dead"]], "f")
    assert r.status_code == 422
    assert "LTO" in r.json()["detail"]


def test_reject_non_elf_main_object(client, artifacts):
    fake = artifacts["base"] / "fake.o"
    fake.write_bytes(b"not an ELF file at all")
    r = _post(client, fake, [artifacts["dead"]], "hook")
    assert r.status_code == 422
    assert "ELF" in r.text


def test_reject_non_archive_library(client, artifacts):
    fake_lib = artifacts["base"] / "libfake.a"
    fake_lib.write_bytes(b"this is not an ar archive")
    r = _post(client, artifacts["main"], [fake_lib], "hook")
    assert r.status_code == 422


def test_reject_duplicate_member_names_within_archive(client, artifacts, tmp_path):
    base = artifacts["base"]
    # Two different objects both named dup.o inside one archive.
    d1 = base / "dupdir1"
    d2 = base / "dupdir2"
    d1.mkdir(exist_ok=True)
    d2.mkdir(exist_ok=True)
    (d1 / "dup.c").write_text("void dup_a(void){}\n")
    (d2 / "dup.c").write_text("void dup_b(void){}\n")
    subprocess.run(["gcc", "-fno-pic", "-fno-pie", "-c", str(d1 / "dup.c"),
                    "-o", str(d1 / "dup.o")], check=True)
    subprocess.run(["gcc", "-fno-pic", "-fno-pie", "-c", str(d2 / "dup.c"),
                    "-o", str(d2 / "dup.o")], check=True)
    lib = base / "libdup.a"
    subprocess.run(
        ["ar", "rcs", str(lib), str(d1 / "dup.o"), str(d2 / "dup.o")],
        check=True,
    )
    r = _post(client, artifacts["main"], [lib], "hook")
    assert r.status_code == 422
    assert "duplicate member" in r.text


def test_reject_missing_hooks_form(client, artifacts):
    files = [
        ("main_object", (
            artifacts["main"].name,
            io.BytesIO(artifacts["main"].read_bytes()),
            "application/x-object",
        )),
        ("libraries", (
            artifacts["dead"].name,
            io.BytesIO(artifacts["dead"].read_bytes()),
            "application/x-archive",
        )),
    ]
    r = client.post("/trim", files=files)
    assert r.status_code == 422


def test_reject_invalid_hook_name(client, artifacts):
    r = _post(client, artifacts["main"], [artifacts["dead"]], "bad-name!")
    assert r.status_code == 422
    assert "Invalid hook name" in r.text


def test_duplicate_library_file_name_rejected(client, artifacts):
    r = _post(
        client, artifacts["main"],
        [artifacts["dead"], artifacts["dead"]],
        "hook",
        lib_names=["same.a", "same.a"],
    )
    assert r.status_code == 422
    assert "duplicate library" in r.text


# ---------------------------------------------------------------------------
# Link diagnostics
# ---------------------------------------------------------------------------


def test_unresolved_hook_reports_reason(client, artifacts):
    # Main references a symbol no archive provides.
    broken = _gcc(
        artifacts["base"], "broken",
        "void ghost_symbol(void);\n"
        "int broken_entry(void){ghost_symbol();return 0;}\n",
    )
    r = _post(client, broken, [artifacts["dead"]], "ghost_symbol")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error_kind"] == "link_failed"
    assert "ghost_symbol" in detail["message"]


def test_hook_defined_nowhere_reports_hook_unresolved(client, artifacts):
    standalone = _gcc(artifacts["base"], "standalone", "int s(void){return 0;}\n")
    r = _post(client, standalone, [artifacts["dead"]], "never_defined")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error_kind"] == "hook_unresolved"
    assert "never_defined" in detail["message"]


def test_multiple_hooks_mixed_sources(client, artifacts):
    main_mix = _gcc(
        artifacts["base"], "mix",
        "void main_hook(void){}\n"
        "void lib_hook(void);\n"
        "int mix_entry(void){main_hook();lib_hook();return 0;}\n",
    )
    _gcc(artifacts["base"], "libhook_o", "void lib_hook(void){}\n")
    lib = _ar(artifacts["base"], "mixlib", ["libhook_o"])
    r = _post(client, main_mix, [lib], "main_hook,lib_hook")
    assert r.status_code == 200, r.text
    sources = {h["name"]: h for h in r.json()["hook_sources"]}
    assert sources["main_hook"]["source"] == "main"
    assert sources["lib_hook"]["source"] == "library"
    assert sources["lib_hook"]["member_name"] == "libhook_o.o"


def test_health_reports_binutils(client):
    body = client.get("/health").json()
    assert "nm" in body and "readelf" in body and "gcc" in body
