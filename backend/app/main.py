from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .sql_steps import UnsupportedQueryError, execute_steps, preview_source_tables

app = FastAPI(title="SQL可視化学習アプリ API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5174"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class QueryRequest(BaseModel):
    sql: str


class StepResponse(BaseModel):
    label: str
    description: str
    sql: str
    columns: list[str] | None = None
    rows: list[list] | None = None
    row_count: int | None = None
    error: str | None = None
    highlight_columns: list[str] = []


class QueryStepsResponse(BaseModel):
    steps: list[StepResponse]


class TablePreviewResponse(BaseModel):
    table: str
    alias: str | None = None
    columns: list[str] | None = None
    rows: list[list] | None = None
    row_count: int | None = None
    error: str | None = None


class SourceTablesResponse(BaseModel):
    tables: list[TablePreviewResponse]
    highlight_columns: list[str] = []


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/query/steps", response_model=QueryStepsResponse)
def query_steps(req: QueryRequest) -> QueryStepsResponse:
    try:
        steps = execute_steps(req.sql)
    except UnsupportedQueryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return QueryStepsResponse(steps=[StepResponse(**vars(s)) for s in steps])


@app.post("/api/query/source-tables", response_model=SourceTablesResponse)
def query_source_tables(req: QueryRequest) -> SourceTablesResponse:
    """SQLのFROM句が参照するテーブルの、実行前の生データを返す(ステップ0用)"""
    try:
        tables, highlight_columns = preview_source_tables(req.sql)
    except UnsupportedQueryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SourceTablesResponse(
        tables=[TablePreviewResponse(**vars(t)) for t in tables],
        highlight_columns=highlight_columns,
    )
