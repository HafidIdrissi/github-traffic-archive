"""Access rules supported when replacing existing archives."""

import errno
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path


def _report_cleanup_error(path: Path, error: OSError) -> None:
    try:
        print(f"Could not clean up temporary archive file {path}: {error}", file=sys.stderr)
    except (OSError, ValueError):
        # Diagnostics must not hide the error that caused cleanup.
        pass


def create_windows_temp(directory: Path) -> tuple[int, Path]:
    """Create an existing archive's staging file with no inherited grants."""
    import ctypes
    import msvcrt
    import secrets
    from ctypes import wintypes

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    convert = security.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(pointer), pointer]
    convert.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", pointer),
                    ("inherit_handle", wintypes.BOOL)]

    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.POINTER(SecurityAttributes), wintypes.DWORD,
                       wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    descriptor = pointer()
    # Protected EMPTY DACL (not a null DACL): no inherited grants to readers
    # or ACL editors. Tightening an inherited DACL later cannot revoke handles.
    if not convert("D:P", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    path = directory / f".traffic-archive-{secrets.token_hex(16)}.tmp"
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        # The creation handle retains write/security rights while the empty
        # DACL denies subsequent opens. CREATE_NEW never overwrites a collision.
        handle = create(str(path), 0x400E0000, 7, ctypes.byref(attributes), 1, 0x80, None)
        error = ctypes.get_last_error()
    finally:
        kernel.LocalFree(descriptor)
    if handle == pointer(-1).value:
        raise ctypes.WinError(error)
    try:
        return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY), path
    except BaseException:
        kernel.CloseHandle(handle)
        try:
            path.unlink()
        except OSError as error:
            _report_cleanup_error(path, error)
        raise


def preserve_access(source: Path, target: Path, previous: os.stat_result,
                    *, target_fd: int) -> None:
    """Apply the supported access policy before writing archive contents."""
    try:
        if sys.platform == "linux":
            _copy_linux_access(source, target, previous)
        elif sys.platform == "darwin":
            # Ordinary macOS files can be updated, but mode bits alone do
            # not establish ACL absence. Check both files before any payload.
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                _require_macos_no_acl(fd)
                _require_macos_no_acl(target_fd)
                _copy_posix_access(target, previous)
            finally:
                os.close(fd)
        elif os.name == "nt":
            # EFS keys also restrict readers; matching a DACL alone cannot
            # preserve encryption on an individually encrypted archive.
            attributes = previous.st_file_attributes | target.stat().st_file_attributes
            if attributes & stat.FILE_ATTRIBUTE_ENCRYPTED:
                raise PermissionError("Windows encrypted archives are unsupported")
            _copy_windows_access(source, target, target_fd)
        else:
            raise PermissionError(f"access policy inspection is unsupported on {sys.platform}")
    except OSError as error:
        raise PermissionError(f"Cannot preserve archive access for {source}: {error}") from error


def _require_macos_no_acl(fd: int) -> None:
    """Require a successful, supported Darwin ACL query reporting no ACL."""
    import ctypes
    import struct

    query = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True).fgetattrlist
    query.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.c_size_t, ctypes.c_ulong]
    query.restype = ctypes.c_int
    # ATTR_CMN_EXTENDED_SECURITY only. Deliberately omit RETURNED_ATTRS and
    # PACK_INVAL_ATTRS: unsupported ACL inspection must fail, not omit data
    # or supply a default. Unlike acl_get_fd_np, this distinguishes failure
    # from an absent ACL. See Darwin's getattrlist(2) and vfs_attrlist.c.
    def read(common, volume, size):
        attributes = ctypes.create_string_buffer(struct.pack("=HH5I", 5, 0, common, volume, 0, 0, 0))
        result = ctypes.create_string_buffer(size)
        # FSOPT_REPORT_FULLSIZE prevents a truncated ACL from appearing absent.
        if query(fd, attributes, result, size, 4) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        if struct.unpack_from("=I", result.raw)[0] != size:
            raise PermissionError("extended macOS ACL or incomplete access-policy inspection")
        return result.raw

    # ATTR_VOL_INFO | ATTR_VOL_ATTRIBUTES: require native AND valid ACL
    # inspection, excluding a filesystem's synthesized no-ACL fallback.
    support = struct.unpack("=10I", read(0, 0xC0000000, 44)[4:])
    acl_attribute = 0x00400000
    if not (support[0] & support[5] & acl_attribute):
        raise PermissionError("native macOS ACL inspection is unsupported")
    # uint32 size + attrreference_t. Any security record, even an explicit
    # empty ACL, is outside this ordinary-permissions-only policy.
    size, offset, acl_size = struct.unpack("=IiI", read(acl_attribute, 0, 12))
    if acl_size != 0:
        raise PermissionError("extended macOS ACL is unsupported")


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
    _copy_posix_access(target, previous)


def _copy_posix_access(target: Path, previous: os.stat_result) -> None:
    mode = stat.S_IMODE(previous.st_mode)
    if mode & 0o7000:
        raise PermissionError("special POSIX mode bits are unsupported")
    staged = target.stat()
    if getattr(previous, "st_flags", 0) or getattr(staged, "st_flags", 0):
        raise PermissionError("POSIX file flags are unsupported")
    owner_group = (previous.st_uid, previous.st_gid)
    if (staged.st_uid, staged.st_gid) != owner_group:
        os.chown(target, *owner_group)
    target.chmod(mode)
    staged = target.stat()
    if (staged.st_uid, staged.st_gid, stat.S_IMODE(staged.st_mode)) != (*owner_group, mode):
        raise PermissionError("replacement owner, group or mode differs")


def _copy_windows_access(source: Path, target: Path, target_fd: int) -> None:
    """Keep the existing owner, group and DACL, without inheriting extra access."""
    import ctypes
    import msvcrt
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
    set_security = security.SetSecurityInfo
    set_security.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
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
            msvcrt.get_osfhandle(target_fd), file_object, owner_group_dacl | inheritance,
            owner, group, dacl, None,
        )
        if error:
            raise ctypes.WinError(error)
        with read_access(target) as staged:
            if access_bytes(*staged) != expected:
                raise PermissionError("replacement owner, group or DACL differs")
