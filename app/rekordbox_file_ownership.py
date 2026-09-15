"""Windows handle ownership for publication without clobbering competing writers.

An owned existing file denies other write/delete handles. Replacement renames the
old object by its owning handle into retained evidence, then renames a separately
owned staged object to the now-absent name with ReplaceIfExists=False. A competing
creation wins that name rather than being overwritten. Handles are crash-released.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path


class OwnershipError(OSError):
    pass


class OwnedFile:
    def __init__(self, path, *, create=False, read_only=False):
        if os.name != 'nt':
            raise OwnershipError('Windows file ownership is required')
        self.path = Path(path)
        self.handle = None
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.api.CreateFileW.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.api.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
            ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
        self.api.SetFilePointerEx.restype = wintypes.BOOL
        self.api.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        self.api.ReadFile.restype = wintypes.BOOL
        self.api.WriteFile.argtypes = self.api.ReadFile.argtypes
        self.api.WriteFile.restype = wintypes.BOOL
        self.api.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        self.api.FlushFileBuffers.restype = wintypes.BOOL
        self.api.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int,
            wintypes.LPVOID, wintypes.DWORD]
        self.api.SetFileInformationByHandle.restype = wintypes.BOOL
        access = 0x80000000 if read_only else 0x80000000 | 0x40000000 | 0x10000
        handle = self.api.CreateFileW(str(self.path.absolute()), access,
            1, None, 1 if create else 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if error in (2, 3) and not create:
                raise FileNotFoundError(str(self.path))
            raise OwnershipError(error, 'File ownership unavailable')
        self.handle = handle

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def digest(self):
        if not self.api.SetFilePointerEx(self.handle, 0, None, 0):
            raise OwnershipError('Cannot seek owned file')
        h = hashlib.sha256()
        buffer = ctypes.create_string_buffer(1024 * 1024)
        count = wintypes.DWORD()
        while True:
            if not self.api.ReadFile(self.handle, buffer, len(buffer), ctypes.byref(count), None):
                raise OwnershipError('Cannot read owned file')
            if not count.value:
                return h.hexdigest()
            h.update(buffer.raw[:count.value])

    def write(self, payload):
        buffer = ctypes.create_string_buffer(payload)
        count = wintypes.DWORD()
        if not self.api.WriteFile(self.handle, buffer, len(payload), ctypes.byref(count), None) or count.value != len(payload):
            raise OwnershipError('Cannot write owned staging file')
        if not self.api.FlushFileBuffers(self.handle):
            raise OwnershipError('Cannot flush owned staging file')

    def rename(self, target):
        target = Path(target).absolute()
        name = str(target)
        encoded_length = len(name.encode('utf-16-le'))
        class RenameInfo(ctypes.Structure):
            _fields_ = [('ReplaceIfExists', wintypes.BOOLEAN), ('RootDirectory', wintypes.HANDLE),
                ('FileNameLength', wintypes.DWORD), ('FileName', wintypes.WCHAR * (encoded_length // 2 + 1))]
        info = RenameInfo()
        info.ReplaceIfExists = False
        info.RootDirectory = None
        info.FileNameLength = encoded_length
        info.FileName = name
        if not self.api.SetFileInformationByHandle(self.handle, 3, ctypes.byref(info), ctypes.sizeof(info)):
            raise OwnershipError(ctypes.get_last_error(), 'Owned no-clobber rename failed')
        self.path = target
