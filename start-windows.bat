@echo off
cd /d "%~dp0"
py -m dlna_emu -c config.yaml
pause