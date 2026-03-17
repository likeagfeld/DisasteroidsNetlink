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
)

ECHO.
PAUSE
