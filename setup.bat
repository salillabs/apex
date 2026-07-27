@echo off
echo Setting up APEX...

python -m venv .venv
call .venv\Scripts\activate.bat

pip install --quiet PyGithub slack_sdk sqlalchemy langgraph fastapi uvicorn pydantic requests psycopg2-binary pyyaml python-dotenv python-multipart

if not exist .env (
    copy .env.example .env
    echo Created .env — edit it with your credentials before starting.
)

if not exist projects.yaml (
    copy projects.yaml.example projects.yaml
    echo Created projects.yaml — edit it with your repos before starting.
)

echo Setup complete. Run start.bat to start APEX.
