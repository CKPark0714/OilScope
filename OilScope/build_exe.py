# -*- coding: utf-8 -*-
"""
build_exe.py

PyInstaller를 이용해 gui_main.py를 Windows용 실행 파일로 빌드하는 스크립트.

기본적으로 "폴더형(onedir)"으로 빌드한다: exe와 의존 파일들이 한 폴더 안에
그대로 들어있어, 압축해서 배포하면 받는 사람은 그냥 압축을 풀고 exe를 실행하면
된다(설치 프로그램 불필요). 데이터(원료 DB 등)는 이 실행 파일이 있는 폴더
바로 밑에 "OilScopeData" 폴더를 만들어 저장하므로, 프로그램 폴더를 통째로
옮기거나 USB로 복사해도 데이터가 그대로 따라온다(휴대용 배포 전제).

빌드가 끝나면 dist/<name>/ 폴더 전체를 dist/<name>-windows.zip 으로도
압축해 배포하기 좋은 형태로 함께 만들어 둔다.

사용법
------
    python build_exe.py

옵션
----
    --onedir / --onefile : 폴더형(기본) vs 단일 exe 파일 선택
                           (--onefile은 실행 시 임시 폴더에 매번 압축을 풀어
                            느리고, 데이터 폴더 위치도 덜 직관적이라 배포용
                            기본값으로는 권장하지 않는다.)
    --console            : 콘솔창 표시 (디버깅용, 기본은 --noconsole)
    --name NAME          : 결과 실행파일 이름 지정 (기본: OilScope)
    --no-zip             : 빌드 후 zip 압축을 생략한다.

내부적으로 PyInstaller를 하위 프로세스로 실행하며, matplotlib의 Qt 백엔드 및
PySide6 관련 hidden import/데이터 파일을 자동으로 포함하도록 옵션을 구성한다.

빌드 결과물은 ./dist/<name>/ (폴더형) 또는 ./dist/<name>.exe (단일파일)에
생성된다.
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
DEFAULT_APP_NAME = "OilScope"


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


def build(app_name: str, onefile: bool, console: bool, make_zip: bool) -> None:
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

        # pandas.read_excel()은 파일 확장자에 따라 openpyxl(.xlsx)/xlrd(.xls) 엔진을
        # importlib으로 "문자열 이름"만 보고 지연 임포트한다. 코드 어디에도 이 모듈들을
        # 직접 import하는 곳이 없어 PyInstaller의 정적 분석이 놓치기 쉽고, 그 결과
        # 빌드는 성공해도 실제로 엑셀 파일을 열 때만(원료 DB 엑셀 가져오기 등)
        # ModuleNotFoundError로 터지는 문제가 생길 수 있다.
        "--hidden-import=openpyxl",
        "--hidden-import=xlrd",

        # 프로젝트 내 다른 모듈들도 함께 패키징되도록 명시 (같은 폴더 내 존재)
        f"--paths={PROJECT_ROOT}",
    ]

    # 앱/작업표시줄 아이콘 (K-Petro 마스코트 배지)
    icon_file = os.path.join(PROJECT_ROOT, "assets", "icon.ico")
    if os.path.isfile(icon_file):
        cmd.append(f"--icon={icon_file}")

    # assets 폴더(아이콘, 로고 PNG)를 실행 파일 안에 데이터로 함께 번들링한다.
    # --onefile 빌드는 실행 시점에 이 데이터를 sys._MEIPASS 임시 폴더에 풀어놓으므로,
    # theme.resource_path()가 그 경로를 찾아 쓴다.
    assets_dir = os.path.join(PROJECT_ROOT, "assets")
    if os.path.isdir(assets_dir):
        cmd.append(f"--add-data={assets_dir}{os.pathsep}assets")

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
        zip_source_dir = dist_dir if onefile else os.path.join(dist_dir, app_name)
        zip_base = os.path.join(dist_dir, f"{app_name}-windows")
        if onefile:
            # onefile은 이미 파일 하나라 폴더 압축이 무의미 - exe만 담아 압축
            import zipfile
            zip_path = f"{zip_base}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(exe_path, arcname=os.path.basename(exe_path))
        else:
            zip_path = shutil.make_archive(zip_base, "zip", root_dir=dist_dir, base_dir=app_name)
        print(f"       배포용 압축파일: {zip_path}")
        print("       -> 이 zip을 그대로 배포하면, 받는 사람은 압축을 풀고")
        print(f"          {app_name}.exe를 실행하기만 하면 됩니다. 실행 시 그 폴더")
        print("          바로 밑에 OilScopeData 폴더가 자동 생성되어 데이터가 관리됩니다.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="gui_main.py를 Windows용 실행 파일로 빌드합니다 (PyInstaller 기반)."
    )
    parser.add_argument("--name", default=DEFAULT_APP_NAME, help="결과 실행파일 이름")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--onedir", action="store_true",
                             help="폴더 형태로 빌드 (기본값 - zip으로 배포, 실행 위치 옆에 데이터 폴더 생성)")
    mode_group.add_argument("--onefile", action="store_true",
                             help="단일 exe 파일로 빌드 (실행할 때마다 임시폴더에 압축을 풀어 느림, 배포 기본값 아님)")
    parser.add_argument("--console", action="store_true",
                         help="콘솔 창을 표시합니다 (디버깅용). 기본은 창 없는 GUI 모드입니다.")
    parser.add_argument("--no-clean", action="store_true",
                         help="이전 build/dist/spec 산출물을 정리하지 않고 빌드합니다.")
    parser.add_argument("--no-zip", action="store_true",
                         help="빌드 후 배포용 zip 압축을 생략합니다.")
    return parser.parse_args()


def main():
    args = parse_args()
    onefile = bool(args.onefile)  # 기본은 onedir, --onefile을 명시했을 때만 단일파일

    if not args.no_clean:
        clean_previous_build(args.name)

    build(app_name=args.name, onefile=onefile, console=args.console, make_zip=not args.no_zip)


if __name__ == "__main__":
    main()
