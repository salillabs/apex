@echo off
call .venv\Scripts\activate.bat
uvicorn main:app --reload
