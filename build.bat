@echo off
setlocal EnableDelayedExpansion

REM =====================================================================
REM  GB28181 IPC Simulator - Windows build ^& package script
REM  Location: build.bat   (project root; double-click or run in cmd)
REM  Stages:
REM    1) Compile pjsua2 native module (.pyd) with portable MSYS2
REM    2) Verify/install Python deps (PySide6 + PyInstaller)
REM    3) Package with PyInstaller into a directory build (onedir)
REM  Output: dist\GB28181_IPC模拟工具\GB28181_IPC模拟工具.exe
REM  NOTE: PROJ is derived from this script's own location (project
REM        root), so the whole project can be moved to any path.
REM =====================================================================

REM ---- project root (this script lives in project root) ----
set "PROJ=%~dp0"
if "%PROJ:~-1%"=="\" set "PROJ=%PROJ:~0,-1%"

set "VENV_PY=%PROJ%\runtime\venv312\Scripts\python.exe"
set "MSYS_BASH=%PROJ%\tools\msys64\usr\bin\bash.exe"
set "SH_BUILD=%PROJ%\tools\build_param\build_pjsua_win.sh"
set "SPEC=%PROJ%\tools\build_param\build_exe.spec"
set "OUTDIR=%PROJ%\dist\GB28181_IPC模拟工具"

REM ---- parse arguments ----
set "DO_BUILD=auto"
set "BUILD_ARG="
set "CONSOLE=0"
set "NO_PAUSE=0"
set "VERIFY=0"
set "FORCE_ONEFILE=0"

:parse
if "%~1"=="" goto :done_parse
if /i "%~1"=="--rebuild"    set "DO_BUILD=full" & shift & goto :parse
if /i "%~1"=="--relink"     set "DO_BUILD=link" & set "BUILD_ARG=--link-only" & shift & goto :parse
if /i "%~1"=="--skip-build" set "DO_BUILD=skip" & shift & goto :parse
if /i "%~1"=="--onefile"    set "FORCE_ONEFILE=1" & shift & goto :parse
if /i "%~1"=="--console"    set "CONSOLE=1" & shift & goto :parse
if /i "%~1"=="--verify"     set "VERIFY=1" & shift & goto :parse
if /i "%~1"=="--no-pause"   set "NO_PAUSE=1" & shift & goto :parse
if /i "%~1"=="--help"       goto :usage
echo [WARN] unknown arg ignored: %~1
shift
goto :parse
:done_parse

set "MODE_TXT=onedir (directory build)"
if "%FORCE_ONEFILE%"=="1" set "MODE_TXT=onefile (single executable)"

echo ============================================================
echo  GB28181 IPC Simulator - build ^& package
echo    Project : %PROJ%
echo    Package : %MODE_TXT%
echo    Console : %CONSOLE%
echo ============================================================

REM ---- base python (bundled, location-independent via %PROJ%) ----
set "BASE_PY=%PROJ%\runtime\python\python.exe"

REM =====================================================================
REM Self-heal: the committed venv (runtime\venv312) bakes the base-python
REM path from when it was first created. If the project was moved, the venv
REM python refuses to start ("No Python at '...'") and every `python -m pip`
REM call fails. Rewrite the stale absolute root in pyvenv.cfg / activate
REM scripts using the current %PROJ%. Uses the bundled BASE_PY, which is
REM independent of the (possibly broken) venv, so this stays portable.
REM =====================================================================
if exist "%PROJ%\runtime\venv312\pyvenv.cfg" (
  if exist "%BASE_PY%" (
    echo [fix] heal venv stale absolute paths, if any
    "%BASE_PY%" "%PROJ%\tools\build_param\heal_venv.py"
  ) else (
    echo [WARN] base python missing: %BASE_PY%
    echo         cannot auto-heal venv; ensure the bundled python is present.
  )
)

REM ---- preconditions ----
if not exist "%VENV_PY%" (
  echo [ERROR] venv interpreter not found: %VENV_PY%
  goto :fail
)
if not exist "%SPEC%" (
  echo [ERROR] spec not found: %SPEC%
  goto :fail
)

REM =====================================================================
REM Stage 1: compile pjsua2 (MinGW native module)
REM =====================================================================
set "PYD=%PROJ%\runtime\venv312\Lib\site-packages\pjsua2\_pjsua2.cp312-win_amd64.pyd"

if "%DO_BUILD%"=="skip" (
  echo [1/3] compile: skipped  [use --skip-build]
  goto :stage2
)

if "%DO_BUILD%"=="auto" (
  if exist "%PYD%" (
    echo [1/3] compile: pjsua2.pyd already deployed, skip  [use --rebuild to force]
    goto :stage2
  )
  echo [1/3] compile: pjsua2.pyd missing, full build...
  set "DO_BUILD=full"
)

if "%DO_BUILD%"=="link" (
  echo [1/3] compile: relink only  [use --relink]
) else (
  echo [1/3] compile: full rebuild of pjsua2  [use --rebuild]
)

if not exist "%MSYS_BASH%" (
  echo [ERROR] MSYS2 bash not found: %MSYS_BASH%
  echo         pjsua2 build needs portable MSYS2. Use --skip-build to skip.
  goto :fail
)
if not exist "%SH_BUILD%" (
  echo [ERROR] build script not found: %SH_BUILD%
  goto :fail
)

echo       -^> calling MSYS2: build_pjsua_win.sh %BUILD_ARG%
"%MSYS_BASH%" --norc --noprofile "%SH_BUILD%" %BUILD_ARG%
if errorlevel 1 (
  echo [ERROR] pjsua2 build failed, see output above.
  goto :fail
)
echo [1/3] compile: done

:stage2
REM =====================================================================
REM Stage 2: verify / install Python deps
REM =====================================================================
echo [2/3] deps: check PySide6 / PyInstaller / tomlkit ...
"%VENV_PY%" -c "import PySide6, PyInstaller, tomlkit" >nul 2>&1
if errorlevel 1 (
  echo       -^> missing deps, install via aliyun mirror, binary only ...
  "%VENV_PY%" -m pip install PySide6 PyInstaller tomlkit ^
    -i "https://mirrors.aliyun.com/pypi/simple/" ^
    --only-binary :all: --timeout 60 --retries 10
  if errorlevel 1 (
    echo [ERROR] dependency install failed.
    goto :fail
  )
) else (
  echo       -^> PySide6 + PyInstaller + tomlkit present, skip install
)
echo [2/3] deps: done

REM =====================================================================
REM Stage 3: PyInstaller package
REM =====================================================================
echo [3/3] package: PyInstaller ...
if "%FORCE_ONEFILE%"=="1" (set "MODE=onefile") else (set "MODE=onedir")
set "CONSOLE=%CONSOLE%"

REM remove old output first (rmdir /s /q deletes directly, no recycle bin)
if exist "%OUTDIR%" (
  echo       -^> removing old build: %OUTDIR%
  rmdir /s /q "%OUTDIR%" 2>nul
  if exist "%OUTDIR%" (
    timeout /t 2 >nul
    rmdir /s /q "%OUTDIR%" 2>nul
  )
)

"%VENV_PY%" -m PyInstaller "%SPEC%" --noconfirm ^
  --workpath "%PROJ%\.buildtmp\pywork" --distpath "%PROJ%\dist"
if errorlevel 1 (
  echo [ERROR] PyInstaller packaging failed.
  goto :fail
)
echo [3/3] package: done

REM =====================================================================
REM Stage 4: verify artifact
REM =====================================================================
set "EXE=%OUTDIR%\GB28181_IPC模拟工具.exe"
if not exist "%EXE%" (
  echo [ERROR] build finished but exe missing: %EXE%
  goto :fail
)

REM ---- 随包资源：必须落在 exe 同级（不在 _internal 内）----
REM 原因：PyInstaller 的 datas 目标一律落在 _internal 之下（已实测，
REM 目标名含 '.' 也不会提升到 exe 同级），故可替换资源改由本处统一拷贝。
REM 目的：用户可直接增删 PS 素材 / 查阅手册，且替换后无需重新打包。

REM (1) PS 流素材 -> exe 同级 resource\ps_samples\
set "PS_DST=%OUTDIR%\resource\ps_samples"
if not exist "%PS_DST%" mkdir "%PS_DST%" 2>nul
xcopy /Y /Q "%PROJ%\resource\ps_samples\*.ps" "%PS_DST%\" >nul 2>&1
REM 统计拷贝后的 .ps 文件数：
set "PS_N=0"
for /f "delims=" %%F in ('dir /b "%PS_DST%\*.ps" 2^>nul') do set /a "PS_N+=1"
if "%PS_N%"=="0" (
  echo [WARN] PS samples not copied; check %PROJ%\resource\ps_samples
) else (
  echo [4/4] ps samples : %PS_N% file^(s^) -^> resource\ps_samples\
)

REM (2) 用户手册 -> exe 同级 Readme.md（根目录，打包目录不再含 docs\）
copy /Y "%PROJ%\Readme.md" "%OUTDIR%\Readme.md" >nul 2>&1
if exist "%OUTDIR%\Readme.md" (
  echo [4/4] user manual: -^> Readme.md
) else (
  echo [WARN] user manual not copied; check %PROJ%\Readme.md
)

REM (3) 默认配置 -> exe 同级 config\（优先拷贝项目根已调好的 config；
REM     若项目根无 config 则退回物化一份默认值；首次运行 load_config 仍会兜底）
REM     用 Python 脚本拷贝/生成：避免 xcopy 在中文目标路径下静默失败。
"%VENV_PY%" "%PROJ%\tools\build_param\copy_config.py" "%PROJ%" "%OUTDIR%"
if exist "%OUTDIR%\config\app_config.toml" (
  echo [4/4] conf ok: config\app_config.toml present
) else (
  echo [WARN] config not materialized; check %PROJ%\app\core\config.py
)

for %%F in ("%EXE%") do set "EXESIZE=%%~zF"
echo ============================================================
echo  BUILD SUCCESS
echo    exe  : %EXE%
echo    size : %EXESIZE% bytes
echo    dir  : %OUTDIR%
echo          (copy the whole dir to deploy on another Windows PC)
echo    conf : %OUTDIR%\config\app_config.toml
echo    ps   : %OUTDIR%\resource\ps_samples\  (replace samples here)
echo    doc  : %OUTDIR%\Readme.md
echo ============================================================

if "%VERIFY%"=="1" (
  echo [verify] launch exe and auto-close in about 8s ...
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$p=Start-Process '%EXE%' -PassThru; Start-Sleep 8; $p.Refresh(); " ^
    "Write-Host ('window title: '+$p.MainWindowTitle); " ^
    "$p.CloseMainWindow() | Out-Null; Start-Sleep 4; $p.Refresh(); " ^
    "Write-Host ('clean exit: '+$p.HasExited); " ^
    "if(-not $p.HasExited){Stop-Process -Id $p.Id -Force; Write-Host 'FORCE_KILLED'}"
)

goto :done

:usage
echo Usage: build.bat [options]
echo   (no args)   auto: skip compile if pjsua2 deployed else full build; then package
echo   --rebuild     force full pjsua2 rebuild
echo   --relink      relink .pyd only (needs .buildtmp/pjsua2_wrap.o)
echo   --skip-build  skip pjsua2 compile (when .pyd already deployed)
echo   --onefile     build single executable (slow first launch, config in temp; not recommended)
echo   --console     build with a console window (easier troubleshooting)
echo   --verify      launch exe once after build for a smoke test
echo   --no-pause    do not pause at end (for CI / scripting)
goto :eof

:fail
echo.
echo [FAILED] build incomplete, see errors above.
if "%NO_PAUSE%"=="0" pause
exit /b 1

:done
REM =====================================================================
REM 清理：构建成功后移除 .buildtmp
REM   - 增量构建 --relink（DO_BUILD==link）不清理：relink 依赖
REM     .buildtmp/pjsua2_wrap.o，需保留供下次 relink 复用；
REM   - 其余路径（默认/--rebuild/--skip-build）编完即清理。
REM   注：本段仅在构建【成功】时到达，失败走 :fail 不会清理（便于排错）。
REM =====================================================================
if "%DO_BUILD%"=="link" (
  echo [cleanup] 增量构建 --relink 时保留 .buildtmp 供下次 relink 使用
  goto :done_pause
)

@REM echo [cleanup] 移除构建临时目录: %PROJ%\.buildtmp
if exist "%PROJ%\.buildtmp" (
  rmdir /s /q "%PROJ%\.buildtmp" 2>nul
  if exist "%PROJ%\.buildtmp" (
    timeout /t 2 >nul
    rmdir /s /q "%PROJ%\.buildtmp" 2>nul
  )
  if exist "%PROJ%\.buildtmp" (
    echo [WARN] 无法移除 .buildtmp，已保留（可手动清理）
  ) 
) 

:done_pause
pause
exit /b 0
