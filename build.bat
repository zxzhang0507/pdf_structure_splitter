@echo off
chcp 65001 >nul
setlocal

echo ============================================
echo  pdf_structure_splitter GUI - 一键打包
echo ============================================
echo.

cd /d "%~dp0"

echo [1/4] 检查 Python ...
python --version
if errorlevel 1 (
    echo [!] 找不到 python，请先安装 Python 3.11 并加入 PATH
    pause
    exit /b 1
)

echo.
echo [2/4] 安装依赖 ...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [!] 依赖安装失败
    pause
    exit /b 1
)

echo.
echo [3/4] 确保图标存在 ...
if not exist "resources\app.ico" (
    echo     生成程序图标 ...
    python tests\make_icon.py
)

echo.
echo [4/4] 打包（PyInstaller）...
python -m PyInstaller --noconfirm --clean build.spec
if errorlevel 1 (
    echo [!] 打包失败
    pause
    exit /b 1
)

echo.
echo ============================================
echo  完成: dist\pdf_structure_splitter.exe
echo ============================================
for %%F in ("dist\pdf_structure_splitter.exe") do echo  体积: %%~zF 字节
echo.
echo 验证方法：把 dist\pdf_structure_splitter.exe 复制到一台**未装 Python** 的
echo Windows 11 x64 机器，双击运行，打开一个 PDF 并导出即可。
echo.

pause
endlocal
