# -*- coding: utf-8 -*-
"""
build_exe.py

PyInstaller를 이용해 gui_main.py를 Windows 독립 실행형 단일 .exe 파일로 빌드하는 스크립트.

사용법
------
    python build_exe.py

옵션
----
    --onefile / --onedir : 단일 exe(기본) vs 폴더 배포 방식 선택
    --console            : 콘솔창 표시 (디버깅용, 기본은 --noconsole)
    --name NAME          : 결과 실행파일 이름 지정 (기본: FakeOilAnalyzer)

내부적으로 PyInstaller를 하위 프로세스로 실행하며, matplotlib의 Qt 백엔드 및
PySide6 관련 hidden import/데이터 파일을 자동으로 포함하도록 옵션을 구성한다.

빌드 결과물은 ./dist/<name>(.exe) 에 생성된다.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

# Windows 콘솔의 기본 코드페이지(cp1252 등)는 한글을 인코딩하지 못해, 아래
# print()의 한글 메시지가 UnicodeEncodeError로 스크립트를 죽일 수 있다.
# (영어 로캘의 Windows에서 특히 잘 발생한다.) stdout/stderr를 UTF-8로
# 강제 재설정해 어떤 시스템 로캘에서도 안전하게 출력되도록 한다.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
ENTRY_SCRIPT = os.path.join(PROJECT_ROOT, "gui_main.py")
DEFAULT_APP_NAME = "FakeOilAnalyzer"


def check_pyinstaller() -> None:
    """PyInstaller 설치 여부 확인, 없으면 설치 안내."""
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[오류] PyInstaller가 설치되어 있지 않습니다.")
        print("       다음 명령으로 설치 후 다시 실행하세요: pip install pyinstaller")
        sys.exit(1)


def clean_previous_build(app_name: str) -> None:
    """이전 빌드 산출물(build/, dist/, *.spec) 정리."""
    for folder in ("build", "dist"):
        path = os.path.join(PROJECT_ROOT, folder)
        if os.path.isdir(path):
            print(f"[정리] 기존 폴더 삭제: {path}")
            shutil.rmtree(path, ignore_errors=True)

    spec_path = os.path.join(PROJECT_ROOT, f"{app_name}.spec")
    if os.path.isfile(spec_path):
        print(f"[정리] 기존 spec 파일 삭제: {spec_path}")
        os.remove(spec_path)


def build(app_name: str, onefile: bool, console: bool) -> None:
    """PyInstaller 명령을 구성하여 빌드를 실행한다."""
    check_pyinstaller()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        ENTRY_SCRIPT,
        f"--name={app_name}",
        "--noconfirm",
        "--clean",
        "--onefile" if onefile else "--onedir",
        "--console" if console else "--noconsole",

        # PySide6 / matplotlib Qt 백엔드 관련 hidden import 및 데이터 수집
        "--hidden-import=PySide6.QtSvg",
        "--hidden-import=PySide6.QtPrintSupport",
        "--hidden-import=matplotlib.backends.backend_qtagg",
        "--collect-submodules=matplotlib.backends",
        "--collect-data=matplotlib",

        # scipy / sklearn / pandas 등은 동적 임포트가 많아 누락 방지를 위해 전체 수집
        "--collect-submodules=scipy",
        "--collect-submodules=sklearn",
        "--collect-submodules=pandas",

        # 프로젝트 내 다른 모듈들도 함께 패키징되도록 명시 (같은 폴더 내 존재)
        f"--paths={PROJECT_ROOT}",
    ]

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="gui_main.py를 Windows 독립 실행형 exe로 빌드합니다 (PyInstaller 기반)."
    )
    parser.add_argument("--name", default=DEFAULT_APP_NAME, help="결과 실행파일 이름")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--onefile", action="store_true", default=True,
                             help="단일 exe 파일로 빌드 (기본값)")
    mode_group.add_argument("--onedir", action="store_true",
                             help="폴더 형태(onedir)로 빌드 (실행 속도가 더 빠름)")
    parser.add_argument("--console", action="store_true",
                         help="콘솔 창을 표시합니다 (디버깅용). 기본은 창 없는 GUI 모드입니다.")
    parser.add_argument("--no-clean", action="store_true",
                         help="이전 build/dist/spec 산출물을 정리하지 않고 빌드합니다.")
    return parser.parse_args()


def main():
    args = parse_args()
    onefile = not args.onedir  # --onedir 지정 시에만 폴더 모드

    if not args.no_clean:
        clean_previous_build(args.name)

    build(app_name=args.name, onefile=onefile, console=args.console)


if __name__ == "__main__":
    main()
