"""Shared path normalization and matching helpers for extractors and verifiers."""

from __future__ import annotations

import contextlib
import functools
import posixpath
from collections.abc import Sequence
from pathlib import Path

KNOWN_EXTENSIONLESS_FILES: frozenset[str] = frozenset(
    {
        "Makefile",
        "GNUmakefile",
        "Dockerfile",
        "Containerfile",
        "Procfile",
        "Gemfile",
        "Rakefile",
        "Justfile",
        "Vagrantfile",
        "Jenkinsfile",
        "Brewfile",
        "LICENSE",
        "README",
        "CHANGELOG",
        "AUTHORS",
        "CODEOWNERS",
        "BUILD",
        "WORKSPACE",
        "Tiltfile",
        "Earthfile",
        ".gitignore",
        ".dockerignore",
        ".editorconfig",
        ".npmrc",
        ".nvmrc",
        ".eslintrc",
        ".prettierrc",
        ".babelrc",
        ".env",
        ".python-version",
        ".tool-versions",
    }
)


def _to_posix(raw: str) -> str:
    """Convert backslashes to forward slashes and strip surrounding whitespace/quotes."""
    s = raw.strip().strip("`\"'")
    return s.replace("\\", "/")


def _norm_base(base: str | Path) -> str:
    s = _to_posix(str(base))
    if not s:
        return ""
    norm = posixpath.normpath(s)
    if norm == "/":
        return "/"
    return norm.rstrip("/")


def build_base_dirs(cwd: str | None, repo_root: Path | None) -> tuple[str, ...]:
    """Build ordered, deduplicated normalized base directory prefixes (longest first)."""
    candidates: list[str] = []
    if repo_root is not None:
        candidates.append(str(repo_root))
        with contextlib.suppress(OSError):
            candidates.append(str(repo_root.resolve()))
    if cwd:
        candidates.append(cwd)
        with contextlib.suppress(OSError):
            cwd_path = Path(cwd)
            if cwd_path.exists():
                candidates.append(str(cwd_path.resolve()))

    seen: set[str] = set()
    normalized: list[str] = []
    for item in candidates:
        nb = _norm_base(item)
        if nb and nb != "." and nb not in seen:
            seen.add(nb)
            normalized.append(nb)

    # Longest prefixes first so nested paths strip the most specific base directory
    normalized.sort(key=len, reverse=True)
    return tuple(normalized)


@functools.lru_cache(maxsize=8192)
def _normalize_path_cached(raw: str, base_dirs: tuple[str, ...]) -> str:
    s = _to_posix(raw)
    if not s:
        return ""

    is_abs = s.startswith("/")
    norm = posixpath.normpath(s)
    if is_abs and not norm.startswith("/"):
        norm = "/" + norm

    if norm.startswith("/"):
        for base in base_dirs:
            if not base or not base.startswith("/"):
                continue
            if norm == base:
                return "."
            prefix = base + "/" if base != "/" else "/"
            if norm.startswith(prefix):
                rel = norm[len(prefix) :]
                return rel or "."

    while norm.startswith("./"):
        norm = norm[2:]

    return norm or "."


def normalize_path(raw: str, *, base_dirs: Sequence[str] = ()) -> str:
    """Normalize a file or directory path to a clean repo-relative POSIX form when possible."""
    bd = base_dirs if isinstance(base_dirs, tuple) else tuple(base_dirs)
    return _normalize_path_cached(raw, bd)


def is_exact_path_match(claimed: str, observed: str, *, base_dirs: Sequence[str] = ()) -> bool:
    """Return True if claimed and observed match exactly (or abs-vs-rel suffix)."""
    norm_c = normalize_path(claimed, base_dirs=base_dirs)
    norm_o = normalize_path(observed, base_dirs=base_dirs)
    if not norm_c or not norm_o:
        return False
    if norm_c == norm_o:
        return True
    c_abs = norm_c.startswith("/")
    o_abs = norm_o.startswith("/")
    if o_abs and not c_abs and norm_o.endswith("/" + norm_c):
        return True
    return c_abs and not o_abs and norm_c.endswith("/" + norm_o)


def is_suffix_path_match(claimed: str, observed: str, *, base_dirs: Sequence[str] = ()) -> bool:
    """Return True if two multi-segment paths match via a segment-boundary suffix in either dir.

    Covers monorepo sub-paths (`apps/api/src/auth.ts` vs `src/auth.ts`) and subdirectory cwd
    paths (`src/a.py` with cwd `/repo/packages/web` vs `packages/web/src/a.py`).
    """
    norm_c = normalize_path(claimed, base_dirs=base_dirs)
    norm_o = normalize_path(observed, base_dirs=base_dirs)
    if not norm_c or not norm_o:
        return False
    if norm_c == norm_o:
        return True
    if "/" not in norm_c or "/" not in norm_o:
        return False
    return norm_o.endswith("/" + norm_c) or norm_c.endswith("/" + norm_o)


def paths_match(claimed: str, observed: str, *, base_dirs: Sequence[str] = ()) -> bool:
    """Return True if a claimed file path refers to the same file as an observed path."""
    if is_exact_path_match(claimed, observed, base_dirs=base_dirs):
        return True
    if is_suffix_path_match(claimed, observed, base_dirs=base_dirs):
        return True
    norm_c = normalize_path(claimed, base_dirs=base_dirs)
    norm_o = normalize_path(observed, base_dirs=base_dirs)
    if not norm_c or not norm_o:
        return False
    return "/" not in norm_c and posixpath.basename(norm_o) == norm_c


def is_repo_root_path(path: str | None, *, base_dirs: Sequence[str] = ()) -> bool:
    """Return True if a search root path covers the entire repository root."""
    if path is None:
        return True
    s = _to_posix(path)
    if not s or s in (".", "./", "*"):
        return True
    norm = normalize_path(s, base_dirs=base_dirs)
    if norm in ("", ".", "./"):
        return True
    if norm.startswith("/"):
        for base in base_dirs:
            if base.startswith("/") and (base == norm or base.startswith(norm.rstrip("/") + "/")):
                return True
    return False


def path_is_within(child: str, parent: str, *, base_dirs: Sequence[str] = ()) -> bool:
    """Return True if normalized `child` is equal to or nested inside normalized `parent`."""
    if is_repo_root_path(parent, base_dirs=base_dirs):
        return True
    norm_child = normalize_path(child, base_dirs=base_dirs)
    norm_parent = normalize_path(parent, base_dirs=base_dirs)
    if not norm_child or not norm_parent:
        return False
    if norm_child == norm_parent:
        return True
    parent_prefix = norm_parent.rstrip("/") + "/"
    if norm_child.startswith(parent_prefix):
        return True
    if (
        norm_parent.startswith("/")
        and not norm_child.startswith("/")
        and norm_parent.endswith("/" + norm_child)
    ):
        return True
    return (
        norm_child.startswith("/")
        and not norm_parent.startswith("/")
        and ("/" + norm_parent + "/") in (norm_child + "/")
    )


def resolve_in_repo(
    claimed: str,
    repo_root: Path,
    *,
    base_dirs: Sequence[str] = (),
    fallback_observed: Sequence[str] = (),
) -> Path | None:
    """Resolve a claimed path inside `repo_root`, returning None if outside `repo_root`."""
    norm_c = normalize_path(claimed, base_dirs=base_dirs)
    if not norm_c:
        return None

    try:
        resolved_root = repo_root.resolve()
    except OSError:
        resolved_root = repo_root

    def _confined(candidate: Path) -> Path | None:
        try:
            res = candidate.resolve()
            if res.is_relative_to(resolved_root):
                return res
        except OSError:
            return None
        return None

    if norm_c.startswith("/"):
        return _confined(Path(norm_c))

    direct = _confined(repo_root / norm_c)
    if direct is not None and direct.exists():
        return direct

    # Try resolving via observed transcript paths (handles basename & monorepo suffix matches)
    for obs in fallback_observed:
        norm_o = normalize_path(obs, base_dirs=base_dirs)
        if not norm_o:
            continue
        if is_suffix_path_match(norm_c, norm_o) or (
            "/" not in norm_c and posixpath.basename(norm_o) == norm_c
        ):
            cand = Path(norm_o) if norm_o.startswith("/") else repo_root / norm_o
            confined = _confined(cand)
            if confined is not None and confined.exists():
                return confined

    return direct
