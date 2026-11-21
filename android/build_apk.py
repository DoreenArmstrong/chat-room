#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构建聊天室 APK —— 不需要 Android Studio，也不需要 Gradle。

依赖（都在项目根目录的 .toolchain 里）：
    jdk17.zip          JDK 17（提供 javac / keytool 和 d8、apksigner 需要的 java）
    platform-34.zip    Android SDK platform（提供 android.jar）
    build-tools-34.zip Android SDK build-tools（aapt2 / d8 / zipalign / apksigner）

用法：
    .venv\\Scripts\\python.exe android\\build_apk.py

产物：
    android/build/chatroom.apk   （已用 debug key 签名，可直接安装）
"""

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # .../chat-room/android
ROOT = HERE.parent                              # .../chat-room
TOOLCHAIN = ROOT / ".toolchain"
BUILD = HERE / "build"
SRC = HERE / "app" / "src" / "main"
MANIFEST = SRC / "AndroidManifest.xml"
RES = SRC / "res"
JAVA_SRC = SRC / "java"

PACKAGE = "com.example.chatroom"
VERSION_CODE = "1"
VERSION_NAME = "1.0"


def log(msg):
    print("[build]", msg)


def run(cmd, env=None):
    cmd = [str(c) for c in cmd]
    log(" ".join(Path(c).name if i == 0 else c for i, c in enumerate(cmd[:6])) + " ...")
    subprocess.run(cmd, check=True, env=env)


def bat(tool):
    """Windows 上 .bat 工具（d8 / apksigner）要用 cmd /c 调"""
    tool = Path(tool)
    return ["cmd", "/c", str(tool)] if tool.suffix.lower() == ".bat" else [str(tool)]


def extract(zip_path, dest):
    if not zip_path.exists():
        raise SystemExit(f"缺少 {zip_path}，请先把它下载到 .toolchain 目录")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)


def ensure_toolchain():
    TOOLCHAIN.mkdir(exist_ok=True)
    if not any((TOOLCHAIN / "jdk").rglob("javac.exe")):
        log("解压 JDK …")
        extract(TOOLCHAIN / "jdk17.zip", TOOLCHAIN / "jdk")
    if not any((TOOLCHAIN / "platform").rglob("android.jar")):
        log("解压 Android platform …")
        extract(TOOLCHAIN / "platform-34.zip", TOOLCHAIN / "platform")
    if not any((TOOLCHAIN / "buildtools").rglob("aapt2.exe")):
        log("解压 build-tools …")
        extract(TOOLCHAIN / "build-tools-34.zip", TOOLCHAIN / "buildtools")


def find_one(base, pattern, what):
    hits = sorted(base.rglob(pattern))
    if not hits:
        raise SystemExit(f"在 {base} 里找不到 {what}")
    return hits[0]


def build():
    ensure_toolchain()

    javac = find_one(TOOLCHAIN / "jdk", "javac.exe", "javac")
    jdk_home = javac.parent.parent
    keytool = jdk_home / "bin" / "keytool.exe"
    android_jar = find_one(TOOLCHAIN / "platform", "android.jar", "android.jar")
    aapt2 = find_one(TOOLCHAIN / "buildtools", "aapt2.exe", "aapt2")
    zipalign = find_one(TOOLCHAIN / "buildtools", "zipalign.exe", "zipalign")
    d8 = find_one(TOOLCHAIN / "buildtools", "d8.bat", "d8")
    apksigner = find_one(TOOLCHAIN / "buildtools", "apksigner.bat", "apksigner")

    env = os.environ.copy()
    env["JAVA_HOME"] = str(jdk_home)
    env["PATH"] = str(jdk_home / "bin") + os.pathsep + env.get("PATH", "")

    if BUILD.exists():
        shutil.rmtree(BUILD)
    for d in ("gen", "classes", "dex"):
        (BUILD / d).mkdir(parents=True, exist_ok=True)

    # 1) 资源编译 + 链接（同时生成 R.java）
    manifest_text = MANIFEST.read_text(encoding="utf-8")
    if not re.search(r"<manifest[^>]*\bpackage\s*=", manifest_text):
        # 用 Android Studio 构建时包名由 namespace 提供；手动构建要补在 manifest 上
        manifest_text = re.sub(
            r"<manifest\b", f'<manifest package="{PACKAGE}"', manifest_text, count=1)
    built_manifest = BUILD / "AndroidManifest.xml"
    built_manifest.write_text(manifest_text, encoding="utf-8")

    compiled = BUILD / "compiled.zip"
    run([aapt2, "compile", "--dir", str(RES), "-o", str(compiled)], env=env)

    base_apk = BUILD / "base.apk"
    run([
        aapt2, "link", "-o", str(base_apk), "-I", str(android_jar),
        "--manifest", str(built_manifest), "--java", str(BUILD / "gen"),
        "--min-sdk-version", "24", "--target-sdk-version", "34",
        "--version-code", VERSION_CODE, "--version-name", VERSION_NAME,
        str(compiled),
    ], env=env)

    # 2) 编译 Java
    sources = list(JAVA_SRC.rglob("*.java")) + list((BUILD / "gen").rglob("*.java"))
    if not sources:
        raise SystemExit("没有找到 Java 源码")
    run([
        javac, "-encoding", "UTF-8", "-source", "11", "-target", "11",
        "-classpath", str(android_jar), "-d", str(BUILD / "classes"),
    ] + [str(s) for s in sources], env=env)

    # 3) 转成 dex
    classes = list((BUILD / "classes").rglob("*.class"))
    run(bat(d8) + [
        "--lib", str(android_jar), "--min-api", "24",
        "--output", str(BUILD / "dex"),
    ] + [str(c) for c in classes], env=env)

    # 4) 把 classes.dex 塞进 APK
    dex = BUILD / "dex" / "classes.dex"
    if not dex.exists():
        raise SystemExit("d8 没有生成 classes.dex")
    with zipfile.ZipFile(base_apk, "a", zipfile.ZIP_DEFLATED) as z:
        z.write(dex, "classes.dex")

    # 5) 对齐
    aligned = BUILD / "aligned.apk"
    run([zipalign, "-f", "4", str(base_apk), str(aligned)], env=env)

    # 6) 签名（第一次会自动生成 debug keystore）
    keystore = TOOLCHAIN / "debug.keystore"
    if not keystore.exists():
        log("生成 debug 签名证书 …")
        run([
            keytool, "-genkeypair", "-keystore", str(keystore),
            "-storepass", "android", "-keypass", "android",
            "-alias", "androiddebugkey", "-keyalg", "RSA", "-keysize", "2048",
            "-validity", "10000", "-dname", "CN=Android Debug,O=Android,C=US",
        ], env=env)

    apk = BUILD / "chatroom.apk"
    run(bat(apksigner) + [
        "sign", "--ks", str(keystore),
        "--ks-pass", "pass:android", "--key-pass", "pass:android",
        "--ks-key-alias", "androiddebugkey",
        "--out", str(apk), str(aligned),
    ], env=env)

    # 7) 自检
    print()
    run([aapt2, "dump", "badging", str(apk)], env=env)
    run(bat(apksigner) + ["verify", "--print-certs", str(apk)], env=env)

    size_kb = apk.stat().st_size / 1024
    print()
    print(f"构建完成：{apk}（{size_kb:.0f} KB）")
    print("把这个文件拷到安卓手机，点开安装即可（首次需要允许“安装未知来源应用”）。")


if __name__ == "__main__":
    try:
        build()
    except subprocess.CalledProcessError as exc:
        print(f"\n构建失败：命令返回 {exc.returncode}")
        sys.exit(1)
