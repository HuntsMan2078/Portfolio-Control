# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)
datas = [(str(ROOT/'index.html'),'.'),(str(ROOT/'manifest.json'),'.'),(str(ROOT/'service-worker.js'),'.'),(str(ROOT/'supabase_setup.sql'),'.')]
binaries=[]
hiddenimports=['webview','webview.platforms.cocoa','objc','Cocoa','Foundation','WebKit','cryptography','cryptography.fernet']
for package in ('webview','tzdata','cryptography'):
    try:
        d,b,h=collect_all(package); datas+=d; binaries+=b; hiddenimports+=h
    except Exception:
        pass
lb=ROOT/'vendor'/'longbridge'
if lb.exists(): binaries.append((str(lb),'tools'))

a=Analysis([str(ROOT/'app.py')], pathex=[str(ROOT)], binaries=binaries, datas=datas,
           hiddenimports=hiddenimports, hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False, optimize=0)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='PortfolioControl',debug=False,bootloader_ignore_signals=False,strip=False,upx=False,console=False)
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,upx_exclude=[],name='PortfolioControl')
app=BUNDLE(coll,name='Portfolio Control.app',bundle_identifier='com.portfoliocontrol.desktop',icon=str(ROOT/'assets'/'portfolio_control.icns') if (ROOT/'assets'/'portfolio_control.icns').exists() else None,info_plist={
    'CFBundleName':'Portfolio Control','CFBundleDisplayName':'Portfolio Control','CFBundleShortVersionString':'3.6.0','CFBundleVersion':'3.6.0',
    'NSHighResolutionCapable':True,'LSMinimumSystemVersion':'11.0'
})
