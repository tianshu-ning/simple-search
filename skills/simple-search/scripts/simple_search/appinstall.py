"""编译并安装 simple-search 的 macOS 原生窗口。"""
import os
import re
import shutil
import subprocess
import time

from .common import HOME, IS_MAC, SKILL_DIR, VERSION, data_dir, eprint, which

APP = os.path.join(HOME, "Applications", "simple-search.app")
BUNDLE_ID = "local.simple.search.desktop"

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>simple-search</string>
  <key>CFBundleDisplayName</key><string>simple-search</string>
  <key>CFBundleIdentifier</key><string>%s</string>
  <key>CFBundleExecutable</key><string>SimpleSearch</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>%s</string>
  <key>CFBundleVersion</key><string>%s</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
"""


def _swiftc():
    for p in (which("swiftc"), "/usr/bin/swiftc"):
        if p and os.path.exists(p):
            return p
    return None


def _swift_sdk_args():
    sdk = os.environ.get("SDKROOT")
    if not sdk:
        try:
            sdk = subprocess.check_output(
                ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
                text=True, stderr=subprocess.DEVNULL, timeout=10,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            return []
    if not os.path.isdir(sdk):
        return []
    args = ["-sdk", sdk]
    # 按实际参与编译的 SDK 判断，旧 SDK 无需解析新 API。
    glass_header = os.path.join(sdk, "System/Library/Frameworks/AppKit.framework/Headers/NSGlassEffectView.h")
    if os.path.isfile(glass_header):
        args.extend(["-D", "SIMPLE_SEARCH_LIQUID_GLASS"])
    return args


def install(open_after=True):
    if not IS_MAC:
        raise RuntimeError("原生窗口只支持 macOS。")
    swiftc = _swiftc()
    if not swiftc:
        raise RuntimeError("找不到 Swift 编译器。请先运行 xcode-select --install 安装命令行工具，再执行 simple-search app install。")
    src = os.path.join(SKILL_DIR, "scripts", "helper", "SimpleSearchApp.swift")
    tmp = APP + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.join(tmp, "Contents", "MacOS"))
    os.makedirs(os.path.join(tmp, "Contents", "Resources"))
    eprint("正在编译 simple-search 窗口（约 30~60 秒，仅此一次）…")
    t0 = time.time()
    binary = os.path.join(tmp, "Contents", "MacOS", "SimpleSearch")
    target = os.uname().machine + "-apple-macos11"
    args = [swiftc, "-O", "-target", target, *_swift_sdk_args(), "-o", binary, src]
    r = subprocess.run(args, capture_output=True, text=True, timeout=1200)
    log = os.path.join(data_dir(), "app-build.log")
    with open(log, "w", encoding="utf-8") as f:
        f.write(r.stdout + "\n" + r.stderr)
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("编译失败，详情见 " + log + "\n" + r.stderr[-1500:])
    with open(os.path.join(tmp, "Contents", "Info.plist"), "w", encoding="utf-8") as f:
        f.write(PLIST % (BUNDLE_ID, VERSION, str(int(time.time()))))
    for name in ("AppIcon.icns", "StatusIcon.png"):
        shutil.copy(os.path.join(SKILL_DIR, "data", name), os.path.join(tmp, "Contents", "Resources", name))
    subprocess.run(["codesign", "--force", "--sign", "-", tmp], capture_output=True)
    _stop_running()
    shutil.rmtree(APP, ignore_errors=True)
    os.replace(tmp, APP)
    subprocess.run(["touch", APP])
    lsreg = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    if os.path.exists(lsreg):
        subprocess.run([lsreg, "-f", APP], capture_output=True)
    if open_after:
        subprocess.run(["open", APP])
    return {"seconds": round(time.time() - t0, 1), "app": APP}


def _stop_running():
    """退出同一安装路径下的运行实例，以便替换应用。"""
    executable = re.escape(os.path.join(APP, "Contents", "MacOS", "SimpleSearch"))
    r = subprocess.run(["pgrep", "-f", executable], capture_output=True, text=True)
    mine = {os.getpid(), os.getppid()}
    for pid in (int(x) for x in r.stdout.split() if x.isdigit()):
        if pid not in mine:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    time.sleep(0.4)


def uninstall():
    _stop_running()
    shutil.rmtree(APP, ignore_errors=True)
    agent = os.path.join(HOME, "Library", "LaunchAgents", BUNDLE_ID + ".plist")
    if os.path.exists(agent):
        os.unlink(agent)
