#!/bin/bash
set -e

echo "Setting up APEX..."

python -m venv .venv
source .venv/Scripts/activate 2>/dev/null || source .venv/bin/activate

pip install --quiet PyGithub slack_sdk sqlalchemy langgraph fastapi uvicorn pydantic requests psycopg2-binary pyyaml python-dotenv python-multipart

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env — edit it with your credentials before starting."
fi

if [ ! -f projects.yaml ]; then
    cp projects.yaml.example projects.yaml
    echo "Created projects.yaml — edit it with your repos before starting."
fi

echo "Setup complete. Run ./start.sh to start APEX."
