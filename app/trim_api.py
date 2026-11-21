"""``POST /trim`` -- firmware static-library trimming API.

Upload contract (multipart/form-data):

* ``main_object``: the single main ``.o`` produced by ``/compile`` (GCC,
  non-LTO, relocatable ELF).
* ``libraries``: 1..6 ordinary ``.a`` static archives, uploaded in the exact
  order of the original link command.
* ``hooks``: comma/newline separated hook function names whose
  implementations must keep coming from their original provider.

The endpoint never runs a real link: it parses the artifacts with
``nm``/``readelf`` and evaluates GNU ld static-link semantics in pure Python
(see :mod:`app.linker`).  It returns the shortest ordered library list that
still links and still provides every hook from its original object.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from . import config
from .linker import (
    ToolError,
    build_archives,
    parse_archive_nm,
    parse_object_nm,
    read_elf_header,
    read_elf_sections,
    run_nm,
)
from .trimmer import HookResolution, TrimPlan, solve

router = APIRouter(tags=["trimming"])

MAX_LIBRARIES = 6


# ---------------------------------------------------------------------------
# Response models (drive the OpenAPI schema shown on /docs)
# ---------------------------------------------------------------------------


class HookSource(BaseModel):
    name: str
    source: str               # "main" | "library"
    provider: str             # "main.o" or "libx.a(member.o)"
    binding: str              # "strong" | "weak" | "common"
    library_index: int | None = None
    library_name: str | None = None
    member_name: str | None = None


class TrimResponse(BaseModel):
    status: str               # "ok"
    original_library_order: list[str]
    keep_library_indices: list[int]
    keep_libraries: list[str]
    removed_libraries: list[str]
    hook_sources: list[HookSource]
    extracted_members: list[str]
    weak_unresolved: list[str]
    candidates_checked: int
    main_object: str


class TrimFailure(BaseModel):
    status: str               # "error"
    error_kind: str           # "link_failed" | "hook_unresolved" | "invalid_input"
    detail: str


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _read_upload(upload: UploadFile) -> bytes:
    data = upload.file.read()
    if not isinstance(data, bytes):  # pragma: no cover - defensive
        data = data.encode("utf-8")
    return data


def _basename(filename: str | None) -> str:
    name = (filename or "").replace("\\", "/").split("/")[-1]
    return name


def _is_safe_artifact_name(name: str) -> bool:
    """Whitelist for uploaded artifact file names (no paths, no traversal)."""
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


def _parse_hooks(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        raise HTTPException(
            status_code=422,
            detail="hooks is required: list at least one hook function name.",
        )
    hooks: list[str] = []
    seen: set[str] = set()
    for token in raw.replace("\n", ",").split(","):
        name = token.strip()
        if not name:
            continue
        if not _is_valid_c_name(name):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid hook name {name!r}: expected a C identifier "
                    "(optionally versioned with '@...')."
                ),
            )
        if name not in seen:
            seen.add(name)
            hooks.append(name)
    if not hooks:
        raise HTTPException(status_code=422, detail="hooks is empty.")
    return hooks


def _is_valid_c_name(name: str) -> bool:
    head, _, tail = name.partition("@")
    if not head or not head.replace("_", "x").isalnum() or head[0].isdigit():
        return False
    if tail and any(ch.isspace() for ch in tail):
        return False
    return True


def _assert_relocatable_non_lto(path: Path, label: str) -> None:
    """Reject non-ELF inputs, non-relocatable ELF and LTO bytecode."""
    try:
        header = read_elf_header(str(path), readelf_bin=config.READELF_BIN)
    except ToolError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{label} is not a readable ELF relocatable object: {exc}",
        ) from exc
    if "REL (Relocatable file)" not in header:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{label} must be a relocatable ELF object (ET_REL, e.g. GCC -c "
                f"output); readelf reports: "
                f"{[line.strip() for line in header.splitlines() if 'Type' in line]}"
            ),
        )
    try:
        sections = read_elf_sections(str(path), readelf_bin=config.READELF_BIN)
    except ToolError as exc:  # pragma: no cover - header succeeded earlier
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if ".gnu.lto_" in sections:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{label} contains .gnu.lto_* sections: LTO bytecode is not "
                "supported. Recompile without -flto and upload the GCC "
                "non-LTO ELF object."
            ),
        )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post(
    "/trim",
    response_model=TrimResponse,
    summary="Trim static libraries while preserving board-hook implementations",
    responses={422: {"model": TrimFailure}},
)
async def trim_endpoint(
    main_object: UploadFile = File(
        ...,
        description=(
            "Main relocatable .o from /compile (container GCC, non-LTO). "
            "Always included in the link, exactly like the original command."
        ),
    ),
    libraries: list[UploadFile] = File(
        ...,
        description=(
            "1..6 ordinary .a static archives, uploaded in the original link "
            "order. Member file names must be unique within each archive."
        ),
    ),
    hooks: str = Form(
        ...,
        description="Comma/newline separated hook function names to preserve.",
    ),
) -> Any:
    hook_list = _parse_hooks(hooks)
    lib_uploads = list(libraries)
    if not 1 <= len(lib_uploads) <= MAX_LIBRARIES:
        raise HTTPException(
            status_code=422,
            detail=f"libraries must contain between 1 and {MAX_LIBRARIES} archives.",
        )

    main_name = _basename(main_object.filename) or "main.o"
    if not _is_safe_artifact_name(main_name):
        raise HTTPException(
            status_code=422, detail="main_object file name must not contain a path."
        )
    if not main_name.lower().endswith(".o"):
        raise HTTPException(
            status_code=422, detail="main_object must be a .o file."
        )
    lib_names: list[str] = []
    for upload in lib_uploads:
        name = _basename(upload.filename)
        if not name or not _is_safe_artifact_name(name):
            raise HTTPException(
                status_code=422,
                detail="every library needs a simple file name without a path.",
            )
        if not name.lower().endswith(".a"):
            raise HTTPException(
                status_code=422,
                detail=f"library {name!r} must be a .a static archive.",
            )
        if name in lib_names:
            raise HTTPException(
                status_code=422, detail=f"duplicate library file name {name!r}."
            )
        lib_names.append(name)

    with tempfile.TemporaryDirectory(prefix="trim-req-") as tmp:
        tmpdir = Path(tmp)
        main_path = tmpdir / main_name
        main_path.write_bytes(_read_upload(main_object))
        _assert_relocatable_non_lto(main_path, f"main_object {main_name!r}")

        lib_paths: list[Path] = []
        for upload in lib_uploads:
            name = _basename(upload.filename)
            path = tmpdir / f"lib{len(lib_paths)}_{name}"
            path.write_bytes(_read_upload(upload))
            lib_paths.append(path)

        # --- Parse the main object ----------------------------------------
        try:
            main_syms = parse_object_nm(
                run_nm(str(main_path), nm_bin=config.NM_BIN), main_name
            )
        except ToolError as exc:
            raise HTTPException(
                status_code=422, detail=f"cannot read symbols of {main_name}: {exc}"
            ) from exc

        # --- Parse every archive and enforce member-name uniqueness -------
        nm_texts: list[tuple[str, str]] = []
        for path, name in zip(lib_paths, lib_names):
            try:
                text = run_nm(str(path), nm_bin=config.NM_BIN)
            except ToolError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"nm cannot read archive {name}: {exc}",
                ) from exc
            archive = parse_archive_nm(text, len(nm_texts), name)
            if not archive.members:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"archive {name} contains no readable object members "
                        "(thin archive or non-ar file?)."
                    ),
                )
            member_names = [m.member_name for m in archive.members]
            duplicates = sorted({m for m in member_names if member_names.count(m) > 1})
            if duplicates:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"archive {name} has duplicate member names {duplicates}; "
                        "member file names must be unique within a library."
                    ),
                )
            nm_texts.append((name, text))

    # --- Solve outside the temp dir; the simulator only needs parsed data ---
    archives = build_archives(nm_texts)
    plan: TrimPlan = solve(main_syms, archives, hook_list, main_label=main_name)
    if not plan.ok:
        raise HTTPException(
            status_code=422,
            detail={
                "error_kind": plan.error_kind or "link_failed",
                "message": plan.error,
                "hooks": hook_list,
                "original_library_order": lib_names,
            },
        )

    return _response(plan, lib_names, main_name)


def _response(
    plan: TrimPlan, lib_names: list[str], main_name: str
) -> TrimResponse:
    return TrimResponse(
        status="ok",
        original_library_order=lib_names,
        keep_library_indices=plan.keep_indices,
        keep_libraries=plan.keep_libraries,
        removed_libraries=plan.removed_libraries,
        hook_sources=[_hook_source(h) for h in plan.hooks],
        extracted_members=plan.extracted_members,
        weak_unresolved=plan.weak_unresolved,
        candidates_checked=plan.candidates_checked,
        main_object=main_name,
    )


def _hook_source(hook: HookResolution) -> HookSource:
    return HookSource(
        name=hook.name,
        source=hook.source,
        provider=hook.provider,
        binding=hook.binding,
        library_index=hook.library_index,
        library_name=hook.library_name,
        member_name=hook.member_name,
    )
