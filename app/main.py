import json
import os
import glob as glob_module
import boto3
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from openai import AzureOpenAI

app = FastAPI()
templates = Jinja2Templates(directory="templates")

SECRET_NAME = "Azure_OpneAI_Hituji"
DEPT_SKILL_PATH = os.path.join(os.path.dirname(__file__), "dept-skill")
SKILLS_BASE = os.path.join(DEPT_SKILL_PATH, ".claude", "skills")
TEAM_DATA_PATH = os.path.join(DEPT_SKILL_PATH, "team_data")

_azure_client: AzureOpenAI | None = None
_deployment_name: str = ""


def get_azure_client() -> tuple[AzureOpenAI, str]:
    global _azure_client, _deployment_name
    if _azure_client is not None:
        return _azure_client, _deployment_name

    sm = boto3.client("secretsmanager")
    response = sm.get_secret_value(SecretId=SECRET_NAME)
    secret = json.loads(response["SecretString"])

    _deployment_name = secret["DEPLOYMENT_NAME"]
    _azure_client = AzureOpenAI(
        api_key=secret["API_KEY"],
        azure_endpoint=secret["END_POINT"],
        api_version="2024-02-01",
    )
    return _azure_client, _deployment_name


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    meta: dict = {}
    body = content
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            for line in content[3:end].splitlines():
                if ":" in line and not line.startswith(" ") and not line.startswith("-"):
                    key, _, val = line.partition(":")
                    meta[key.strip()] = val.strip()
            body = content[end + 3:].lstrip("\n")
    return meta, body


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def list_skills() -> list[dict]:
    skills = []
    category_labels = {"dept-manager": "マネージャー", "workflows": "ワークフロー"}
    for category in ["dept-manager", "workflows"]:
        cat_path = os.path.join(SKILLS_BASE, category)
        if not os.path.isdir(cat_path):
            continue
        for skill_dir in sorted(os.listdir(cat_path)):
            skill_file = os.path.join(cat_path, skill_dir, "SKILL.md")
            if not os.path.isfile(skill_file):
                continue
            meta, _ = _parse_frontmatter(_read_file(skill_file))
            skills.append({
                "id": skill_dir,
                "name": meta.get("name", skill_dir),
                "description": meta.get("description", ""),
                "category": category,
                "categoryLabel": category_labels.get(category, category),
            })
    return skills


def _load_skill_md(skill_id: str) -> str:
    pattern = os.path.join(SKILLS_BASE, "**", skill_id, "SKILL.md")
    matches = glob_module.glob(pattern, recursive=True)
    return _read_file(matches[0]) if matches else ""


def _load_shared_skills() -> str:
    shared_path = os.path.join(SKILLS_BASE, "shared")
    parts = []
    if os.path.isdir(shared_path):
        for skill_dir in sorted(os.listdir(shared_path)):
            skill_file = os.path.join(shared_path, skill_dir, "SKILL.md")
            if os.path.isfile(skill_file):
                parts.append(_read_file(skill_file))
    return "\n\n---\n\n".join(parts)


def _load_team_data() -> str:
    if not os.path.isdir(TEAM_DATA_PATH):
        return ""
    parts = []
    for member in sorted(os.listdir(TEAM_DATA_PATH)):
        member_path = os.path.join(TEAM_DATA_PATH, member)
        if not os.path.isdir(member_path):
            continue
        blocks = [f"## チームメンバー: {member}さん"]
        for fname in ["monthly-schedule.md", "career-goals.md", "term-job-plan.md"]:
            fpath = os.path.join(member_path, fname)
            if os.path.isfile(fpath):
                blocks.append(f"### {fname}\n{_read_file(fpath)}")
        parts.append("\n\n".join(blocks))
    if not parts:
        return ""
    return "# チームデータ\n\n" + "\n\n---\n\n".join(parts)


def build_system_prompt(skill_id: str) -> str:
    parts = []
    skill_content = _load_skill_md(skill_id)
    if skill_content:
        parts.append(skill_content)
    shared = _load_shared_skills()
    if shared:
        parts.append("# 共通スタイルガイド\n\n" + shared)
    team_data = _load_team_data()
    if team_data:
        parts.append(team_data)
    return "\n\n---\n\n".join(parts)


class ChatRequest(BaseModel):
    messages: list[dict]
    skill: str = "manager-orchestrator"


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/skills")
async def get_skills():
    return JSONResponse(list_skills())


@app.post("/api/chat")
async def chat(req: ChatRequest):
    client, deployment = get_azure_client()
    system_prompt = build_system_prompt(req.skill)
    messages = [{"role": "system", "content": system_prompt}] + req.messages

    def generate():
        stream = client.chat.completions.create(
            model=deployment,
            messages=messages,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    return StreamingResponse(generate(), media_type="text/plain")
