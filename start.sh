#!/bin/bash
source .venv/Scripts/activate 2>/dev/null || source .venv/bin/activate
uvicorn main:app --reload
