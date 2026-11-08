#!/usr/bin/env python3
"""Atermius Windows runtime release tool (stdlib only; run with `python -I`).

A single implementation is shared by CI (windows runner) and root (Linux):

  make-native-bundle  root: derive native-bundle.zip from a verified local release ZIP
  make-source-bundle  root: freeze a source tree into source.tar.gz + source-manifest.json
  extract-native      CI:   verify + extract native-bundle.zip
  extract-source      CI:   verify + extract source.tar.gz strictly against its manifest
  check-lock          CI:   validate pubspec.lock sources (hosted/sdk/path/git)
  reverify-source     CI:   prove the build did not modify any frozen source file
  package             CI:   package only runtime files + write the release manifest
  verify-runtime      both: defensive verification of a runtime ZIP + manifest

Nothing here prints file contents, tokens or environment values; only paths,
sizes and hashes. Every failure exits non-zero with a one-line reason.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
import zipfile

SCHEMA_NATIVE = "atermius-native-bundle/1"
SCHEMA_RUNTIME = "atermius-windows-runtime/1"
SCHEMA_SOURCE = "atermius-source-bundle/1"
NATIVE_MANIFEST_NAME = "NATIVE_BUNDLE_MANIFEST.json"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
FIXED_TAR_MTIME = 315532800  # 1980-01-01T00:00:00Z

MAX_REL_PATH = 180          # keeps <work>\src\<rel> well below MAX_PATH on runners
MAX_FILE_BYTES = 512 << 20
MAX_TOTAL_BYTES = 3 << 30
MAX_FILES = 50000

# Verified read-only from the R8 ZIP a4a9ff42d8...f66b6 (2026-11-08).
R8_ZIP_SHA256 = "a4a9ff42d810418928d3dabc88c099efe1945ed9b22d7ad79fa3aef3069f66b6"
ENGINE_SHA256 = "803770d08258d09ffbfcc4694a97a5ef75da105f85198355192998b9950887fe"
# bundle path -> (path inside release ZIP after '\\'->'/' normalization, bytes, sha256)
NATIVE_FILES = {
    "atermius_rdp.dll": ("atermius_rdp.dll", 8457216, ENGINE_SHA256),
    "libusb-1.0.dll": ("libusb-1.0.dll", 166400,
                       "fb6ccd21499cf28c3e4caa3b830e261e1f35761d40b051b6b3af12e0fe4e26f3"),
    "licenses/desktop/FreeRDP.txt": ("data/licenses/desktop/FreeRDP.txt", 11358,
                                     "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"),
    "licenses/desktop/OpenSSL.txt": ("data/licenses/desktop/OpenSSL.txt", 10175,
                                     "7d5450cb2d142651b8afa315b5f238efc805dad827d91ba367d8516bc9d49e7a"),
    "licenses/desktop/libusb-LGPL-2.1.txt": ("data/licenses/desktop/libusb-LGPL-2.1.txt", 26436,
                                             "5df07007198989c622f5d41de8d703e7bef3d0e79d62e24332ee739a452af62a"),
    "licenses/desktop/libusb-NOTICE.txt": ("data/licenses/desktop/libusb-NOTICE.txt", 269,
                                           "8c19f499fe1b66957b8d537e40bea0391405416ce38e190e94eb2bdcb6a55727"),
    "licenses/desktop/zlib.txt": ("data/licenses/desktop/zlib.txt", 1002,
                                  "e32ff4e00d9d94930537635291da39e7e612703334bf6fde8c7f1686fe8a45a2"),
}

# Runtime packaging policy (Release dir relative paths, '/' separated).
DEV_EXTS = {".lib", ".exp", ".pdb", ".ilk", ".obj", ".iobj", ".ipdb", ".map",
            ".tlog", ".log", ".lastbuildstate", ".idb", ".res", ".recipe"}
ROOT_RUNTIME_EXTS = {".exe", ".dll"}
REQUIRED_RUNTIME = ["aterm_client.exe", "flutter_windows.dll", "atermius_rdp.dll",
                    "data/app.so", "data/icudtl.dat",
                    "data/flutter_assets/AssetManifest.bin"]

# Source freeze policy.
FORBIDDEN_SOURCE_SEGMENTS = {".git", ".dart_tool", ".pub-cache", ".idea", ".gradle"}
FORBIDDEN_SOURCE_NAMES = {"directory.build.rsp", "directory.build.props",
                          "directory.build.targets", ".packages"}
REQUIRED_SOURCE = ["apps/client/pubspec.yaml", "apps/client/pubspec.lock",
                   "apps/client/windows/CMakeLists.txt"]

WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$)(\..*)?$",
                          re.IGNORECASE)
BAD_CHARS = set('<>:"|?*')


class PolicyError(Exception):
    pass


def fail(msg: str) -> "None":
    raise PolicyError(msg)


# --------------------------------------------------------------------------- paths

def norm_rel(name: str, *, allow_backslash: bool = False, is_dir: bool = False) -> str:
    """Validate an archive/manifest relative path and return its '/' form.

    Rejects absolute paths, drive letters, UNC, '..', '.', empty segments,
    control chars, Windows-reserved device names, ADS ':' and names Windows
    would silently alter (trailing dot/space)."""
    if not isinstance(name, str) or not name:
        fail("empty path")
    if "\\" in name:
        if not allow_backslash:
            fail(f"backslash in path: {name!r}")
        if "/" in name:
            fail(f"mixed separators: {name!r}")
        name = name.replace("\\", "/")
    if is_dir and name.endswith("/"):
        name = name[:-1]
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        fail(f"absolute path: {name!r}")
    if len(name) > MAX_REL_PATH:
        fail(f"path longer than {MAX_REL_PATH}: {name[:60]!r}...")
    for ch in name:
        if ord(ch) < 0x20 or ch == "\x7f":
            fail(f"control character in path: {name!r}")
        if ch in BAD_CHARS:
            fail(f"character {ch!r} not allowed on Windows: {name!r}")
    for seg in name.split("/"):
        if seg in ("", ".", ".."):
            fail(f"bad segment in path: {name!r}")
        if seg[-1] in ". ":
            fail(f"segment ends with dot/space: {name!r}")
        if WIN_RESERVED.match(seg):
            fail(f"reserved Windows name: {name!r}")
    return name


class PathSet:
    """Tracks exact, casefold and file/dir-prefix collisions."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.dirs: set[str] = set()

    def add(self, rel: str) -> None:
        key = rel.casefold()
        if key in self.files:
            other = self.files[key]
            fail(f"duplicate path: {rel!r}" if other == rel
                 else f"case-insensitive collision: {rel!r} vs {other!r}")
        if key in self.dirs:
            fail(f"file collides with directory: {rel!r}")
        parts = key.split("/")
        for i in range(1, len(parts)):
            prefix = "/".join(parts[:i])
            if prefix in self.files:
                fail(f"directory collides with file: {rel!r}")
            self.dirs.add(prefix)
        self.files[key] = rel


def safe_join(root: str, rel: str) -> str:
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, *rel.split("/")))
    if os.path.commonpath([root_real, target]) != root_real or target == root_real:
        fail(f"path escapes destination: {rel!r}")
    return target


# --------------------------------------------------------------------------- hashing / io

def sha256_file(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def expect_hex(value: str, what: str, length: int = 64) -> str:
    # Canonical form only: lowercase, no whitespace (manifests and inputs alike).
    if not isinstance(value, str) or not (HEX64 if length == 64 else HEX40).match(value):
        fail(f"{what} must be {length} lowercase hex characters")
    return value


def check_file_sha(path: str, expected: str, what: str) -> int:
    expected = expect_hex(expected, f"expected {what} sha256")
    actual, n = sha256_file(path)
    if actual != expected:
        fail(f"{what} sha256 mismatch: got {actual}, expected {expected}")
    return n


def write_json(path: str, obj) -> None:
    data = (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    atomic_write(path, data)


def atomic_write(path: str, data: bytes) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_json_strict(path: str):
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]

    def no_dupes(pairs):
        seen = {}
        for k, v in pairs:
            if k in seen:
                fail(f"duplicate JSON key in {os.path.basename(path)}: {k!r}")
            seen[k] = v
        return seen
    return json.loads(raw.decode("utf-8"), object_pairs_hook=no_dupes)


def write_new_file(dest_root: str, rel: str, src, expected_sha: str | None,
                   limit: int) -> tuple[str, int]:
    target = safe_join(dest_root, rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    safe_join(dest_root, rel)  # re-check after mkdir (no symlinked parents)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags, 0o644)
    h = hashlib.sha256()
    n = 0
    with os.fdopen(fd, "wb") as out:
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            n += len(chunk)
            if n > limit:
                fail(f"file exceeds size limit: {rel!r}")
            h.update(chunk)
            out.write(chunk)
    digest = h.hexdigest()
    if expected_sha is not None and digest != expected_sha:
        fail(f"content sha256 mismatch: {rel!r}")
    return digest, n


def ensure_empty_dir(path: str) -> None:
    if os.path.lexists(path):
        if not os.path.isdir(path) or os.path.islink(path) or os.listdir(path):
            fail(f"destination must be absent or an empty directory: {path}")
    os.makedirs(path, exist_ok=True)


# --------------------------------------------------------------------------- zip reading

def zip_entries_strict(zf: zipfile.ZipFile, *, allow_backslash: bool):
    """Yield (normalized_path, ZipInfo) for file entries; validates every entry."""
    seen = PathSet()
    out = []
    names = [i.filename for i in zf.infolist()]
    if len(names) > MAX_FILES:
        fail("too many ZIP entries")
    total = 0
    for info in zf.infolist():
        if info.flag_bits & 0x1:
            fail(f"encrypted ZIP entry: {info.filename!r}")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            fail(f"unsupported compression in entry: {info.filename!r}")
        mode = (info.external_attr >> 16) & 0xFFFF
        # Unix creators may omit the type bits (e.g. Python writestr: 0o600) -> regular.
        if info.create_system == 3 and stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
            fail(f"non-regular ZIP entry (symlink/device): {info.filename!r}")
        is_dir = info.filename.endswith("/") or (allow_backslash and info.filename.endswith("\\"))
        if is_dir:
            name = info.filename[:-1]
            norm_rel(name, allow_backslash=allow_backslash)
            if info.file_size:
                fail(f"directory entry with data: {info.filename!r}")
            continue
        rel = norm_rel(info.filename, allow_backslash=allow_backslash)
        seen.add(rel)
        if info.file_size > MAX_FILE_BYTES:
            fail(f"ZIP entry too large: {rel!r}")
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            fail("ZIP total uncompressed size exceeds limit")
        out.append((rel, info))
    return out


def read_entry_sha(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with zf.open(info) as f:  # zipfile verifies CRC-32 at EOF
        for chunk in iter(lambda: f.read(1 << 20), b""):
            n += len(chunk)
            if n > info.file_size:
                fail(f"ZIP entry longer than declared: {info.filename!r}")
            h.update(chunk)
    if n != info.file_size:
        fail(f"ZIP entry length mismatch: {info.filename!r}")
    return h.hexdigest(), n


def deterministic_zip(out_path: str, files: list[tuple[str, bytes | str]], *, deflate: bool) -> str:
    """files: (archive_path, bytes-or-filesystem-path). Sorted, fixed metadata."""
    tmp = out_path + ".partial"
    if os.path.exists(tmp):
        os.unlink(tmp)
    method = zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED
    with zipfile.ZipFile(tmp, "w", compression=method, allowZip64=True) as zf:
        for arc, payload in sorted(files, key=lambda x: x[0]):
            zi = zipfile.ZipInfo(arc, date_time=FIXED_ZIP_TIME)
            zi.create_system = 0
            zi.external_attr = 0
            zi.compress_type = method
            if isinstance(payload, (bytes, bytearray)):
                zf.writestr(zi, bytes(payload))
            else:
                with open(payload, "rb") as src, zf.open(zi, "w", force_zip64=True) as dst:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        dst.write(chunk)
    os.replace(tmp, out_path)
    return sha256_file(out_path)[0]


# --------------------------------------------------------------------------- native bundle

def cmd_make_native_bundle(a) -> dict:
    check_file_sha(a.from_zip, a.expect_zip_sha or R8_ZIP_SHA256, "source release ZIP")
    if os.path.exists(a.out) and not a.force:
        fail(f"refusing to overwrite {a.out} (use --force)")
    with zipfile.ZipFile(a.from_zip) as zf:
        entries = dict(zip_entries_strict(zf, allow_backslash=True))
        payload = []
        records = []
        for bundle_path, (zip_path, size, sha) in sorted(NATIVE_FILES.items()):
            info = entries.get(zip_path)
            if info is None:
                fail(f"release ZIP lacks {zip_path!r}")
            data = zf.read(info)
            if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
                fail(f"pinned native file mismatch: {zip_path!r}")
            payload.append((bundle_path, data))
            records.append({"path": bundle_path, "bytes": size, "sha256": sha})
        extra_licenses = sorted(p for p in entries if p.startswith("data/licenses/")
                                and p[len("data/"):] not in NATIVE_FILES)
        if extra_licenses:
            fail(f"release ZIP has unpinned license files: {extra_licenses}")
    manifest = {
        "schema": SCHEMA_NATIVE,
        "engine_sha256": ENGINE_SHA256,
        "source_archive_sha256": a.expect_zip_sha or R8_ZIP_SHA256,
        "source_archive": os.path.basename(a.from_zip),
        "files": records,
    }
    mbytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    payload.append((NATIVE_MANIFEST_NAME, mbytes))
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    zsha = deterministic_zip(a.out, payload, deflate=False)
    return {"native_bundle": a.out, "native_bundle_sha256": zsha,
            "native_manifest_sha256": hashlib.sha256(mbytes).hexdigest(),
            "engine_sha256": ENGINE_SHA256, "files": len(records)}


def load_native_manifest(obj) -> dict:
    if not isinstance(obj, dict) or obj.get("schema") != SCHEMA_NATIVE:
        fail("native manifest schema mismatch")
    files = obj.get("files")
    if not isinstance(files, list) or not files:
        fail("native manifest has no files")
    out = {}
    ps = PathSet()
    for rec in files:
        if not isinstance(rec, dict) or set(rec) != {"path", "bytes", "sha256"}:
            fail("native manifest record must have exactly path/bytes/sha256")
        rel = norm_rel(rec["path"])
        ps.add(rel)
        if not (rel in ("atermius_rdp.dll", "libusb-1.0.dll") or
                (rel.startswith("licenses/desktop/") and rel.count("/") == 2 and rel.endswith(".txt"))):
            fail(f"native bundle path outside allowlist: {rel!r}")
        if not isinstance(rec["bytes"], int) or rec["bytes"] < 0:
            fail("native manifest bytes must be a non-negative integer")
        out[rel] = (rec["bytes"], expect_hex(rec["sha256"], f"native sha256 for {rel}"))
    if "atermius_rdp.dll" not in out:
        fail("native manifest lacks atermius_rdp.dll")
    if out["atermius_rdp.dll"][1] != expect_hex(obj.get("engine_sha256", ""), "engine_sha256"):
        fail("native manifest engine_sha256 does not match atermius_rdp.dll")
    if not any(p.startswith("licenses/desktop/") for p in out):
        fail("native manifest lacks licenses/desktop")
    if "libusb-1.0.dll" in out and "licenses/desktop/libusb-LGPL-2.1.txt" not in out:
        fail("libusb-1.0.dll shipped without its LGPL license")
    return out


def cmd_extract_native(a) -> dict:
    zsha = expect_hex(a.expect_sha, "native bundle sha256")
    check_file_sha(a.zip, zsha, "native bundle")
    engine = expect_hex(a.expect_engine_sha, "engine sha256")
    ensure_empty_dir(a.dest)
    with zipfile.ZipFile(a.zip) as zf:
        entries = dict(zip_entries_strict(zf, allow_backslash=False))
        if NATIVE_MANIFEST_NAME not in entries:
            fail("native bundle lacks its manifest")
        mraw = zf.read(entries[NATIVE_MANIFEST_NAME])
        manifest = json.loads(mraw.decode("utf-8"))
        files = load_native_manifest(manifest)
        if files["atermius_rdp.dll"][1] != engine:
            fail("native bundle engine does not match the dispatch engine_sha256")
        if set(entries) != set(files) | {NATIVE_MANIFEST_NAME}:
            extra = sorted(set(entries) - set(files) - {NATIVE_MANIFEST_NAME})
            missing = sorted(set(files) - set(entries))
            fail(f"native bundle entries differ from manifest: extra={extra} missing={missing}")
        for rel, (size, sha) in sorted(files.items()):
            info = entries[rel]
            if info.file_size != size:
                fail(f"native entry size mismatch: {rel!r}")
            with zf.open(info) as src:
                write_new_file(a.dest, rel, src, sha, size)
    if a.manifest_out:
        write_json(a.manifest_out, manifest)
    return {"native_dir": os.path.abspath(a.dest), "native_bundle_sha256": zsha,
            "engine_sha256": engine, "files": len(files),
            "libusb": "libusb-1.0.dll" in files,
            "native_manifest_sha256": hashlib.sha256(mraw).hexdigest()}


# --------------------------------------------------------------------------- source bundle

def source_path_policy(rel: str) -> None:
    segs = rel.split("/")
    low = [s.casefold() for s in segs]
    if any(s in FORBIDDEN_SOURCE_SEGMENTS for s in low[:-1]) or low[-1] in FORBIDDEN_SOURCE_NAMES:
        fail(f"forbidden path in frozen source: {rel!r}")
    if rel.casefold().startswith("apps/client/build/"):
        fail(f"build output inside frozen source: {rel!r}")
    for i in range(len(low) - 1):
        if low[i] == "flutter" and low[i + 1] == "ephemeral":
            fail(f"Flutter ephemeral output inside frozen source: {rel!r}")


def load_source_manifest(path: str) -> dict[str, str]:
    obj = load_json_strict(path)
    if not isinstance(obj, dict) or not obj:
        fail("source manifest must be a non-empty JSON object {path: sha256}")
    if len(obj) > MAX_FILES:
        fail("source manifest has too many files")
    ps = PathSet()
    out = {}
    for rel, sha in obj.items():
        nrel = norm_rel(rel)
        if nrel != rel:
            fail(f"non-canonical manifest path: {rel!r}")
        source_path_policy(rel)
        ps.add(rel)
        out[rel] = expect_hex(sha, f"manifest sha256 for {rel}")
    for req in REQUIRED_SOURCE:
        if req not in out:
            fail(f"source manifest lacks required file {req}")
    return out


DEFAULT_EXCLUDED_DIRS = {".git", ".dart_tool", ".idea", ".vscode", ".pub-cache", ".gradle"}


def collect_tree(tree: str) -> tuple[list[tuple[str, str, bool]], list[str]]:
    files, excluded = [], []
    root = os.path.realpath(tree)

    def walk(abs_dir: str, rel_dir: str) -> None:
        with os.scandir(abs_dir) as it:
            ents = sorted(it, key=lambda e: e.name)
        for e in ents:
            rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
            if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
                fail(f"symlink/junction in source tree: {rel!r}")
            if e.is_dir(follow_symlinks=False):
                low = rel.casefold()
                if (e.name in DEFAULT_EXCLUDED_DIRS or low == "apps/client/build"
                        or low.endswith("/flutter/ephemeral")):
                    excluded.append(rel + "/")
                    continue
                walk(e.path, rel)
            elif e.is_file(follow_symlinks=False):
                if e.name.casefold() in FORBIDDEN_SOURCE_NAMES:
                    fail(f"forbidden file in source tree: {rel!r}")
                st = e.stat(follow_symlinks=False)
                files.append((norm_rel(rel), e.path, bool(st.st_mode & 0o111)))
            else:
                fail(f"non-regular file in source tree: {rel!r}")
    walk(root, "")
    return files, excluded


def cmd_make_source_bundle(a) -> dict:
    files, excluded = collect_tree(a.tree)
    ps = PathSet()
    manifest = {}
    for rel, path, _ in files:
        source_path_policy(rel)
        ps.add(rel)
        manifest[rel] = sha256_file(path)[0]
    for req in REQUIRED_SOURCE:
        if req not in manifest:
            fail(f"tree lacks required file {req}")
    if a.expect_manifest:
        expected = load_json_strict(a.expect_manifest)
        if expected != manifest:
            extra = sorted(set(manifest) - set(expected))[:20]
            missing = sorted(set(expected) - set(manifest))[:20]
            changed = sorted(k for k in set(manifest) & set(expected) if manifest[k] != expected[k])[:20]
            fail(f"tree differs from --expect-manifest: extra={extra} missing={missing} changed={changed}")
    os.makedirs(a.out_dir, exist_ok=True)
    tar_path = os.path.join(a.out_dir, "source.tar.gz")
    man_path = os.path.join(a.out_dir, "source-manifest.json")
    for p in (tar_path, man_path):
        if os.path.exists(p) and not a.force:
            fail(f"refusing to overwrite {p} (use --force)")
    tmp = tar_path + ".partial"
    with open(tmp, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf:
                for rel, path, execbit in sorted(files):
                    ti = tarfile.TarInfo(rel)
                    ti.size = os.path.getsize(path)
                    ti.mtime = FIXED_TAR_MTIME
                    ti.mode = 0o755 if execbit else 0o644
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = ""
                    ti.type = tarfile.REGTYPE
                    with open(path, "rb") as f:
                        tf.addfile(ti, f)
    os.replace(tmp, tar_path)
    write_json(man_path, manifest)
    # Self-check: the tarball must re-verify against the manifest exactly.
    with tempfile.TemporaryDirectory() as td:
        extract_source(tar_path, load_source_manifest(man_path), os.path.join(td, "x"))
    summary = {
        "schema": SCHEMA_SOURCE,
        "source_sha256": sha256_file(tar_path)[0],
        "source_manifest_sha256": sha256_file(man_path)[0],
        "files": len(manifest),
        "pubspec_lock_sha256": manifest["apps/client/pubspec.lock"],
        "excluded_dirs": excluded,
    }
    write_json(os.path.join(a.out_dir, "SOURCE_BUNDLE.json"), summary)
    return summary


def extract_source(tar_path: str, manifest: dict[str, str], dest: str) -> dict:
    ensure_empty_dir(dest)
    ps = PathSet()
    total = 0
    written = 0
    with open(tar_path, "rb") as raw:
        with tarfile.open(fileobj=raw, mode="r|gz") as tf:
            for m in tf:
                if m.isdir():
                    norm_rel(m.name.rstrip("/"), is_dir=True)
                    continue
                if (not m.isreg() or m.type not in (tarfile.REGTYPE, tarfile.AREGTYPE)
                        or getattr(m, "sparse", None)):
                    fail(f"non-regular tar member (link/device/sparse): {m.name!r}")
                rel = norm_rel(m.name)
                if rel != m.name:
                    fail(f"non-canonical tar member name: {m.name!r}")
                ps.add(rel)
                if rel not in manifest:
                    fail(f"tar member not in manifest: {rel!r}")
                if m.size > MAX_FILE_BYTES:
                    fail(f"tar member too large: {rel!r}")
                total += m.size
                if total > MAX_TOTAL_BYTES:
                    fail("tar total size exceeds limit")
                src = tf.extractfile(m)
                if src is None:
                    fail(f"unreadable tar member: {rel!r}")
                _, n = write_new_file(dest, rel, src, manifest[rel], m.size)
                if n != m.size:
                    fail(f"tar member truncated: {rel!r}")
                written += 1
    missing = sorted(set(manifest) - {v for v in ps.files.values()})
    if missing:
        fail(f"manifest files missing from tar: {missing[:20]} (+{max(0, len(missing) - 20)})")
    return {"files": written, "bytes": total}


def cmd_extract_source(a) -> dict:
    tsha = expect_hex(a.expect_tar_sha, "source tar sha256")
    msha = expect_hex(a.expect_manifest_sha, "source manifest sha256")
    check_file_sha(a.manifest, msha, "source manifest")
    check_file_sha(a.tar, tsha, "source tar")
    manifest = load_source_manifest(a.manifest)
    res = extract_source(a.tar, manifest, a.dest)
    res.update({"source_dir": os.path.abspath(a.dest), "source_sha256": tsha,
                "source_manifest_sha256": msha,
                "pubspec_lock_sha256": manifest["apps/client/pubspec.lock"]})
    return res


def cmd_reverify_source(a) -> dict:
    manifest = load_source_manifest(a.manifest)
    changed, missing = [], []
    for rel, sha in sorted(manifest.items()):
        path = safe_join(a.root, rel)
        if not os.path.isfile(path) or os.path.islink(path):
            missing.append(rel)
            continue
        actual, _ = sha256_file(path)
        if actual != sha:
            with open(path, "rb") as f:
                data = f.read()
            crlf_only = hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest() == sha
            changed.append({"path": rel, "expected": sha, "actual": actual, "crlf_only": crlf_only})
    res = {"checked": len(manifest), "changed": changed, "missing": missing}
    if changed or missing:
        print(json.dumps(res, indent=2), file=sys.stderr)
        fail(f"frozen source modified during {a.stage}: {len(changed)} changed, {len(missing)} missing")
    return res


# --------------------------------------------------------------------------- pubspec.lock

def parse_pubspec_lock(text: str) -> dict:
    """Minimal parser for the pub-generated lock layout (2-space indentation)."""
    pkgs: dict[str, dict] = {}
    section = None
    cur = None
    in_desc = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0:
            section = line.rstrip(":")
            cur = None
            continue
        if section != "packages":
            continue
        if indent == 2:
            name = line.rstrip(":").strip('"')
            if name in pkgs:
                fail(f"duplicate lock package {name}")
            cur = pkgs.setdefault(name, {"description": {}})
            in_desc = False
            continue
        if cur is None:
            fail("malformed pubspec.lock")
        key, _, val = line.partition(":")
        val = val.strip().strip('"')
        if indent == 4:
            in_desc = key == "description"
            if key == "description" and val:
                cur["description"] = {"_scalar": val}
            elif key != "description":
                cur[key] = val
        elif indent == 6 and in_desc:
            cur["description"][key] = val
    if not pkgs:
        fail("pubspec.lock has no packages")
    return pkgs


def cmd_check_lock(a) -> dict:
    app = os.path.realpath(a.app_dir)
    root = os.path.realpath(a.root)
    with open(os.path.join(app, "pubspec.lock"), "r", encoding="utf-8") as f:
        pkgs = parse_pubspec_lock(f.read())
    counts: dict[str, int] = {}
    paths = {}
    for name, p in sorted(pkgs.items()):
        src = p.get("source")
        counts[src] = counts.get(src, 0) + 1
        d = p["description"]
        if src == "hosted":
            if d.get("url") != "https://pub.dev":
                fail(f"hosted package {name} not from https://pub.dev")
            expect_hex(d.get("sha256", ""), f"lock sha256 for {name}")
        elif src == "sdk":
            if d.get("_scalar") != "flutter":
                fail(f"sdk package {name} not from the flutter SDK")
        elif src == "path":
            if d.get("relative") != "true":
                fail(f"path package {name} is not relative")
            rel = d.get("path", "")
            target = os.path.realpath(os.path.join(app, rel))
            if os.path.commonpath([root, target]) != root:
                fail(f"path package {name} resolves outside the frozen source")
            if not os.path.isfile(os.path.join(target, "pubspec.yaml")):
                fail(f"path package {name} missing pubspec.yaml in archive")
            paths[name] = os.path.relpath(target, root).replace(os.sep, "/")
        elif src == "git":
            expect_hex(d.get("resolved-ref", ""), f"git resolved-ref for {name}", 40)
            if not d.get("url", "").startswith("https://"):
                fail(f"git package {name} must use https")
        else:
            fail(f"unsupported lock source for {name}: {src!r}")
    return {"packages": len(pkgs), "sources": counts, "path_packages": paths}


# --------------------------------------------------------------------------- runtime package

def classify_runtime(rel: str) -> str:
    """Return 'runtime' or 'dev'; fail for anything unexpected."""
    low = rel.casefold()
    ext = os.path.splitext(low)[1]
    if ext in DEV_EXTS:
        return "dev"
    if "/" not in rel:
        if ext in ROOT_RUNTIME_EXTS:
            return "runtime"
        fail(f"unexpected top-level file in Release output: {rel!r}")
    if low.startswith("data/"):
        return "runtime"
    # Native-asset hooks place DLLs beside the exe; nothing else may live elsewhere.
    fail(f"unexpected file outside data/ in Release output: {rel!r}")
    return "dev"


def native_runtime_expectations(native: dict) -> dict:
    exp = {}
    for rel, (size, sha) in native.items():
        exp["data/" + rel if rel.startswith("licenses/") else rel] = (size, sha)
    return exp


def cmd_package(a) -> dict:
    engine = expect_hex(a.expect_engine_sha, "engine sha256")
    native = load_native_manifest(load_json_strict(a.native_manifest))
    rel_root = os.path.realpath(a.release_dir)
    files, _ = collect_tree(rel_root)
    ps = PathSet()
    runtime, dev = [], []
    for rel, path, _ in files:
        ps.add(rel)
        (runtime if classify_runtime(rel) == "runtime" else dev).append((rel, path))
    records = []
    total = 0
    for rel, path in sorted(runtime):
        sha, n = sha256_file(path)
        records.append({"path": rel, "bytes": n, "sha256": sha})
        total += n
    by_path = {r["path"]: r for r in records}
    for req in REQUIRED_RUNTIME:
        if req not in by_path:
            fail(f"Release output lacks required runtime file {req}")
    if by_path["atermius_rdp.dll"]["sha256"] != engine:
        fail("packaged atermius_rdp.dll differs from the pinned engine")
    for rel, (size, sha) in native_runtime_expectations(native).items():
        r = by_path.get(rel)
        if r is None or r["bytes"] != size or r["sha256"] != sha:
            fail(f"packaged native file differs from native bundle: {rel}")
    os.makedirs(os.path.dirname(os.path.abspath(a.out_zip)), exist_ok=True)
    zsha = deterministic_zip(a.out_zip, [(r, p) for r, p in runtime], deflate=True)
    meta = {}
    for kv in a.meta or []:
        k, sep, v = kv.partition("=")
        if not sep or not re.match(r"^[a-z][a-z0-9_]{0,63}$", k):
            fail(f"bad --meta {kv!r}")
        meta[k] = v
    manifest = {
        "schema": SCHEMA_RUNTIME,
        "archive": os.path.basename(a.out_zip),
        "archive_sha256": zsha,
        "engine_sha256": engine,
        "file_count": len(records),
        "total_bytes": total,
        "files": records,
        "excluded_dev_files": sorted(r for r, _ in dev),
        "build": meta,
    }
    write_json(a.out_manifest, manifest)
    atomic_write(a.out_zip + ".sha256", f"{zsha}  {os.path.basename(a.out_zip)}\n".encode())
    return {"archive_sha256": zsha, "file_count": len(records), "total_bytes": total,
            "excluded_dev_files": len(dev)}


# --------------------------------------------------------------------------- verify runtime

def manifest_records(obj, *, allow_backslash: bool) -> dict[str, tuple[int, str]]:
    recs = obj.get("files") if isinstance(obj, dict) else obj
    if not isinstance(recs, list) or not recs:
        fail("manifest has no file records")
    out = {}
    ps = PathSet()
    for r in recs:
        if not isinstance(r, dict) or not {"path", "bytes", "sha256"} <= set(r):
            fail("manifest record must have path/bytes/sha256")
        rel = norm_rel(r["path"], allow_backslash=allow_backslash)
        ps.add(rel)
        if not isinstance(r["bytes"], int) or r["bytes"] < 0:
            fail(f"bad byte count for {rel}")
        out[rel] = (r["bytes"], expect_hex(r["sha256"], f"sha256 for {rel}"))
    return out


def cmd_verify_runtime(a) -> dict:
    actual_zip_sha, zip_bytes = sha256_file(a.zip)
    mobj = load_json_strict(a.manifest)
    if not isinstance(mobj, dict) or mobj.get("schema") != SCHEMA_RUNTIME:
        fail("runtime manifest schema mismatch")
    if mobj.get("archive_sha256") != actual_zip_sha:
        fail("runtime ZIP sha256 does not match manifest archive_sha256")
    if a.expect_archive_sha and expect_hex(a.expect_archive_sha, "archive sha") != actual_zip_sha:
        fail("runtime ZIP sha256 does not match the independently expected value")
    engine = expect_hex(a.expect_engine_sha, "engine sha256")
    if mobj.get("engine_sha256") != engine:
        fail("manifest engine_sha256 differs from expected engine")
    expected = manifest_records(mobj, allow_backslash=False)
    if mobj.get("file_count") != len(expected):
        fail("manifest file_count mismatch")
    if mobj.get("total_bytes") != sum(s for s, _ in expected.values()):
        fail("manifest total_bytes mismatch")
    for rel in expected:
        if classify_runtime(rel) != "runtime":
            fail(f"manifest lists a non-runtime file: {rel}")
    for req in REQUIRED_RUNTIME:
        if req not in expected:
            fail(f"runtime lacks required file {req}")
    if expected["atermius_rdp.dll"][1] != engine:
        fail("manifest atermius_rdp.dll differs from expected engine")
    if a.native_manifest:
        native = load_native_manifest(load_json_strict(a.native_manifest))
        for rel, val in native_runtime_expectations(native).items():
            if expected.get(rel) != val:
                fail(f"runtime native file differs from native bundle: {rel}")
    if a.extract_to:
        ensure_empty_dir(a.extract_to)
    with zipfile.ZipFile(a.zip) as zf:
        entries = zip_entries_strict(zf, allow_backslash=False)
        names = {rel for rel, _ in entries}
        if names != set(expected):
            fail(f"ZIP entries differ from manifest: extra={sorted(names - set(expected))[:10]} "
                 f"missing={sorted(set(expected) - names)[:10]}")
        for rel, info in entries:
            size, sha = expected[rel]
            if info.file_size != size:
                fail(f"ZIP entry size differs from manifest: {rel}")
            if a.extract_to:
                with zf.open(info) as src:
                    write_new_file(a.extract_to, rel, src, sha, size)
            else:
                got, _ = read_entry_sha(zf, info)
                if got != sha:
                    fail(f"ZIP entry sha256 differs from manifest: {rel}")
    res = {"archive_sha256": actual_zip_sha, "archive_bytes": zip_bytes,
           "file_count": len(expected), "engine_sha256": engine, "status": "VERIFIED"}
    if a.reference_manifest:
        ref = manifest_records(load_json_strict(a.reference_manifest), allow_backslash=True)
        res["vs_reference"] = {
            "added": sorted(set(expected) - set(ref)),
            "removed": sorted(set(ref) - set(expected)),
            "changed": sorted(k for k in set(ref) & set(expected) if ref[k] != expected[k]),
            "unchanged": len([k for k in set(ref) & set(expected) if ref[k] == expected[k]]),
        }
    return res


# --------------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("make-native-bundle")
    p.add_argument("--from-zip", required=True)
    p.add_argument("--expect-zip-sha", default=R8_ZIP_SHA256)
    p.add_argument("--out", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_make_native_bundle)

    p = sub.add_parser("make-source-bundle")
    p.add_argument("--tree", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--expect-manifest")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_make_source_bundle)

    p = sub.add_parser("extract-native")
    p.add_argument("--zip", required=True)
    p.add_argument("--expect-sha", required=True)
    p.add_argument("--expect-engine-sha", required=True)
    p.add_argument("--dest", required=True)
    p.add_argument("--manifest-out")
    p.set_defaults(fn=cmd_extract_native)

    p = sub.add_parser("extract-source")
    p.add_argument("--tar", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--expect-tar-sha", required=True)
    p.add_argument("--expect-manifest-sha", required=True)
    p.add_argument("--dest", required=True)
    p.set_defaults(fn=cmd_extract_source)

    p = sub.add_parser("reverify-source")
    p.add_argument("--manifest", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--stage", default="build")
    p.set_defaults(fn=cmd_reverify_source)

    p = sub.add_parser("check-lock")
    p.add_argument("--app-dir", required=True)
    p.add_argument("--root", required=True)
    p.set_defaults(fn=cmd_check_lock)

    p = sub.add_parser("package")
    p.add_argument("--release-dir", required=True)
    p.add_argument("--native-manifest", required=True)
    p.add_argument("--expect-engine-sha", required=True)
    p.add_argument("--out-zip", required=True)
    p.add_argument("--out-manifest", required=True)
    p.add_argument("--meta", action="append")
    p.set_defaults(fn=cmd_package)

    p = sub.add_parser("verify-runtime")
    p.add_argument("--zip", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--expect-archive-sha")
    p.add_argument("--expect-engine-sha", default=ENGINE_SHA256)
    p.add_argument("--native-manifest")
    p.add_argument("--reference-manifest")
    p.add_argument("--extract-to")
    p.set_defaults(fn=cmd_verify_runtime)

    p.add_argument("--json-out")
    for sp in sub.choices.values():
        if sp is not p:
            sp.add_argument("--json-out")
    a = ap.parse_args(argv)
    try:
        res = a.fn(a)
    except PolicyError as e:
        print(f"winrt_tool {a.cmd}: FAIL: {e}", file=sys.stderr)
        return 2
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError, ValueError, gzip.BadGzipFile) as e:
        print(f"winrt_tool {a.cmd}: FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    out = json.dumps(res, indent=2, sort_keys=True)
    if a.json_out:
        atomic_write(a.json_out, (out + "\n").encode())
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
