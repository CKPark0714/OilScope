# -*- coding: utf-8 -*-
"""
build_exe.py

PyInstaller를 이용해 main.py를 Windows용 실행 파일로 빌드하는 스크립트.
OilScope/build_exe.py와 동일한 방식(폴더형/onedir)으로 만든다: exe와 의존
파일들이 한 폴더 안에 그대로 들어있어, 압축해서 배포하면 받는 사람은 그냥
압축을 풀고 exe를 실행하면 된다(설치 프로그램 불필요).

사용법
------
    python build_exe.py
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
ENTRY_SCRIPT = os.path.join(PROJECT_ROOT, "main.py")
DEFAULT_APP_NAME = "GCConverter"


def check_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[오류] PyInstaller가 설치되어 있지 않습니다.")
        print("       다음 명령으로 설치 후 다시 실행하세요: pip install pyinstaller")
        sys.exit(1)


def clean_previous_build(app_name: str) -> None:
    for folder in ("build", "dist"):
        path = os.path.join(PROJECT_ROOT, folder)
        if os.path.isdir(path):
            print(f"[정리] 기존 폴더 삭제: {path}")
            shutil.rmtree(path, ignore_errors=True)

    spec_path = os.path.join(PROJECT_ROOT, f"{app_name}.spec")
    if os.path.isfile(spec_path):
        print(f"[정리] 기존 spec 파일 삭제: {spec_path}")
        os.remove(spec_path)


def build(app_name: str, onefile: bool, console: bool, make_zip: bool) -> None:
    check_pyinstaller()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        ENTRY_SCRIPT,
        f"--name={app_name}",
        "--noconfirm",
        "--clean",
        "--onefile" if onefile else "--onedir",
        "--console" if console else "--noconsole",

        # rainbow-api는 lxml(C 확장)을 쓰는데, 동적 임포트 경로가 있어 정적
        # 분석이 놓칠 수 있다.
        "--hidden-import=lxml.etree",
        "--hidden-import=lxml._elementpath",

        f"--paths={PROJECT_ROOT}",
    ]

    icon_file = os.path.join(PROJECT_ROOT, "assets", "icon.ico")
    if os.path.isfile(icon_file):
        cmd.append(f"--icon={icon_file}")

    print("[빌드 시작] 실행 명령:")
    print("  " + " ".join(cmd))

    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        print(f"[실패] PyInstaller 빌드가 실패했습니다 (exit code={result.returncode}).")
        sys.exit(result.returncode)

    dist_dir = os.path.join(PROJECT_ROOT, "dist")
    if onefile:
        exe_path = os.path.join(dist_dir, f"{app_name}.exe")
    else:
        exe_path = os.path.join(dist_dir, app_name, f"{app_name}.exe")

    print("\n[성공] 빌드가 완료되었습니다.")
    print(f"       실행파일 경로: {exe_path}")

    if make_zip:
        zip_base = os.path.join(dist_dir, f"{app_name}-windows")
        if onefile:
            import zipfile
            zip_path = f"{zip_base}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(exe_path, arcname=os.path.basename(exe_path))
        else:
            zip_path = shutil.make_archive(zip_base, "zip", root_dir=dist_dir, base_dir=app_name)
        print(f"       배포용 압축파일: {zip_path}")
        print("       -> 이 zip을 그대로 배포하면, 받는 사람은 압축을 풀고")
        print(f"          {app_name}.exe를 실행하기만 하면 됩니다.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="main.py를 Windows용 실행 파일로 빌드합니다 (PyInstaller 기반)."
    )
    parser.add_argument("--name", default=DEFAULT_APP_NAME, help="결과 실행파일 이름")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--onedir", action="store_true", help="폴더 형태로 빌드 (기본값)")
    mode_group.add_argument("--onefile", action="store_true", help="단일 exe 파일로 빌드")
    parser.add_argument("--console", action="store_true", help="콘솔 창을 표시합니다 (디버깅용).")
    parser.add_argument("--no-clean", action="store_true", help="이전 빌드 산출물을 정리하지 않습니다.")
    parser.add_argument("--no-zip", action="store_true", help="빌드 후 zip 압축을 생략합니다.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    onefile = bool(args.onefile)

    if not args.no_clean:
        clean_previous_build(args.name)

    build(app_name=args.name, onefile=onefile, console=args.console, make_zip=not args.no_zip)


if __name__ == "__main__":
    main()
