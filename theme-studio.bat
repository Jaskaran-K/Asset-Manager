@echo off
rem The Theme Studio: make your own themes and backgrounds (see docs\theme-studio.md)
call .venv\Scripts\activate
python main.py --theme-studio %*
