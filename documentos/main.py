"""
AGENTE 3 — PROCESSADOR DE DOCUMENTOS
=======================================
Extrai, classifica e estrutura informações de documentos:
- PDFs (contratos, relatórios, manuais)
- Texto livre (e-mails, notas, descrições)
- Dados estruturados (NF-e simulada, formulários)

Funcionalidades:
- Classificação automática do tipo de documento
- Extração de campos-chave (valores, datas, partes, prazos)
- Resumo executivo automático
- Detecção de cláusulas importantes em contratos
- Armazenamento e busca de documentos processados

Rota principal: POST /processar (texto ou JSON)
Rota de upload: POST /upload-pdf
Rota de busca: GET /documentos?tipo=contrato
"""

import json
import uuid
import io
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

import anthropic
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Text, DateTime, Float
from sqlalchemy.orm import DeclarativeBase, Session

import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.utils import config, StructuredLogger, retry, Timer, success_response, error_response

# ─── BANCO DE DADOS ──────────────────────────────────────────────────────────

engine = create_engine("sqlite:///./documentos.db", connect_args={"check_same_thread": False})

class Base(DeclarativeBase):
    pass

class Documento(Base):
    __tablename__ = "documentos"
    id             = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tipo           = Column(String, index=True)    # contrato, nfe, relatorio, email, outro
    titulo         = Column(String)
    resumo         = Column(Text)
    campos_json    = Column(Text)                  # campos extraídos em JSON
    alertas_json   = Column(Text)                  # alertas e pontos de atenção
    texto_original = Column(Text)
    confianca      = Column(Float, default=0.0)    # 0.0 a 1.0
    processado_em  = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

# ─── FERRAMENTAS DO AGENTE ───────────────────────────────────────────────────

TOOLS = [
    {
        "name": "classificar_documento",
        "description": "Classifica o tipo de documento e extrai metadados básicos.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo": {
                    "type": "string",
                    "enum": ["contrato", "nfe", "relatorio", "email", "orcamento", "laudo", "outro"],
                    "description": "Tipo do documento identificado"
                },
                "titulo_sugerido": {"type": "string", "description": "Título descritivo para o documento"},
                "confianca": {"type": "number", "description": "Nível de confiança na classificação (0.0 a 1.0)"}
            },
            "required": ["tipo", "titulo_sugerido", "confianca"]
        }
    },
    {
        "name": "extrair_campos",
        "description": "Extrai campos estruturados do documento conforme seu tipo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "campos": {
                    "type": "object",
                    "description": """Campos a extrair por tipo:
                    CONTRATO: partes, valor_total, data_inicio, data_fim, prazo_vigencia, clausulas_principais, penalidades, foro
                    NFE: emitente, destinatario, numero_nfe, data_emissao, valor_total, valor_icms, itens, chave_acesso
                    RELATORIO: periodo, elaborado_por, conclusoes_principais, metricas, recomendacoes
                    EMAIL: remetente, destinatario, assunto, data, acao_necessaria, prazo
                    ORCAMENTO: fornecedor, cliente, itens, valor_total, validade, condicoes_pagamento
                    """
                }
            },
            "required": ["campos"]
        }
    },
    {
        "name": "identificar_alertas",
        "description": "Identifica pontos de atenção, riscos e cláusulas importantes que requerem revisão humana.",
        "input_schema": {
            "type": "object",
            "properties": {
                "alertas": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "nivel": {"type": "string", "enum": ["alto", "medio", "baixo"]},
                            "tipo": {"type": "string"},
                            "descricao": {"type": "string"},
                            "trecho": {"type": "string", "description": "Trecho do documento que gerou o alerta"}
                        }
                    }
                }
            },
            "required": ["alertas"]
        }
    },
    {
        "name": "gerar_resumo",
        "description": "Gera um resumo executivo do documento em linguagem clara e objetiva.",
        "input_schema": {
            "type": "object",
            "properties": {
                "resumo": {"type": "string", "description": "Resumo executivo em 3-5 frases"}
            },
            "required": ["resumo"]
        }
    }
]

# ─── ESTADO DE EXTRAÇÃO ───────────────────────────────────────────────────────

class EstadoExtracao:
    def __init__(self):
        self.tipo = None
        self.titulo = None
        self.confianca = 0.0
        self.campos = {}
        self.alertas = []
        self.resumo = ""

def executar_ferramenta(nome: str, params: dict, estado: EstadoExtracao) -> str:
    if nome == "classificar_documento":
        estado.tipo      = params["tipo"]
        estado.titulo    = params["titulo_sugerido"]
        estado.confianca = params["confianca"]
        return json.dumps({"ok": True, "tipo": estado.tipo})

    elif nome == "extrair_campos":
        estado.campos = params.get("campos", {})
        return json.dumps({"ok": True, "campos_extraidos": len(estado.campos)})

    elif nome == "identificar_alertas":
        estado.alertas = params.get("alertas", [])
        return json.dumps({"ok": True, "alertas": len(estado.alertas)})

    elif nome == "gerar_resumo":
        estado.resumo = params.get("resumo", "")
        return json.dumps({"ok": True})

    return json.dumps({"erro": f"Ferramenta '{nome}' não encontrada."})

# ─── AGENTE PRINCIPAL ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Você é um especialista em análise e extração de informações de documentos corporativos brasileiros.

Sua missão é processar qualquer documento e extrair informações estruturadas de forma precisa.

PROCESSO OBRIGATÓRIO (sempre nesta ordem):
1. classificar_documento — identifique o tipo e crie um título descritivo
2. extrair_campos — extraia TODOS os campos relevantes para o tipo identificado
3. identificar_alertas — aponte riscos, prazos críticos, valores altos, cláusulas abusivas
4. gerar_resumo — escreva um resumo executivo claro

REGRAS CRÍTICAS:
- Extraia APENAS o que está explicitamente no documento — não invente dados
- Para campos não encontrados, use null ou "não informado"
- Para datas, use o formato DD/MM/AAAA quando possível
- Para valores monetários, sempre extraia o número e a moeda
- Em contratos: SEMPRE verifique cláusulas de multa, rescisão e reajuste
- Em NF-e: SEMPRE extraia a chave de acesso e verifique a data de emissão
- Classifique alertas por impacto real no negócio

ALERTAS AUTOMÁTICOS obrigatórios (quando aplicável):
- Prazo vencendo em menos de 30 dias → nível: alto
- Multa rescisória acima de 20% → nível: alto
- Valor acima de R$ 50.000 sem garantias → nível: medio
- Cláusula de exclusividade → nível: medio
- Foro de outra cidade/estado → nível: baixo
"""

@retry(max_attempts=3, base_delay=1.0)
async def processar_documento(texto: str, nome_arquivo: str = "documento") -> dict:
    logger = StructuredLogger("documentos")
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    estado = EstadoExtracao()

    prompt = f"Arquivo: {nome_arquivo}\n\nConteúdo do documento:\n\n{texto[:12000]}"
    messages = [{"role": "user", "content": prompt}]

    with Timer(logger, "document_processing"):
        for i in range(config.MAX_ITERATIONS):
            response = client.messages.create(
                model=config.MODEL,
                max_tokens=config.MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        resultado = executar_ferramenta(block.name, block.input, estado)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": resultado,
                        })
                messages.append({"role": "user", "content": tool_results})

    # Salva no banco
    doc_id = str(uuid.uuid4())
    with Session(engine) as db:
        db.add(Documento(
            id=doc_id,
            tipo=estado.tipo or "outro",
            titulo=estado.titulo or nome_arquivo,
            resumo=estado.resumo,
            campos_json=json.dumps(estado.campos, ensure_ascii=False),
            alertas_json=json.dumps(estado.alertas, ensure_ascii=False),
            texto_original=texto[:5000],
            confianca=estado.confianca,
        ))
        db.commit()

    logger.info(
        "document_processed",
        doc_id=doc_id,
        tipo=estado.tipo,
        alertas=len(estado.alertas),
        confianca=estado.confianca,
    )

    return {
        "doc_id": doc_id,
        "tipo": estado.tipo,
        "titulo": estado.titulo,
        "confianca": estado.confianca,
        "resumo": estado.resumo,
        "campos": estado.campos,
        "alertas": estado.alertas,
    }

# ─── API FASTAPI ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    StructuredLogger("documentos").info("agent_started", agent="documentos")
    yield

app = FastAPI(title="Agente Processador de Documentos", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class ProcessarRequest(BaseModel):
    texto: str
    nome_arquivo: Optional[str] = "documento.txt"

@app.post("/processar")
async def processar(req: ProcessarRequest):
    """Processa um documento em texto e extrai informações estruturadas."""
    if len(req.texto.strip()) < 50:
        raise HTTPException(status_code=400, detail="Documento muito curto (mínimo 50 caracteres)")
    try:
        resultado = await processar_documento(req.texto, req.nome_arquivo)
        return success_response(resultado)
    except Exception as e:
        StructuredLogger("documentos").error("api_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/upload-pdf")
async def upload_pdf(file: UploadFile = File(...)):
    """Faz upload de um PDF e processa automaticamente."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Apenas arquivos PDF são aceitos")
    try:
        import PyPDF2
        contents = await file.read()
        reader   = PyPDF2.PdfReader(io.BytesIO(contents))
        texto    = "\n".join(page.extract_text() or "" for page in reader.pages)

        if len(texto.strip()) < 50:
            raise HTTPException(status_code=400, detail="Não foi possível extrair texto do PDF")

        resultado = await processar_documento(texto, file.filename)
        return success_response(resultado)
    except ImportError:
        raise HTTPException(status_code=500, detail="PyPDF2 não instalado. Execute: pip install PyPDF2")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/documentos")
async def listar_documentos(tipo: Optional[str] = None, limit: int = 20):
    """Lista documentos processados, com filtro opcional por tipo."""
    with Session(engine) as db:
        query = db.query(Documento)
        if tipo:
            query = query.filter(Documento.tipo == tipo)
        docs = query.order_by(Documento.processado_em.desc()).limit(limit).all()

    return success_response([{
        "id": d.id,
        "tipo": d.tipo,
        "titulo": d.titulo,
        "resumo": d.resumo,
        "confianca": d.confianca,
        "alertas_count": len(json.loads(d.alertas_json or "[]")),
        "processado_em": d.processado_em.isoformat(),
    } for d in docs])

@app.get("/documentos/{doc_id}")
async def obter_documento(doc_id: str):
    """Retorna um documento processado com todos os campos extraídos."""
    with Session(engine) as db:
        doc = db.query(Documento).filter(Documento.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Documento não encontrado")
    return success_response({
        "id": doc.id,
        "tipo": doc.tipo,
        "titulo": doc.titulo,
        "resumo": doc.resumo,
        "campos": json.loads(doc.campos_json or "{}"),
        "alertas": json.loads(doc.alertas_json or "[]"),
        "confianca": doc.confianca,
        "processado_em": doc.processado_em.isoformat(),
    })

@app.get("/health")
async def health():
    return {"status": "ok", "agent": "documentos", "timestamp": datetime.utcnow().isoformat()}


if __name__ == "__main__":
    import asyncio
    from rich.console import Console
    from rich.panel import Panel
    from rich.json import JSON

    console = Console()
    console.print(Panel.fit(
        "[bold magenta]Agente de Documentos — Modo Demo[/bold magenta]\n"
        "Cole o texto de um contrato, NF-e ou e-mail abaixo.\n"
        "Digite 'fim' em uma linha vazia para processar.",
        border_style="magenta"
    ))

    CONTRATO_DEMO = """
CONTRATO DE PRESTAÇÃO DE SERVIÇOS DE TECNOLOGIA

Contratante: Empresa Alpha Ltda., CNPJ 12.345.678/0001-99, com sede na Rua das Flores, 100, São Paulo - SP.
Contratada: TechSolutions ME, CNPJ 98.765.432/0001-11, com sede na Av. Paulista, 1000, São Paulo - SP.

Objeto: Desenvolvimento e implementação de sistema de gestão de estoque com inteligência artificial.

Valor: R$ 85.000,00 (oitenta e cinco mil reais), pagos em 3 parcelas.
Prazo: 12 semanas a partir de 15/01/2025, com término previsto para 09/04/2025.
Multa por atraso: 0,5% ao dia sobre o valor total.
Multa rescisória: 30% do valor total do contrato.
Foro: Comarca de São Paulo - SP.
Reajuste: IGPM anual após o primeiro ano de vigência.
Exclusividade: A contratada não poderá prestar serviços similares para concorrentes diretos pelo período de 6 meses.
    """

    async def demo():
        console.print("\n[dim]Usando contrato de demonstração...[/dim]")
        console.print("[dim]Processando...[/dim]")
        resultado = await processar_documento(CONTRATO_DEMO, "contrato_demo.txt")

        console.print(Panel(resultado["resumo"], title="[bold]Resumo[/bold]", border_style="magenta"))
        console.print(Panel(JSON(json.dumps(resultado["campos"], ensure_ascii=False, indent=2)), title="[bold]Campos Extraídos[/bold]"))

        if resultado["alertas"]:
            console.print("\n[bold red]Alertas:[/bold red]")
            for a in resultado["alertas"]:
                nivel_color = {"alto": "red", "medio": "yellow", "baixo": "blue"}.get(a["nivel"], "white")
                console.print(f"  [{nivel_color}][{a['nivel'].upper()}][/{nivel_color}] {a['descricao']}")

    asyncio.run(demo())
