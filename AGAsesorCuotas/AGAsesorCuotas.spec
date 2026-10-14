# -*- mode: python ; coding: utf-8 -*-
# Compilación: pyinstaller --noconfirm AGAsesorCuotas.spec   (solo en Windows)

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[("templates", "templates"), ("static", "static")],
    hiddenimports=[
        "outlook_sender", "mock_sender",
        "win32com", "win32com.client", "pythoncom", "pywintypes", "win32timezone",
    ],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AGAsesorCuotas",
    console=True,  # la ventana de consola indica que la app está en marcha; al cerrarla se detiene
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="AGAsesorCuotas")
