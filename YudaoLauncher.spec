# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

# 项目根目录
base = Path(SPECPATH).resolve()

a = Analysis(
    [str(base / "app.py")],
    pathex=[str(base)],
    binaries=[],
    datas=[
        (str(base / "templates"), "templates"),
    ],
    hiddenimports=[
        "flask",
        "flask.json",
        "jinja2",
        "jinja2.ext",
        "itsdangerous",
        "werkzeug",
        "click",
        "markupsafe",
        "blinker",
        "paramiko",
        "paramiko.transport",
        "paramiko.ssh_exception",
        "Cryptodome.Cipher.AES",
        "Cryptodome.Cipher.DES3",
        "Cryptodome.Hash.SHA256",
        "Cryptodome.Hash.SHA1",
        "Cryptodome.Hash.HMAC",
        "Cryptodome.Hash.MD5",
        "Cryptodome.PublicKey.RSA",
        "Cryptodome.PublicKey.Ed25519",
        "Cryptodome.Util.number",
        "bcrypt",
        "pynacl",
        "nacl",
        "nacl.signing",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="YudaoLauncher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # 保留控制台，方便看日志和 Ctrl+C 退出
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
