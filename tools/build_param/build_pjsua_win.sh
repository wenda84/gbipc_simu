#!/bin/bash
# 便携 MSYS2 构建 pjproject 2.15.1 (信令优先视频版) + SWIG Python 绑定
# 用法: 在 Git Bash 中执行  bash tools/build_param/build_pjsua_win.sh  [--link-only]
#
# 关键机制说明（踩坑记录，勿随意改动）:
#  1. 必须用 tools/msys64/usr/bin/bash.exe --norc --noprofile
#     （login shell 会触发 pacman-key 报错；Git Bash 的 PATH 会污染 msys 环境变量）
#  2. Git Bash 导出的 PATH 会被 msys bash 重置，因此在本脚本内重新挂载并设 PATH
#  3. / 之前被错误挂载到 PortableGit 目录，需 mount -f 把 ucrt64/bin 重新挂到 /ucrt64/bin
#  4. pjproject 的构建入口是 pjsip/build/Makefile（不是 pjsip/Makefile）
#  5. 链接 .pyd 必须用 Windows 风格路径；TEMP 必须重定向到可写目录
#  6. 链接需额外补 -lspeex / -lpjsdp / -lquartz（AMGetErrorTextA 在 libquartz.a）
#
# 工程根从本脚本自身位置推导（build_param -> tools -> 工程根），
# 因此整个工程目录可移动到任意路径。

set -u
PROJ="$(cd "$(dirname "$0")/../.." && pwd)"
PJR="$PROJ/tools/pjproject-2.15.1"
PY="$PROJ/runtime/python"
MSYS="$PROJ/tools/msys64"
SWIGDIR="$PJR/pjsip-apps/src/swig/python"
BUILDTMP="$PROJ/.buildtmp"
VENV_PKG="$PROJ/runtime/venv312/Lib/site-packages/pjsua2"

LINK_ONLY=0
[ "${1:-}" = "--link-only" ] && LINK_ONLY=1

mkdir -p "$BUILDTMP"

PROJ_ROOT="$PROJ" "$MSYS/usr/bin/bash.exe" --norc --noprofile -c '
set -e
PROJ="$PROJ_ROOT"
PJR="$PROJ/tools/pjproject-2.15.1"
PY="$PROJ/runtime/python"
BUILDTMP="$PROJ/.buildtmp"
LINK_ONLY='"$LINK_ONLY"'

mount -f "$PROJ/tools/msys64/ucrt64/bin" /ucrt64/bin 2>/dev/null || true
export PATH="/ucrt64/bin:/usr/bin:/bin"
export TEMP="$BUILDTMP" TMP="$BUILDTMP" TMPDIR="$BUILDTMP"

if [ "$LINK_ONLY" != "1" ]; then
  echo "=== [1/3] 重编译 libpjsua ==="
  cd "$PJR/pjsip/build"
  # pjsua2-test 链接会因 speex/quartz 缺库失败，与本目标无关，忽略其退出码
  make 2>&1 | grep -vE "^make\[|is up to date" | tail -20 || true
fi

echo "=== [2/3] 编译 pjsua2_wrap.cpp ==="
cd "$PJR/pjsip-apps/src/swig/python"
g++ -c -pipe -O2 -fPIC \
  -DPJ_AUTOCONF=1 -DPJ_IS_BIG_ENDIAN=0 -DPJ_IS_LITTLE_ENDIAN=1 \
  -DPJMEDIA_HAS_VIDEO=1 -DPJMEDIA_VIDEO_DEV_HAS_DSHOW=1 \
  -I"$PJR/pjlib/include" -I"$PJR/pjlib-util/include" -I"$PJR/pjnath/include" \
  -I"$PJR/pjmedia/include" -I"$PJR/pjsip/include" \
  -I"$PY/include" \
  -o "$BUILDTMP/pjsua2_wrap.o" \
  pjsua2_wrap.cpp

echo "=== [3/3] 链接 _pjsua2.pyd ==="
g++ -shared -fPIC -static-libgcc -static-libstdc++ \
  -o "$BUILDTMP/_pjsua2_new.pyd" \
  "$BUILDTMP/pjsua2_wrap.o" \
  -L"$PJR/pjlib/lib" -L"$PJR/pjlib-util/lib" -L"$PJR/pjnath/lib" \
  -L"$PJR/pjmedia/lib" -L"$PJR/pjsip/lib" -L"$PJR/third_party/lib" \
  -L"$PY/libs" \
  -Wl,--start-group \
    -lpjsua2-x86_64-w64-mingw32 \
    -lpjsua-x86_64-w64-mingw32 \
    -lpjsip-ua-x86_64-w64-mingw32 \
    -lpjsip-simple-x86_64-w64-mingw32 \
    -lpjsip-x86_64-w64-mingw32 \
    -lpjmedia-codec-x86_64-w64-mingw32 \
    -lpjmedia-videodev-x86_64-w64-mingw32 \
    -lpjmedia-audiodev-x86_64-w64-mingw32 \
    -lpjmedia-x86_64-w64-mingw32 \
    -lpjsdp-x86_64-w64-mingw32 \
    -lpjnath-x86_64-w64-mingw32 \
    -lpjlib-util-x86_64-w64-mingw32 \
    -lspeex-x86_64-w64-mingw32 \
    -lsrtp-x86_64-w64-mingw32 \
    -lresample-x86_64-w64-mingw32 \
    -lyuv-x86_64-w64-mingw32 \
    -lwebrtc-x86_64-w64-mingw32 \
    -lbaseclasses-x86_64-w64-mingw32 \
    -lpj-x86_64-w64-mingw32 \
  -Wl,--end-group \
  -lpython312 -lm -lwinmm -lole32 -lws2_32 -lwsock32 \
  -Wl,-Bstatic -lpthread -Wl,-Bdynamic \
  -lmingwex -loleaut32 -lrpcrt4 -luuid -lstrmiids -lquartz

ls -la "$BUILDTMP/_pjsua2_new.pyd"
echo "BUILD_OK"
'

rc=$?
if [ $rc -eq 0 ]; then
  echo "=== 部署到 venv312 ==="
  cp "$BUILDTMP/_pjsua2_new.pyd" "$VENV_PKG/_pjsua2.cp312-win_amd64.pyd"
  ls -la "$VENV_PKG/_pjsua2.cp312-win_amd64.pyd"
  echo "DEPLOY_OK"
else
  echo "BUILD_FAILED rc=$rc"
fi
exit $rc
