"""Access rules supported when replacing existing archives."""

import errno
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path


def preserve_access(source: Path, target: Path, previous: os.stat_result) -> None:
    """Apply the supported access policy before writing archive contents."""
    try:
        if sys.platform == "linux":
            _copy_linux_access(source, target, previous)
        elif os.name == "nt":
            _copy_windows_access(source, target)
        else:
            # macOS supports extended ACLs, but the Linux ACL check cannot
            # inspect them. Refuse existing-file updates until we can verify
            # their access policy. New files do not go through this check.
            raise PermissionError(f"access policy inspection is unsupported on {sys.platform}")
    except OSError as error:
        raise PermissionError(f"Cannot preserve archive access for {source}: {error}") from error


def _copy_linux_access(source: Path, target: Path, previous: os.stat_result) -> None:
    for path in (source, target):
        # An absent access ACL on Linux is reported as ENODATA. Refuse
        # unsupported filesystems and permission errors rather than guessing.
        try:
            os.getxattr(path, "system.posix_acl_access", follow_symlinks=False)
        except OSError as error:
            if error.errno != errno.ENODATA:
                raise
        else:
            raise PermissionError(f"extended POSIX ACL on {path}")

    def security_attributes(path):
        return {
            name: os.getxattr(path, name, follow_symlinks=False)
            for name in os.listxattr(path, follow_symlinks=False)
            if name.startswith("security.")
        }

    if security_attributes(source) != security_attributes(target):
        raise PermissionError("security attributes differ from the replacement")
    mode = stat.S_IMODE(previous.st_mode)
    if mode & 0o7000:
        raise PermissionError("special POSIX mode bits are unsupported")
    staged = target.stat()
    owner_group = (previous.st_uid, previous.st_gid)
    if (staged.st_uid, staged.st_gid) != owner_group:
        os.chown(target, *owner_group)
    target.chmod(mode)
    staged = target.stat()
    if (staged.st_uid, staged.st_gid, stat.S_IMODE(staged.st_mode)) != (*owner_group, mode):
        raise PermissionError("replacement owner, group or mode differs")


def _copy_windows_access(source: Path, target: Path) -> None:
    """Keep the existing owner, group and DACL, without inheriting extra access."""
    import ctypes
    from ctypes import wintypes

    security = ctypes.WinDLL("advapi32")
    local_free = ctypes.WinDLL("kernel32").LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL
    pointer = ctypes.c_void_p
    pointer_out = ctypes.POINTER(pointer)
    get_security = security.GetNamedSecurityInfoW
    get_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             pointer_out, pointer_out, pointer_out, pointer_out, pointer_out]
    get_security.restype = wintypes.DWORD
    set_security = security.SetNamedSecurityInfoW
    set_security.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                             pointer, pointer, pointer, pointer]
    set_security.restype = wintypes.DWORD

    file_object = 1  # SE_FILE_OBJECT
    owner_group_dacl = 0x00000007
    access_extensions = 0x00000070  # Integrity label, resource attributes, central policy
    protected_dacl = 0x80000000
    unprotected_dacl = 0x20000000
    dacl_protected = 0x1000  # SE_DACL_PROTECTED
    get_control = security.GetSecurityDescriptorControl
    get_control.argtypes = [
        pointer, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD),
    ]
    get_control.restype = wintypes.BOOL
    sid_length = security.GetLengthSid
    sid_length.argtypes = [pointer]
    sid_length.restype = wintypes.DWORD

    class AclHeader(ctypes.Structure):
        _fields_ = [
            ("revision", ctypes.c_ubyte), ("reserved", ctypes.c_ubyte),
            ("size", ctypes.c_ushort), ("ace_count", ctypes.c_ushort),
            ("reserved2", ctypes.c_ushort),
        ]

    @contextmanager
    def read_access(path):
        owner, group, dacl, extensions, descriptor = (pointer() for _ in range(5))
        error = get_security(
            str(path), file_object, owner_group_dacl | access_extensions,
            ctypes.byref(owner), ctypes.byref(group), ctypes.byref(dacl),
            ctypes.byref(extensions), ctypes.byref(descriptor),
        )
        if error:
            raise ctypes.WinError(error)
        try:
            if extensions and AclHeader.from_address(extensions.value).ace_count:
                raise PermissionError("unsupported Windows access policy beyond the DACL")
            if not (owner and group and dacl):
                raise PermissionError("Windows owner, group or DACL is missing")
            control, revision = wintypes.WORD(), wintypes.DWORD()
            if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
                raise ctypes.WinError()
            yield owner, group, dacl, bool(control.value & dacl_protected)
        finally:
            local_free(descriptor)

    def access_bytes(owner, group, dacl, protected):
        return (
            ctypes.string_at(owner, sid_length(owner)),
            ctypes.string_at(group, sid_length(group)),
            ctypes.string_at(dacl, AclHeader.from_address(dacl.value).size),
            protected,
        )

    with read_access(source) as original:
        expected = access_bytes(*original)
        with read_access(target) as staged:
            if access_bytes(*staged) == expected:
                return
        owner, group, dacl, protected = original
        inheritance = protected_dacl if protected else unprotected_dacl
        error = set_security(
            str(target), file_object, owner_group_dacl | inheritance,
            owner, group, dacl, None,
        )
        if error:
            raise ctypes.WinError(error)
        with read_access(target) as staged:
            if access_bytes(*staged) != expected:
                raise PermissionError("replacement owner, group or DACL differs")
