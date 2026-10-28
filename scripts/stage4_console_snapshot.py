"""Read a test PTY's physical Console cells, including a WSL child (no UI input)."""

import ctypes
import json
import sys
from ctypes.wintypes import DWORD, HANDLE

from prompt_toolkit.win32_types import CONSOLE_SCREEN_BUFFER_INFO, COORD

kernel = ctypes.windll.kernel32
kernel.FreeConsole()
if not kernel.AttachConsole(int(sys.argv[1])):
    raise ctypes.WinError()
kernel.CreateFileW.restype = HANDLE
handle = kernel.CreateFileW("CONOUT$", 0x80000000, 3, None, 3, 0, None)
info = CONSOLE_SCREEN_BUFFER_INFO()
if not kernel.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
    raise ctypes.WinError()
rows = []
for y in range(info.srWindow.Top, info.srWindow.Bottom + 1):
    cells = ctypes.create_unicode_buffer(info.dwSize.X + 1)
    read = DWORD()
    if not kernel.ReadConsoleOutputCharacterW(handle, cells, DWORD(info.dwSize.X),
                                             COORD(0, y), ctypes.byref(read)):
        raise ctypes.WinError()
    rows.append(cells[:read.value])
kernel.CloseHandle(handle)
kernel.FreeConsole()
print(json.dumps(rows))
