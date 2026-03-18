@ECHO Off
SET COMPILER_DIR=D:\joengine-master\Compiler
SET JO_ENGINE_SRC_DIR=D:/joengine-master/jo_engine
SET PATH=%COMPILER_DIR%\WINDOWS\Other Utilities;%COMPILER_DIR%\WINDOWS\bin;%PATH%

ECHO.
ECHO === Disasteroids Build ===
ECHO.

make re JO_ENGINE_SRC_DIR=%JO_ENGINE_SRC_DIR% COMPILER_DIR=D:/joengine-master/Compiler OS=Windows_NT
IF %ERRORLEVEL% NEQ 0 (
    ECHO.
    ECHO === Build failed at mkisofs ISO step ===
    ECHO === Checking if ELF/BIN compiled OK... ===
    IF EXIST game.elf (
        IF EXIST cd\0.bin (
            ECHO === Compile+Link OK! Creating ISO manually... ===
            mkisofs -quiet -sysid "SEGA SATURN" -volid "SaturnApp" -volset "SaturnApp" -sectype 2352 -publisher "SEGA ENTERPRISES, LTD." -preparer "SEGA ENTERPRISES, LTD." -appid "SaturnApp" -abstract "ABS.TXT" -copyright "CPY.TXT" -biblio "BIB.TXT" -generic-boot %COMPILER_DIR%\COMMON\IP.BIN -full-iso9660-filenames -o game.iso cd
            IF EXIST game.iso (
                ECHO === ISO created successfully! ===
                JoEngineCueMaker.exe
                CALL :PACKAGE_GAME
            ) ELSE (
                ECHO === ISO creation failed. game.elf and cd\0.bin are ready for manual ISO creation. ===
            )
        ) ELSE (
            ECHO === Compilation failed! ===
        )
    ) ELSE (
        ECHO === Compilation failed! ===
    )
) ELSE (
    ECHO.
    ECHO === Build successful! ===
    REM Regenerate CUE file to ensure audio tracks are included
    IF EXIST game.iso (
        JoEngineCueMaker.exe
        ECHO === CUE file generated with audio tracks ===
        CALL :PACKAGE_GAME
    )
)

ECHO.
PAUSE
GOTO :EOF

:PACKAGE_GAME
ECHO.
ECHO === Packaging Game Files ===
IF EXIST "Game Files" RMDIR /S /Q "Game Files"
MKDIR "Game Files"
MKDIR "Game Files\Online"
MKDIR "Game Files\Online\DreamPi"
MKDIR "Game Files\Online\PC Bridge"

COPY /Y game.cue "Game Files\game.cue" >NUL
COPY /Y game.iso "Game Files\game.iso" >NUL
COPY /Y 02_TITLE.WAV "Game Files\02_TITLE.WAV" >NUL
COPY /Y 03_PAUSE.WAV "Game Files\03_PAUSE.WAV" >NUL
COPY /Y 04_VICTORY.WAV "Game Files\04_VICTORY.WAV" >NUL
COPY /Y 05_GAMEOVER.WAV "Game Files\05_GAMEOVER.WAV" >NUL

COPY /Y tools\dreampi\netlink.py "Game Files\Online\DreamPi\netlink.py" >NUL
COPY /Y tools\dreampi\config.ini "Game Files\Online\DreamPi\config.ini" >NUL

COPY /Y tools\netlink_bridge\bridge.py "Game Files\Online\PC Bridge\bridge.py" >NUL
COPY /Y tools\netlink_bridge\start_bridge.bat "Game Files\Online\PC Bridge\start_bridge.bat" >NUL

COPY /Y README_ONLINE.txt "Game Files\Online\README.TXT" >NUL

ECHO === Game Files folder ready! ===
GOTO :EOF
