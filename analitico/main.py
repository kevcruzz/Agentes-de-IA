"""
AGENTE 2 — ANALÍTICO DE DADOS
================================
Agente que responde perguntas sobre dados em linguagem natural:
- Recebe pergunta em português: "Quais os 5 produtos mais vendidos em março?"
- Traduz para SQL e executa na base de dados
- Retorna resposta em linguagem natural + dados brutos
- Gera análises e insights automaticamente
- Suporta CSV, SQLite e pode ser adaptado para PostgreSQL/MySQL

Rota principal: POST /perguntar
Rota de upload: POST /upload-csv
Rota de schema: GET /schema
"""

import json
import io
import uuid
import pandas as pd
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

import anthropic
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, text, inspect, Column, String, Float, Integer, DateTime
from sqlalchemy.orm import DeclarativeBase, Session

import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.utils import config, StructuredLogger, retry, Timer, success_response, error_response

# ─── BANCO DE DADOS ──────────────────────────────────────────────────────────

engine = create_engine("sqlite:///./analitico.db", connect_args={"check_same_thread": False})

class Base(DeclarativeBase):
    pass

class QueryLog(Base):
    __tablename__ = "query_log"
    id          = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    pergunta    = Column(String)
    sql_gerado  = Column(String)
    sucesso     = Column(Integer, default=1)
    timestamp   = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

# ─── DADOS DE DEMONSTRAÇÃO ───────────────────────────────────────────────────

def seed_demo_data():
    """Cria tabelas de demonstração com dados realistas."""
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS vendas (
                id INTEGER PRIMARY KEY,
                data TEXT,
                produto TEXT,
                categoria TEXT,
                vendedor TEXT,
                regiao TEXT,
                quantidade INTEGER,
                valor_unitario REAL,
                valor_total REAL,
                cliente TEXT
            )
        """))

        count = conn.execute(text("SELECT COUNT(*) FROM vendas")).scalar()
        if count == 0:
            import random
            produtos = [
                ("Software CRM Pro", "Software", 1200.0),
                ("Licença ERP Anual", "Software", 4500.0),
                ("Consultoria IA", "Serviço", 3000.0),
                ("Suporte Premium", "Serviço", 800.0),
                ("Hardware Server", "Hardware", 8500.0),
                ("Notebook Dell", "Hardware", 4200.0),
                ("Cloud Storage 1TB", "Cloud", 350.0),
                ("Backup Automático", "Cloud", 280.0),
            ]
            vendedores  = ["Ana Silva", "Carlos Mendes", "Beatriz Lima", "Diego Costa"]
            regioes     = ["SP", "RJ", "MG", "RS", "PR"]
            clientes    = ["Empresa A", "Empresa B", "Empresa C", "Empresa D", "Empresa E",
                           "Empresa F", "Empresa G", "Empresa H"]
            meses       = ["2024-01", "2024-02", "2024-03", "2024-04", "2024-05", "2024-06",
                           "2024-07", "2024-08", "2024-09", "2024-10", "2024-11", "2024-12"]

            rows = []
            for mes in meses:
                for _ in range(random.randint(15, 30)):
                    p_nome, p_cat, p_val = random.choice(produtos)
                    qtd   = random.randint(1, 5)
                    total = round(qtd * p_val * random.uniform(0.9, 1.1), 2)
                    dia   = random.randint(1, 28)
                    rows.append((
                        f"{mes}-{dia:02d}", p_nome, p_cat,
                        random.choice(vendedores), random.choice(regioes),
                        qtd, p_val, total, random.choice(clientes)
                    ))

            conn.executemany(
                "INSERT INTO vendas (data,produto,categoria,vendedor,regiao,quantidade,valor_unitario,valor_total,cliente) VALUES (?,?,?,?,?,?,?,?,?)",
                rows
            )
            conn.commit()

seed_demo_data()

# ─── FERRAMENTAS DO AGENTE ───────────────────────────────────────────────────

TOOLS = [
    {
        "name": "obter_schema",
        "description": "Obtém o schema das tabelas disponíveis no banco de dados. Use SEMPRE como primeiro passo antes de gerar SQL.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "executar_sql",
        "description": "Executa uma query SQL SELECT no banco de dados e retorna os resultados. Use apenas SELECT — nunca INSERT, UPDATE, DELETE ou DROP.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "Query SQL SELECT válida para SQLite. Limite sempre com LIMIT 100."
                }
            },
            "required": ["sql"]
        }
    },
    {
        "name": "calcular_insight",
        "description": "Calcula estatísticas e métricas a partir de dados já consultados (média, crescimento, top N, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "dados_json": {"type": "string", "description": "Dados em JSON para calcular"},
                "calculo": {"type": "string", "description": "Tipo de cálculo: media, crescimento_percentual, top_n, comparacao"}
            },
            "required": ["dados_json", "calculo"]
        }
    }
]

def executar_ferramenta(nome: str, params: dict) -> str:
    logger = StructuredLogger("analitico")

    if nome == "obter_schema":
        inspector = inspect(engine)
        schema = {}
        for table_name in inspector.get_table_names():
            cols = inspector.get_columns(table_name)
            schema[table_name] = [{"nome": c["name"], "tipo": str(c["type"])} for c in cols]

            # Amostra de valores únicos para colunas de texto
            with engine.connect() as conn:
                for col in cols[:3]:
                    try:
                        result = conn.execute(text(f'SELECT DISTINCT "{col["name"]}" FROM {table_name} LIMIT 5'))
                        sample = [str(r[0]) for r in result if r[0] is not None]
                        next(c for c in schema[table_name] if c["nome"] == col["name"])["exemplos"] = sample
                    except Exception:
                        pass

        logger.info("schema_fetched", tables=list(schema.keys()))
        return json.dumps({"schema": schema}, ensure_ascii=False)

    elif nome == "executar_sql":
        sql = params.get("sql", "").strip()

        # Proteção: só permite SELECT
        sql_upper = sql.upper().lstrip()
        if not sql_upper.startswith("SELECT"):
            return json.dumps({"erro": "Apenas queries SELECT são permitidas."})

        try:
            with engine.connect() as conn:
                result = conn.execute(text(sql))
                cols   = list(result.keys())
                rows   = [dict(zip(cols, row)) for row in result.fetchmany(100)]

            logger.info("sql_executed", rows=len(rows), sql=sql[:100])

            with Session(engine) as db:
                db.add(QueryLog(pergunta="via_tool", sql_gerado=sql))
                db.commit()

            return json.dumps({"colunas": cols, "dados": rows, "total": len(rows)}, ensure_ascii=False, default=str)

        except Exception as e:
            logger.error("sql_error", error=str(e), sql=sql[:100])
            return json.dumps({"erro": f"Erro ao executar SQL: {str(e)}"})

    elif nome == "calcular_insight":
        try:
            dados = json.loads(params.get("dados_json", "[]"))
            calculo = params.get("calculo", "")

            if not dados:
                return json.dumps({"resultado": "Sem dados para calcular."})

            df = pd.DataFrame(dados)
            resultado = {}

            if calculo == "media":
                nums = df.select_dtypes(include="number")
                resultado = nums.mean().round(2).to_dict()
            elif calculo == "crescimento_percentual" and len(dados) >= 2:
                cols_num = df.select_dtypes(include="number").columns.tolist()
                if cols_num:
                    col = cols_num[0]
                    v1, v2 = df[col].iloc[0], df[col].iloc[-1]
                    pct = round(((v2 - v1) / v1 * 100), 2) if v1 != 0 else 0
                    resultado = {"crescimento_percentual": pct, "de": v1, "para": v2}
            elif calculo == "top_n":
                resultado = df.head(5).to_dict(orient="records")
            else:
                resultado = {"resumo": df.describe().round(2).to_dict()}

            return json.dumps({"calculo": calculo, "resultado": resultado}, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"erro": str(e)})

    return json.dumps({"erro": f"Ferramenta '{nome}' não encontrada."})

# ─── AGENTE PRINCIPAL ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Você é DataBot, um analista de dados sênior especializado em SQL e análise de negócios.

Seu papel é responder perguntas sobre dados de forma clara, precisa e com insights acionáveis.

PROCESSO OBRIGATÓRIO:
1. SEMPRE comece com obter_schema para entender as tabelas disponíveis
2. Gere um SQL preciso e execute com executar_sql
3. Se relevante, use calcular_insight para métricas adicionais
4. Responda em linguagem natural com os dados encontrados

REGRAS DE SQL:
- Use APENAS queries SELECT
- Inclua LIMIT 100 em todas as queries
- Para datas em SQLite, use: strftime('%Y-%m', data) para agrupar por mês
- Para valores monetários, formate com ROUND(valor, 2)
- Prefira nomes explícitos de colunas a SELECT *

FORMATO DE RESPOSTA:
- Comece com a resposta direta à pergunta
- Destaque os números mais relevantes
- Adicione 1-2 insights ou observações úteis
- Ofereça uma pergunta de follow-up relacionada

Se não houver dados suficientes ou a pergunta for impossível de responder com os dados disponíveis, diga claramente o motivo.
"""

@retry(max_attempts=3, base_delay=1.0)
async def processar_pergunta(pergunta: str, historico: list = None) -> dict:
    logger = StructuredLogger("analitico")
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    messages = (historico or []) + [{"role": "user", "content": pergunta}]
    sql_executado = None

    with Timer(logger, "data_agent_loop"):
        for i in range(config.MAX_ITERATIONS):
            response = client.messages.create(
                model=config.MODEL,
                max_tokens=config.MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                texto = next((b.text for b in response.content if hasattr(b, "text")), "")
                logger.info("answer_generated", pergunta=pergunta[:80], sql=sql_executado)
                return {
                    "resposta": texto,
                    "sql_executado": sql_executado,
                    "iteracoes": i + 1
                }

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        if block.name == "executar_sql":
                            sql_executado = block.input.get("sql")
                        resultado = executar_ferramenta(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": resultado,
                        })
                messages.append({"role": "user", "content": tool_results})

    return {"resposta": "Não consegui processar a pergunta dentro do limite de iterações.", "sql_executado": None, "iteracoes": config.MAX_ITERATIONS}

# ─── API FASTAPI ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    StructuredLogger("analitico").info("agent_started", agent="analitico")
    yield

app = FastAPI(title="Agente Analítico de Dados", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class PerguntaRequest(BaseModel):
    pergunta: str
    session_id: Optional[str] = None

@app.post("/perguntar")
async def perguntar(req: PerguntaRequest):
    """Faz uma pergunta em linguagem natural sobre os dados."""
    try:
        resultado = await processar_pergunta(req.pergunta)
        return success_response(resultado)
    except Exception as e:
        StructuredLogger("analitico").error("api_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/upload-csv")
async def upload_csv(file: UploadFile = File(...), nome_tabela: str = "dados_importados"):
    """Importa um CSV para o banco de dados e disponibiliza para consulta."""
    try:
        contents = await file.read()
        df = pd.read_csv(io.StringIO(contents.decode("utf-8")))
        df.to_sql(nome_tabela, engine, if_exists="replace", index=False)
        return success_response({
            "tabela": nome_tabela,
            "linhas": len(df),
            "colunas": list(df.columns)
        }, f"CSV importado com sucesso: {len(df)} linhas")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/schema")
async def schema():
    """Retorna o schema de todas as tabelas disponíveis."""
    resultado = executar_ferramenta("obter_schema", {})
    return success_response(json.loads(resultado))

@app.get("/exemplos")
async def exemplos():
    """Perguntas de exemplo para demonstração."""
    return success_response([
        "Qual foi o total de vendas por mês em 2024?",
        "Quais são os 5 produtos mais vendidos?",
        "Qual vendedor teve melhor desempenho?",
        "Compare as vendas por região",
        "Qual a categoria que mais cresceu no segundo semestre?",
        "Qual o ticket médio por cliente?",
    ])

@app.get("/health")
async def health():
    return {"status": "ok", "agent": "analitico", "timestamp": datetime.utcnow().isoformat()}


if __name__ == "__main__":
    import asyncio
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table as RichTable

    console = Console()
    console.print(Panel.fit(
        "[bold cyan]Agente Analítico — Modo Demo[/bold cyan]\n"
        "Pergunte sobre vendas, produtos, vendedores, regiões...\n"
        "Digite 'sair' para encerrar",
        border_style="cyan"
    ))

    async def demo():
        while True:
            pergunta = console.input("\n[bold green]Pergunta:[/bold green] ")
            if pergunta.lower() == "sair":
                break
            console.print("[dim]Analisando dados...[/dim]")
            resultado = await processar_pergunta(pergunta)
            console.print(Panel(
                resultado["resposta"],
                title="[bold cyan]DataBot[/bold cyan]",
                border_style="cyan"
            ))
            if resultado.get("sql_executado"):
                console.print(f"[dim]SQL: {resultado['sql_executado'][:120]}...[/dim]")

    asyncio.run(demo())
