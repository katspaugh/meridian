"""Locate Meridian state and preserve absolute paths held by pre-rename processes."""
import ctypes
import fcntl
import hashlib
import os
from pathlib import Path
import stat


PREVIOUS_NAME = "Muxteroid"


def migrate_directory(previous, current):
    """Swap the directory and its forwarding link atomically on macOS."""
    if previous.is_symlink() or not previous.is_dir():
        return
    # The lock follows the directory inode across the rename, including when
    # multiple hooks and the app all start during an upgrade.
    fd = os.open(previous, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if previous.is_symlink():
            return
        if current.exists():
            raise ValueError(f"Both {current} and {previous} contain data. Choose the data directory with MERIDIAN_HOME.")
        if not current.is_symlink():
            current.symlink_to(current.name)
        elif os.readlink(current) != current.name:
            raise ValueError(f"Unexpected destination link: {current}")
        # RENAME_SWAP preserves the old path at every instant. A staging link
        # left by an interrupted upgrade can be used again on the next launch.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.renamex_np(os.fsencode(previous), os.fsencode(current), 2) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(current))
    finally:
        os.close(fd)


def data_root():
    support = Path.home() / "Library/Application Support"
    old = support / PREVIOUS_NAME
    current = support / "Meridian"
    override = os.getenv("MERIDIAN_HOME") or os.getenv("MUXTEROID_HOME")
    separate = current.is_dir() and old.is_dir() and not current.samefile(old)
    if override and (separate or Path(override).expanduser().absolute() not in (current, old)):
        root = Path(override).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root, root
    migrate_directory(old, current)
    current.mkdir(parents=True, exist_ok=True, mode=0o700)
    return current, old


def terminal_paths(root, previous_root):
    def namespace(name, path):
        return name + "-" + hashlib.sha256(str(path).encode()).hexdigest()[:12]

    current = namespace("meridian", root)
    previous = namespace(PREVIOUS_NAME.lower(), previous_root)
    directory = Path("/tmp") / f"{current}-{os.getuid()}"
    old_directory = Path("/tmp") / f"{previous}-{os.getuid()}"
    # Old dtach daemons retain their bound directory and remove sockets there
    # when they exit. Use the same directory through the new name until reboot.
    if old_directory.is_dir() and not directory.exists():
        info = old_directory.lstat()
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077:
            try:
                directory.symlink_to(old_directory)
            except FileExistsError:
                pass
    tmux = Path(os.getenv("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"
    return previous if (tmux / previous).is_socket() else current, directory, old_directory
