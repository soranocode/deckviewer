# Windows x64 portable build. Build in a clean environment without Qt or CEF.
from pathlib import Path
import hashlib

root = Path(SPECPATH)
staging = root / ".build-metadata"
staging.mkdir(exist_ok=True)
source_names = (
    "server.py", "engine.py", "renderer.py", "blizzard_provider.py",
    "local_library.py", "card_library.py", "desktop.py",
)
build_id = ":".join(hashlib.sha256((root / name).read_bytes()).hexdigest()
                    for name in source_names)
(staging / "build-id.txt").write_text(build_id, encoding="utf-8")

# Whitelist application resources: do not package local cache, generated decks,
# screenshots, virtual environments, tests, or unused legacy Zilliax composites.
datas = [(str(root / name), name) for name in
         ("backs", "death_knight", "labels", "library", "web")]
datas += [(str(root / name), ".") for name in
          ("belwe.ttf", "example.png", "LICENSE")]
datas.append((str(staging / "build-id.txt"), "."))
for name in ("THIRD_PARTY_NOTICES.md",):
    if (root / name).is_file():
        datas.append((str(root / name), "."))
if (root / "licenses").is_dir():
    datas.append((str(root / "licenses"), "licenses"))

analysis = Analysis(
    [str(root / "desktop.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    # Selected at runtime by pywebview; its PyInstaller hook collects WebView2
    # and Python.NET DLLs. No additional Chromium copy is shipped.
    hiddenimports=["webview.platforms.winforms", "webview.platforms.edgechromium"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "cefpython3",
              "gi", "webview.platforms.qt", "webview.platforms.gtk",
              "webview.platforms.cocoa", "webview.platforms.android",
              "webview.platforms.cef", "tkinter", "numpy", "matplotlib"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="DeckViewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    contents_directory="_internal",
    **({"icon": str(root / "web/icon.ico")}
       if (root / "web/icon.ico").is_file() else {}),
)
bundle = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="deckviewer",
)
