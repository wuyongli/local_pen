"""Private persistent public-font storage shared by renderer subprocesses."""

import os
import re
import stat
import sys
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory, gettempdir
from time import time

FONT_MAGIC = (b"\x00\x01\x00\x00", b"OTTO", b"wOFF", b"wOF2")
MAX_FONT_BYTES = 24 * 1024 * 1024


@contextmanager
def library_lock(cache):
    import fcntl

    fd = os.open(cache / ".library.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise OSError("Font library lock must be private")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def cache_entries(cache):
    entries = []
    for p in cache.iterdir():
        try:
            info = p.lstat()
            if stat.S_ISREG(info.st_mode) and re.fullmatch(r"[a-f0-9]{64}", p.name):
                entries.append((info.st_mtime_ns, p, info.st_size))
            elif (stat.S_ISREG(info.st_mode) and p.name.startswith("font-download-")
                  and info.st_uid == os.getuid() and time() - info.st_mtime > 86400):
                p.unlink()
        except OSError:
            continue
    return entries


def store_font(cache, temporary, key, limit):
    cache, temporary = Path(cache), Path(temporary)
    info = cache.lstat()
    if (not cache.is_absolute() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700 or temporary.parent != cache
            or not temporary.name.startswith("font-download-") or not re.fullmatch(r"[a-f0-9]{64}", key)
            or not 32 * 1024 * 1024 <= limit <= 2048 * 1024 * 1024):
        raise ValueError("Invalid font library publication")
    try:
        fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as font:
            info = os.fstat(font.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or not 4 <= info.st_size <= MAX_FONT_BYTES or font.read(4) not in FONT_MAGIC):
                raise ValueError("Invalid font bytes")
            os.fsync(font.fileno())
        size = info.st_size
        destination = cache / key
        with library_lock(cache):
            entries = cache_entries(cache)
            occupied = sum(n for _, path, n in entries if path != destination)
            for _, path, n in sorted(entries):
                if occupied + size <= limit:
                    break
                if path != destination:
                    path.unlink()
                    occupied -= n
            if occupied + size > limit:
                raise ValueError("Font library is full")
            os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


@lru_cache(maxsize=1)
def session_font_cache():
    return TemporaryDirectory(prefix="local-pen-fonts-")


def font_cache_limit():
    raw = os.environ.get("LOCAL_PEN_FONT_CACHE_MAX_MB", "256")
    if not raw.isascii() or not raw.isdigit() or not 32 <= int(raw) <= 2048:
        raise ValueError("LOCAL_PEN_FONT_CACHE_MAX_MB must be an integer from 32 to 2048")
    return int(raw) * 1024 * 1024


def migrate_font_cache(cache, legacy, limit):
    try:
        info = legacy.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            return
    except OSError:
        return
    with library_lock(cache):
        occupied = sum(size for _, _, size in cache_entries(cache))
        entries = []
        for source in legacy.iterdir():
            try:
                info = source.lstat()
                if (not re.fullmatch(r"[a-f0-9]{64}", source.name) or not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid() or not 4 <= info.st_size <= 24 * 1024 * 1024):
                    continue
                entries.append((info.st_mtime_ns, source))
            except OSError:
                continue
        for _, source in sorted(entries, reverse=True):
            destination = cache / source.name
            if os.path.lexists(destination):
                continue
            try:
                fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as font:
                    info = os.fstat(font.fileno())
                    if (not stat.S_ISREG(info.st_mode) or not 4 <= info.st_size <= MAX_FONT_BYTES
                            or occupied + info.st_size > limit):
                        continue
                    data = font.read(MAX_FONT_BYTES + 1)
                if (data[:4] not in FONT_MAGIC
                        or len(data) > 24 * 1024 * 1024 or occupied + len(data) > limit):
                    continue
                with NamedTemporaryFile(dir=cache) as pending:
                    pending.write(data)
                    pending.flush()
                    os.utime(pending.name, ns=(info.st_atime_ns, info.st_mtime_ns))
                    destination.hardlink_to(pending.name)
                occupied += len(data)
            except OSError:
                continue


def font_cache_directory():
    limit = font_cache_limit()
    override = os.environ.get("LOCAL_PEN_FONT_LIBRARY_DIR")
    if override is not None and not Path(override).is_absolute():
        raise ValueError("LOCAL_PEN_FONT_LIBRARY_DIR must be an absolute path")
    if hasattr(os, "getuid"):
        if override is not None:
            cache = Path(override)
        elif sys.platform == "darwin":
            cache = Path.home() / "Library" / "Application Support" / "local_pen" / "fonts"
        else:
            data = Path(os.environ.get("XDG_DATA_HOME", ""))
            if not data.is_absolute():
                data = Path.home() / ".local" / "share"
            cache = data / "local_pen" / "fonts"
        try:
            cache.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = cache.lstat()
            if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700:
                legacy = Path(gettempdir()) / f"local-pen-public-fonts-v1-{os.getuid()}"
                if legacy != cache:
                    migrate_font_cache(cache, legacy, limit)
                return str(cache)
        except OSError:
            pass
    return session_font_cache().name


if __name__ == "__main__":
    try:
        if len(sys.argv) != 6 or sys.argv[1] != "store":
            raise ValueError("Invalid font cache helper arguments")
        store_font(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]))
    except (OSError, ValueError):
        raise SystemExit(1)
