"""Identify the disk behind a path, on Windows, macOS and Linux.

A drive letter is not an identity. The same card can appear as ``E:``, ``F:`` or
``G:`` depending on which slot it was pushed into, whether a reader was used, and
what else is plugged in; a whole removable volume can also be mounted at a
different path on macOS or Linux. Anything that decides "is this the same card?"
by comparing paths will answer "no" after a replug.

This module answers the question the other way round: ask the operating system
for something that belongs to the *volume* rather than to its mount point, and
fall back through the options until one is trustworthy.

The fallbacks matter because the obvious answer is often missing. A camera SD
card formatted as exFAT by the camera very often reports a volume serial of 0
(``stat().st_dev`` is 0 as well on Windows), and a network share reports 0 too -
so "serial == 0" has to mean *unknown*, never "matches everything". Each backend
returns ``None`` rather than a placeholder when it cannot be sure.

Nothing here writes to the volume; every call is a read.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: How a volume identity was obtained, best first.
SOURCE_VOLUME_GUID = "volume-guid"  # Windows: permanent per volume
SOURCE_UUID = "uuid"  # Linux / macOS: filesystem UUID
SOURCE_SERIAL = "serial"  # Windows: volume serial from the filesystem
SOURCE_DEVICE = "device"  # POSIX: device number behind the mount point

#: Ordered most- to least-trustworthy. Used to decide whether a weaker identity
#: may be compared against a stronger one stored earlier.
TRUST_ORDER = (
    SOURCE_VOLUME_GUID,
    SOURCE_UUID,
    SOURCE_SERIAL,
    SOURCE_DEVICE,
)


@dataclass(frozen=True)
class VolumeIdentity:
    """One volume's stable identifier.

    ``key`` is what gets stored and compared; ``source`` says how it was
    obtained, so a later run can tell whether the two keys are comparable at all
    (a Windows volume GUID and a Linux filesystem UUID are both "the volume", but
    a device number is only meaningful on the machine that produced it).
    """

    key: str
    source: str

    @property
    def trusted(self) -> bool:
        """True for identities that survive a replug on any machine.

        ``device`` does not: POSIX device numbers are allocated per boot and are
        not stable enough to prove "this is the card you used last time". It is
        still better than nothing for spotting a *change* within one session.
        """
        return self.source in (SOURCE_VOLUME_GUID, SOURCE_UUID, SOURCE_SERIAL)

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "source": self.source}


def _mount_root(path: str) -> str:
    """The mount point that contains ``path``, spelled the way the OS does."""
    resolved = os.path.abspath(path)
    if os.name == "nt":
        drive, _ = os.path.splitdrive(resolved)
        if drive:
            return drive + os.sep
    # POSIX: walk up to a directory that is a mount point. Comparing ``st_dev``
    # with the parent's is the portable test and needs no /proc or statfs.
    current = resolved
    if not os.path.isdir(current):
        current = os.path.dirname(current) or os.sep
    while True:
        parent = os.path.dirname(current)
        if not parent or parent == current:
            return current
        try:
            if os.stat(current).st_dev != os.stat(parent).st_dev:
                return current
        except OSError:
            return current
        current = parent


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def _windows_volume_guid(root: str) -> str | None:
    """``\\\\?\\Volume{...}\\`` for the volume mounted at ``root``.

    Windows keeps the GUID with the volume, and it stays the same across reboots
    and across drive letters - which is exactly the property needed here. It does
    change if the volume is reformatted, and that is the correct behaviour: a
    reformatted card is a different card as far as the album is concerned.
    """
    try:
        import ctypes
    except ImportError:  # pragma: no cover - always available on Windows
        return None
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    buffer = ctypes.create_unicode_buffer(261)
    ok = windll.kernel32.GetVolumeNameForVolumeMountPointW(
        ctypes.c_wchar_p(root), buffer, 261
    )
    if not ok:
        return None
    name = buffer.value.strip()
    # Normalise case: the GUID itself is hex, the wrapper is fixed.
    return name.lower() or None


def _windows_volume_serial(root: str) -> tuple[int, str, str] | None:
    """``(serial, filesystem, label)`` for ``root``, straight from the filesystem."""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover - always available on Windows
        return None
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    label = ctypes.create_unicode_buffer(261)
    filesystem = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    max_length = wintypes.DWORD()
    flags = wintypes.DWORD()
    ok = windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root),
        label,
        261,
        ctypes.byref(serial),
        ctypes.byref(max_length),
        ctypes.byref(flags),
        filesystem,
        261,
    )
    if not ok:
        return None
    return int(serial.value), filesystem.value or "", label.value or ""


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------

def _linux_uuid(root: str) -> str | None:
    """Filesystem UUID for the volume mounted at ``root``.

    ``/dev/disk/by-uuid`` is a directory of symlinks to device nodes, so the
    reverse lookup is: resolve the mount point to its device, then find the
    symlink that points at that device. ``blkid`` would be simpler but is not
    always installed (and needs the mount table to be readable); this needs
    nothing but the directory that udev already maintains.
    """
    by_uuid = "/dev/disk/by-uuid"
    if not os.path.isdir(by_uuid):
        return None
    target = os.path.realpath(_device_for_mount(root) or "")
    if not target:
        return None
    for uuid in sorted(os.listdir(by_uuid)):
        try:
            if os.path.realpath(os.path.join(by_uuid, uuid)) == target:
                return uuid
        except OSError:
            continue
    return None


def _device_for_mount(root: str) -> str | None:
    """The block device mounted at ``root``, from ``/proc/self/mounts``."""
    try:
        with open("/proc/self/mounts", encoding="utf-8", errors="replace") as handle:
            entries = handle.read().splitlines()
    except OSError:
        return None
    best = None
    for line in entries:
        parts = line.split()
        if len(parts) < 2:
            continue
        device, mount = parts[0], parts[1]
        # Longest matching mount point wins, the way the kernel resolves it.
        inside = mount == root or root.startswith(mount.rstrip("/") + "/")
        if inside and (best is None or len(mount) > len(best[1])):
            best = (device, mount)
    return best[0] if best else None


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------

def _macos_volume_uuid(root: str) -> str | None:
    """Volume UUID from ``diskutil info -plist``.

    ``diskutil`` is part of macOS and reports the volume UUID that Disk
    Arbitration keeps, which is the same idea as the Windows volume GUID. The
    plist output is parsed rather than the human-readable text because the text
    layout has changed between releases.
    """
    try:
        result = subprocess.run(
            ["diskutil", "info", "-plist", root],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        import plistlib

        info = plistlib.loads(result.stdout)
    except Exception:  # noqa: BLE001 - a malformed plist is just "unknown"
        return None
    for key in ("VolumeUUID", "DiskUUID"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def volume_identity(path: str | None) -> VolumeIdentity | None:
    """Identify the volume holding ``path``, or ``None`` when it cannot be known.

    The first backend that returns something trustworthy wins. ``None`` is a
    real answer - "this platform would not tell us" - and callers must treat it
    as unknown rather than as a match; see :func:`identities_compatible`.
    """
    if not path:
        return None
    resolved = os.path.abspath(path)
    if not os.path.exists(resolved):
        # Still identify the volume the path *would* live on: a card that was
        # pulled out has no directory to stat, but its root may still exist.
        resolved = os.path.dirname(resolved) or resolved
    if not os.path.exists(resolved):
        return None
    root = _mount_root(resolved)

    if os.name == "nt":
        guid = _windows_volume_guid(root)
        if guid:
            return VolumeIdentity(guid, SOURCE_VOLUME_GUID)
        info = _windows_volume_serial(root)
        if info:
            serial, filesystem, _label = info
            # A zero serial means the filesystem did not store one (common for
            # exFAT camera cards). It is not an identity: two unrelated cards
            # would collide on it.
            if serial:
                return VolumeIdentity(
                    f"serial:{serial:08x}:{(filesystem or '').lower()}",
                    SOURCE_SERIAL,
                )
        return None

    if sys.platform == "darwin":
        uuid = _macos_volume_uuid(root)
        if uuid:
            return VolumeIdentity(f"uuid:{uuid}", SOURCE_UUID)
    else:
        uuid = _linux_uuid(root)
        if uuid:
            return VolumeIdentity(f"uuid:{uuid.lower()}", SOURCE_UUID)

    # Last resort on POSIX: the device number. Not stable across reboots, so it
    # is flagged as such and never used to claim two sessions saw the same card.
    try:
        device = os.stat(root).st_dev
    except OSError:
        return None
    if device:
        return VolumeIdentity(f"dev:{device}", SOURCE_DEVICE)
    return None


def identities_compatible(
    recorded: VolumeIdentity | None, current: VolumeIdentity | None
) -> tuple[bool, bool]:
    """Compare a stored identity with a freshly read one.

    Returns ``(same, certain)``. ``certain`` is False whenever either side is
    missing or was obtained in a way that cannot be compared - the caller then
    knows it is guessing, and must not use the answer to authorise a write.
    """
    if recorded is None or current is None:
        return False, False
    if recorded.source != current.source:
        # A different backend answered this time (a card moved between a reader
        # that exposes a GUID and one that does not). Nothing to compare.
        return False, False
    certain = recorded.trusted and current.trusted
    return recorded.key == current.key, certain
