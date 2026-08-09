#!/usr/bin/env python3
"""
Download arXiv e-print, extract, pick main .tex, fetch paper title (PDF basename).
Usage: python download.py <paper_id> <work_dir>

stdout: three lines — WORK_DIR='…' MAIN_TEX='…' PDF_NAME='…'
"""
import gzip
import html
import os
import re
import shutil
import sys
import tarfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def extract_tar_archive(tf, work_dir):
    """Use the safer tar extraction mode when the runtime supports it."""
    if sys.version_info >= (3, 12):
        tf.extractall(work_dir, filter="data")
    else:
        tf.extractall(work_dir)


def download_and_extract(paper_id, work_dir):
    os.makedirs(work_dir, exist_ok=True)
    source_path = os.path.join(work_dir, "source.bin")

    url = f"https://arxiv.org/e-print/{paper_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(source_path, "wb") as f:
                shutil.copyfileobj(resp, f)
    except Exception as e:
        print(f"Error: download failed {url}\n{e}", file=sys.stderr)
        sys.exit(1)

    extracted = False
    try:
        if tarfile.is_tarfile(source_path):
            with tarfile.open(source_path) as tf:
                extract_tar_archive(tf, work_dir)
            extracted = True
    except Exception:
        pass
    if not extracted:
        try:
            with gzip.open(source_path, "rb") as gz, open(os.path.join(work_dir, "paper.tex"), "wb") as out:
                shutil.copyfileobj(gz, out)
            extracted = True
        except Exception:
            pass
    if not extracted:
        shutil.copy(source_path, os.path.join(work_dir, "paper.tex"))
    os.remove(source_path)

    tex_files = []
    for root, _, files in os.walk(work_dir):
        for f in files:
            if f.endswith(".tex"):
                tex_files.append(os.path.relpath(os.path.join(root, f), work_dir))
    if not tex_files:
        print("Error: no .tex files found; this paper may be PDF-only.", file=sys.stderr)
        sys.exit(1)
    return tex_files


_RE_DOCCLASS = re.compile(r"\\documentclass")
_RE_INPUT = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
_RE_CITATION_TITLE = re.compile(
    r'<meta\s+name=["\']citation_title["\']\s+content=["\'](.*?)["\']',
    re.IGNORECASE,
)
_RE_HTML_TITLE = re.compile(r"<title>\s*(?:\[[^\]]+\]\s*)?(.*?)\s*</title>", re.IGNORECASE | re.DOTALL)


def find_main_tex(work_dir, tex_files):
    candidates = []
    for tf in tex_files:
        try:
            content = open(os.path.join(work_dir, tf), "r", encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        if _RE_DOCCLASS.search(content):
            candidates.append((tf, content))
    if not candidates:
        print("Error: no .tex file containing \\documentclass found.", file=sys.stderr)
        sys.exit(1)
    if len(candidates) == 1:
        return candidates[0]
    return max(candidates, key=lambda c: len(_RE_INPUT.findall(c[1])))


def fetch_arxiv_title(paper_id):
    """Prefer arXiv API for title; fall back to abstract page metadata."""
    title = fetch_arxiv_title_from_api(paper_id)
    if title:
        return title
    return fetch_arxiv_title_from_abs_page(paper_id)


def fetch_arxiv_title_from_api(paper_id):
    query = urllib.parse.urlencode({"id_list": paper_id})
    url = f"https://arxiv.org/api/query?{query}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "arxiv-translator/1.0 (+https://arxiv.org/help/api/user-manual)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
    except Exception:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    entry = root.find("atom:entry", _ATOM_NS)
    if entry is None:
        return None
    title_el = entry.find("atom:title", _ATOM_NS)
    if title_el is None:
        return None
    title = " ".join(title_el.itertext()).strip()
    return re.sub(r"\s+", " ", title) or None


def fetch_arxiv_title_from_abs_page(paper_id):
    url = f"https://arxiv.org/abs/{paper_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            page = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    for pattern in (_RE_CITATION_TITLE, _RE_HTML_TITLE):
        match = pattern.search(page)
        if not match:
            continue
        title = html.unescape(match.group(1)).strip()
        title = re.sub(r"\s+", " ", title)
        if title:
            return title
    return None


_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _portable_basename(value, max_bytes):
    """Return a readable basename accepted by common filesystems and transfer services."""
    s = " ".join(str(value or "").split())
    s = re.sub(r'[<>:"/\\\\|?*]+', " - ", s)
    s = re.sub(r"[\x00-\x1f\x7f]+", "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        return ""
    if s.casefold() in _WINDOWS_RESERVED_NAMES:
        s = f"_{s}"
    if max_bytes:
        s = s.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")
    return s


def pdf_name_from_title(title, fallback, max_bytes=200):
    """Use a readable, portable, length-bounded paper title as the PDF basename."""
    safe_title = _portable_basename(title, max_bytes)
    if safe_title:
        return safe_title
    safe_fallback = _portable_basename(fallback, max_bytes)
    return safe_fallback or "arxiv-paper"


def _sh_var_assign(name, value):
    esc = str(value).replace("'", "'\\''")
    return f"{name}='{esc}'"


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python download.py <paper_id> <work_dir>", file=sys.stderr)
        sys.exit(2)
    paper_id, work_dir = sys.argv[1], sys.argv[2]
    tex_files = download_and_extract(paper_id, work_dir)
    rel_path, _ = find_main_tex(work_dir, tex_files)
    rel_path = rel_path.replace("\\", "/")
    fallback = os.path.splitext(os.path.basename(rel_path))[0]
    pdf_name = pdf_name_from_title(fetch_arxiv_title(paper_id), fallback)

    print(_sh_var_assign("WORK_DIR", os.path.abspath(work_dir)))
    print(_sh_var_assign("MAIN_TEX", rel_path))
    print(_sh_var_assign("PDF_NAME", pdf_name))
