GB28181 IPC 模拟工具 - 工具链与构建说明（tools/ 目录）

=====================================================================
1. pjproject-2.15.1 与 msys64 的关系
---------------------------------------------------------------------
- pjproject-2.15.1：
  国标广播功能开发时，在 pjsua_call.c 上追加了一处针对国标语音广播
  （UAC 出呼）的改动，属于已修改的本地源码，不能从上游直接拉取。
- msys64 只是编译 pjsip 时使用的 MSYS2 工具链，体积过大，不再纳入版本库。
  需要时按第 2、3 节自行获取，并放置到 tools/msys64/。

=====================================================================
2. 本项目使用的 msys64 版本
---------------------------------------------------------------------
- 发行版：MSYS2，目标环境为 UCRT64（不是 MINGW64 / CLANG64）。
- MSYS2 是滚动发行版，没有单一的版本号。以下为构建 pjsip 时实际用到
  的工具链指纹（来自原提交内 tools/msys64/var/lib/pacman/local）：

    gcc         16.2.0-3   (mingw-w64-ucrt-x86_64-gcc)
    binutils    2.47-3     (mingw-w64-ucrt-x86_64-binutils)
    make        4.4.1-3
    swig        4.5.1-1    (mingw-w64-ucrt-x86_64-swig)  —— 生成 pjsua2 Python 绑定必需
    zlib        1.3.2-2
    crt/headers 14.0.0.r409.g6de5d3b4d-1

  该组合大致对应 MSYS2 2026 年下半年的快照
  （参照包：windows-default-manifest 20260815、tzdata 2026d）。
- 为保证可复现，建议按第 3 节安装上述精确版本（pacman 支持版本锁定）。

=====================================================================
3. 如何获取 msys64
---------------------------------------------------------------------
a. 下载安装包
   官网 https://www.msys2.org/ 下载 msys2-x86_64-*.exe
   （或到 https://github.com/msys2/msys2-installer/releases 取对应版本）。

b. 安装到临时目录（如 C:\msys64），启动 “MSYS2 UCRT64” 终端。

c. 更新包数据库，并安装本项目所需的 UCRT64 工具链：

     pacman -Syu
     pacman -S mingw-w64-ucrt-x86_64-toolchain mingw-w64-ucrt-x86_64-swig make

   # 如需锁定到本项目使用的精确版本：
     pacman -S mingw-w64-ucrt-x86_64-gcc=16.2.0-3 \
                mingw-w64-ucrt-x86_64-binutils=2.47-3 \
                mingw-w64-ucrt-x86_64-swig=4.5.1-1 \
                make=4.4.1-3

   （MSYS 基础包 bash / make / coreutils 随 MSYS2 基础安装自带，
     UCRT64 的 gcc/g++/binutils 由上面的 toolchain 提供；build 脚本
     同时依赖这两者，请勿只装 ucrt64 工具链。）

d. 将安装目录下的 msys64 文件夹整体复制到本项目：

     <仓库根>/tools/msys64

   最终路径应为 tools/msys64/usr/bin/bash.exe 等。

e. 该目录已在 .gitignore 中忽略，不会再次被提交。

=====================================================================
4. 获取后如何编译 pjsip（生成 _pjsua2.pyd）
---------------------------------------------------------------------
前提：tools/pjproject-2.15.1/ 已随仓库提供且已完成 configure
      （顶层 Makefile、pjsip/build/Makefile、config_site.h 均已提交），
      无需再手动执行 ./configure。

最简方式（推荐）：在仓库根目录于 cmd 中运行
     build.bat
  它会：① 用 tools/msys64/usr/bin/bash.exe 调
          tools/build_param/build_pjsua_win.sh 编译并链接 _pjsua2.pyd；
        ② 用 PyInstaller 打包为 dist/GB28181_IPC模拟工具/。

仅重新编译 pjsip（不打包）的方式：
     tools\msys64\usr\bin\bash.exe --norc --noprofile ^
         tools\build_param\build_pjsua_win.sh
   # 仅重新链接 .pyd（需保留 .buildtmp/pjsua2_wrap.o）：追加 --link-only

build_pjsua_win.sh 内部步骤：
   1) cd tools/pjproject-2.15.1/pjsip/build && make
        —— 重编 libpjsua（pjsua2-test 因缺 speex/quartz 链接失败，与本目标无关，已忽略）
   2) g++ -c 编译 pjsip-apps/src/swig/python/pjsua2_wrap.cpp
   3) g++ -shared 链接出 _pjsua2.pyd，并部署到
        runtime/venv312/Lib/site-packages/pjsua2/_pjsua2.cp312-win_amd64.pyd

注意事项：
   - 必须用 tools/msys64/usr/bin/bash.exe --norc --noprofile，
     不要使用 Git Bash（其 PATH 会污染 MSYS 环境并报 pacman-key 错误）。
   - 脚本会自动把 ucrt64/bin 重新挂载到 /ucrt64/bin 并重置 PATH，
     无需手动设置环境变量。
   - TEMP 会被重定向到 .buildtmp，请确保该目录可写。

=====================================================================
5. build_param 目录
---------------------------------------------------------------------
为保证根目录简洁，本目录保存编译时使用的必要参数与脚本：
   build_pjsua_win.sh  —— pjsip 编译/链接脚本
   build_exe.spec      —— PyInstaller 打包规格
   copy_config.py      —— 打包时拷贝默认配置
   gen_icon.py / postbuild_smoke.py / build_smoke.spec —— 图标与冒烟测试
