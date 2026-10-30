#!/usr/bin/env python3
"""Extract auditable score evidence from a PDF without modifying the source.

The script intentionally delegates PDF decoding to Poppler command-line tools.
It has no Python package dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


MANIFEST_NAME = "evidence_manifest.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tool_path(name: str) -> Optional[str]:
    return shutil.which(name) or shutil.which(name + ".exe")


def tool_version(executable: Optional[str], timeout_seconds: int) -> Optional[str]:
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "-v"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (result.stdout + "\n" + result.stderr).strip().splitlines()
    return lines[0].strip() if lines else None


def run_tool(command: Sequence[str], timeout_seconds: int = 120) -> Dict[str, object]:
    try:
        result = subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "external PDF tool exceeded the %d-second timeout: %s"
            % (timeout_seconds, command[0])
        ) from exc
    return {
        "command": list(command),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def parse_pdfinfo(raw: str) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def parse_pdfimages_list(raw: str) -> List[Dict[str, object]]:
    """Parse the stable leading columns while retaining the unparsed row.

    Poppler versions differ slightly after the ``enc`` column, so the manifest
    keeps every original row instead of pretending all trailing fields are
    uniformly structured.
    """
    rows: List[Dict[str, object]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("page") or set(stripped) == {"-"}:
            continue
        fields = stripped.split()
        if len(fields) < 9:
            continue
        try:
            page = int(fields[0])
            number = int(fields[1])
            width = int(fields[3])
            height = int(fields[4])
            components = int(fields[6])
            bits_per_component = int(fields[7])
        except ValueError:
            continue
        rows.append(
            {
                "page": page,
                "number": number,
                "type": fields[2],
                "width": width,
                "height": height,
                "color": fields[5],
                "components": components,
                "bits_per_component": bits_per_component,
                "encoding": fields[8],
                "raw": stripped,
            }
        )
    return rows


def inventory_files(paths: Iterable[Path], base: Path) -> List[Dict[str, object]]:
    result: List[Dict[str, object]] = []
    for path in sorted(paths, key=lambda item: item.name.lower()):
        if not path.is_file():
            continue
        stat = path.stat()
        result.append(
            {
                "path": path.relative_to(base).as_posix(),
                "size_bytes": stat.st_size,
                "sha256": sha256_file(path),
            }
        )
    return result


def ensure_empty_output(path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise ValueError("output path exists and is not a directory: %s" % path)
        if any(path.iterdir()):
            raise ValueError("output directory must be empty: %s" % path)
    else:
        path.mkdir(parents=True)


def extract_embedded(
    pdfimages: str, pdf: Path, directory: Path, timeout_seconds: int
) -> Tuple[Dict[str, object], List[Path]]:
    directory.mkdir()
    prefix = directory / "score-image"
    attempt = run_tool([pdfimages, "-all", str(pdf), str(prefix)], timeout_seconds)
    if int(attempt["returncode"]) != 0:
        # Older Poppler builds may not expose -all. -j preserves JPEG streams
        # and writes other streams to lossless PNM-family files. The directory
        # was created by this call, so remove only its partial files before the
        # fallback; never mix outputs from two extraction attempts.
        for item in directory.iterdir():
            if item.is_file() or item.is_symlink():
                item.unlink()
            else:
                raise RuntimeError(
                    "unexpected directory created by pdfimages fallback: %s" % item
                )
        attempt = run_tool([pdfimages, "-j", str(pdf), str(prefix)], timeout_seconds)
    files = [item for item in directory.iterdir() if item.is_file()]
    if int(attempt["returncode"]) != 0:
        raise RuntimeError("pdfimages extraction failed: %s" % attempt["stderr"])
    return attempt, files


def render_pages(
    pdftoppm: str, pdf: Path, directory: Path, dpi: int, timeout_seconds: int
) -> Tuple[Dict[str, object], List[Path]]:
    directory.mkdir()
    prefix = directory / "page"
    result = run_tool(
        [pdftoppm, "-png", "-r", str(dpi), str(pdf), str(prefix)], timeout_seconds
    )
    if int(result["returncode"]) != 0:
        raise RuntimeError("pdftoppm rendering failed: %s" % result["stderr"])
    return result, [item for item in directory.iterdir() if item.is_file()]


def build_manifest(
    pdf: Path,
    output: Path,
    render_requested: bool,
    dpi: int,
    timeout_seconds: int,
    max_pages: int,
    include_local_paths: bool,
) -> Dict[str, object]:
    pdfinfo = tool_path("pdfinfo")
    pdfimages = tool_path("pdfimages")
    pdftoppm = tool_path("pdftoppm")
    pdftotext = tool_path("pdftotext")
    missing = [name for name, value in (("pdfinfo", pdfinfo), ("pdfimages", pdfimages)) if not value]
    if missing:
        raise RuntimeError("required Poppler tools not found on PATH: %s" % ", ".join(missing))

    source_stat_before = pdf.stat()
    source_hash_before = sha256_file(pdf)

    info_run = run_tool([str(pdfinfo), str(pdf)], timeout_seconds)
    if int(info_run["returncode"]) != 0:
        raise RuntimeError("pdfinfo failed: %s" % info_run["stderr"])

    parsed_info = parse_pdfinfo(str(info_run["stdout"]))
    try:
        page_count = int(parsed_info.get("Pages", "0"))
    except ValueError:
        page_count = 0
    if page_count < 1:
        raise RuntimeError("pdfinfo did not report a positive page count")
    if page_count > max_pages:
        raise RuntimeError(
            "PDF has %d pages, above the safety limit of %d; inspect and split it first"
            % (page_count, max_pages)
        )

    list_run = run_tool([str(pdfimages), "-list", str(pdf)], timeout_seconds)
    if int(list_run["returncode"]) != 0:
        raise RuntimeError("pdfimages -list failed: %s" % list_run["stderr"])

    text_run: Optional[Dict[str, object]] = None
    extracted_text = ""
    if pdftotext:
        text_run = run_tool(
            [str(pdftotext), "-enc", "UTF-8", str(pdf), "-"], timeout_seconds
        )
        if int(text_run["returncode"]) == 0:
            extracted_text = str(text_run["stdout"])

    extract_run, embedded = extract_embedded(
        str(pdfimages), pdf, output / "embedded", timeout_seconds
    )
    should_render = render_requested or not embedded
    render_run: Optional[Dict[str, object]] = None
    rendered: List[Path] = []
    if should_render:
        if not pdftoppm:
            raise RuntimeError("pdftoppm is required to render pages but was not found on PATH")
        render_run, rendered = render_pages(
            str(pdftoppm), pdf, output / "pages", dpi, timeout_seconds
        )

    source_stat_after = pdf.stat()
    source_hash_after = sha256_file(pdf)
    source_unchanged = (
        source_hash_before == source_hash_after
        and source_stat_before.st_size == source_stat_after.st_size
        and source_stat_before.st_mtime_ns == source_stat_after.st_mtime_ns
    )
    if not source_unchanged:
        raise RuntimeError("source PDF changed while evidence was being extracted")

    return {
        "manifest_version": "0.1",
        "source": {
            "path": str(pdf) if include_local_paths else pdf.name,
            "size_bytes": source_stat_before.st_size,
            "mtime_ns": source_stat_before.st_mtime_ns,
            "sha256": source_hash_before,
            "unchanged_after_extraction": True,
        },
        "tools": {
            "pdfinfo": {"path": pdfinfo if include_local_paths else Path(str(pdfinfo)).name,
                        "version": tool_version(pdfinfo, timeout_seconds)},
            "pdfimages": {"path": pdfimages if include_local_paths else Path(str(pdfimages)).name,
                          "version": tool_version(pdfimages, timeout_seconds)},
            "pdftoppm": {"path": (pdftoppm if include_local_paths else Path(str(pdftoppm)).name)
                         if pdftoppm else None,
                         "version": tool_version(pdftoppm, timeout_seconds)},
            "pdftotext": {"path": (pdftotext if include_local_paths else Path(str(pdftotext)).name)
                          if pdftotext else None,
                          "version": tool_version(pdftotext, timeout_seconds)},
        },
        "pdfinfo": parsed_info,
        "text_layer": {
            "tool_available": bool(pdftotext),
            "returncode": int(text_run["returncode"]) if text_run else None,
            "has_non_whitespace_text": bool(extracted_text.strip()),
            "non_whitespace_characters": sum(not char.isspace() for char in extracted_text),
        },
        "pdfimages": {
            "extraction_mode": str(extract_run["command"][1]),
            "returncode": int(extract_run["returncode"]),
            "entries": parse_pdfimages_list(str(list_run["stdout"])),
            "files": inventory_files(embedded, output),
        },
        "rendering": {
            "requested": render_requested,
            "automatic_fallback": not embedded,
            "dpi": dpi if should_render else None,
            "returncode": int(render_run["returncode"]) if render_run else None,
            "files": inventory_files(rendered, output),
        },
        "privacy": {
            "local_paths_included": include_local_paths,
            "note": "PDF metadata may contain author or producer fields; review before sharing.",
        },
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract embedded score images and an evidence manifest from a PDF."
    )
    parser.add_argument("pdf", type=Path, help="source score PDF (read only)")
    parser.add_argument("output_dir", type=Path, help="new or empty evidence directory")
    parser.add_argument(
        "--render-pages",
        action="store_true",
        help="also render every page to PNG even when embedded images exist",
    )
    parser.add_argument("--dpi", type=int, default=300, help="page-rendering DPI (default: 300)")
    parser.add_argument("--timeout-seconds", type=int, default=120,
                        help="per-tool timeout in seconds (default: 120)")
    parser.add_argument("--max-pages", type=int, default=500,
                        help="refuse PDFs above this page count (default: 500)")
    parser.add_argument("--include-local-paths", action="store_true",
                        help="include absolute source and tool paths in the manifest")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    pdf = args.pdf.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    try:
        if not pdf.is_file():
            raise ValueError("source PDF does not exist: %s" % pdf)
        if pdf.suffix.lower() != ".pdf":
            raise ValueError("source must have a .pdf extension: %s" % pdf)
        if args.dpi < 72 or args.dpi > 1200:
            raise ValueError("--dpi must be between 72 and 1200")
        if args.timeout_seconds < 1 or args.timeout_seconds > 3600:
            raise ValueError("--timeout-seconds must be between 1 and 3600")
        if args.max_pages < 1:
            raise ValueError("--max-pages must be at least 1")
        ensure_empty_output(output)
        manifest = build_manifest(
            pdf, output, args.render_pages, args.dpi, args.timeout_seconds,
            args.max_pages, args.include_local_paths,
        )
        manifest_path = output / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "manifest": str(manifest_path),
                "embedded_images": len(manifest["pdfimages"]["files"]),
                "rendered_pages": len(manifest["rendering"]["files"]),
                "source_sha256": manifest["source"]["sha256"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
