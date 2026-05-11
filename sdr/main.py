"""
AGENTE 4 — SDR DE VENDAS (Sales Development Representative)
=============================================================
Agente que automatiza a prospecção e qualificação de leads:
- Recebe um lead (nome, empresa, cargo, contexto)
- Pesquisa e enriquece o perfil
- Qualifica usando BANT (Budget, Authority, Need, Timeline)
- Gera mensagem de prospecção personalizada
- Decide próximo passo: agendar reunião, nutrir ou descartar
- Registra toda a pipeline de vendas

Rota principal: POST /qualificar
Rota de mensagem: POST /gerar-mensagem
Rota de pipeline: GET /pipeline
Rota de followup: POST /followup
"""

import json
import uuid
from datetime import datetime, timedelta
from typing import Optional
from contextlib import asynccontextmanager
from enum import Enum

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Text, DateTime, Integer, Float
from sqlalchemy.orm import DeclarativeBase, Session

import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.utils import config, StructuredLogger, retry, Timer, success_response, error_response

# ─── BANCO DE DADOS ──────────────────────────────────────────────────────────

engine = create_engine("sqlite:///./sdr.db", connect_args={"check_same_thread": False})

class Base(DeclarativeBase):
    pass

class Lead(Base):
    __tablename__ = "leads"
    id              = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    nome            = Column(String)
    cargo           = Column(String)
    empresa         = Column(String)
    setor           = Column(String)
    email           = Column(String)
    linkedin        = Column(String)
    contexto        = Column(Text)          # notas sobre o lead
    score           = Column(Integer, default=0)   # 0-100
    status          = Column(String, default="novo")  # novo, qualificado, reuniao, nurturing, descartado, fechado
    budget_ok       = Column(Integer, default=0)   # 0/1
    authority_ok    = Column(Integer, default=0)
    need_ok         = Column(Integer, default=0)
    timeline_ok     = Column(Integer, default=0)
    justificativa   = Column(Text)
    proximos_passos = Column(Text)
    mensagem_gerada = Column(Text)
    criado_em       = Column(DateTime, default=datetime.utcnow)
    atualizado_em   = Column(DateTime, default=datetime.utcnow)

class Interacao(Base):
    __tablename__ = "interacoes"
    id          = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    lead_id     = Column(String)
    tipo        = Column(String)   # email_enviado, ligacao, reuniao, followup
    conteudo    = Column(Text)
    timestamp   = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

# ─── FERRAMENTAS DO AGENTE ───────────────────────────────────────────────────

TOOLS = [
    {
        "name": "analisar_perfil",
        "description": "Analisa o perfil do lead: setor da empresa, maturidade tecnológica provável, dores comuns do cargo, e fit com a solução de IA.",
        "input_schema": {
            "type": "object",
            "properties": {
                "setor_identificado": {"type": "string"},
                "porte_provavel": {"type": "string", "enum": ["startup", "pequena", "media", "grande", "enterprise"]},
                "dores_provaveis": {"type": "array", "items": {"type": "string"}},
                "fit_score_inicial": {"type": "integer", "description": "Score de fit 0-100 baseado no perfil"}
            },
            "required": ["setor_identificado", "porte_provavel", "dores_provaveis", "fit_score_inicial"]
        }
    },
    {
        "name": "qualificar_bant",
        "description": "Qualifica o lead usando metodologia BANT (Budget, Authority, Need, Timeline).",
        "input_schema": {
            "type": "object",
            "properties": {
                "budget": {
                    "type": "object",
                    "properties": {
                        "qualificado": {"type": "boolean"},
                        "justificativa": {"type": "string"}
                    }
                },
                "authority": {
                    "type": "object",
                    "properties": {
                        "qualificado": {"type": "boolean"},
                        "justificativa": {"type": "string"}
                    }
                },
                "need": {
                    "type": "object",
                    "properties": {
                        "qualificado": {"type": "boolean"},
                        "justificativa": {"type": "string"}
                    }
                },
                "timeline": {
                    "type": "object",
                    "properties": {
                        "qualificado": {"type": "boolean"},
                        "justificativa": {"type": "string"}
                    }
                },
                "score_final": {"type": "integer", "description": "Score final 0-100"},
                "recomendacao": {
                    "type": "string",
                    "enum": ["agendar_reuniao", "nurturing_30_dias", "nurturing_90_dias", "descartar"]
                }
            },
            "required": ["budget", "authority", "need", "timeline", "score_final", "recomendacao"]
        }
    },
    {
        "name": "gerar_mensagem_prospeccao",
        "description": "Gera uma mensagem de prospecção personalizada para o lead (LinkedIn ou e-mail).",
        "input_schema": {
            "type": "object",
            "properties": {
                "canal": {"type": "string", "enum": ["linkedin", "email"]},
                "assunto": {"type": "string", "description": "Assunto do e-mail (se canal for email)"},
                "mensagem": {"type": "string", "description": "Texto da mensagem personalizado para este lead específico"}
            },
            "required": ["canal", "mensagem"]
        }
    },
    {
        "name": "definir_proximos_passos",
        "description": "Define os próximos passos táticos para avançar com este lead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "passos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "acao": {"type": "string"},
                            "prazo": {"type": "string"},
                            "responsavel": {"type": "string", "enum": ["agente_ia", "vendedor_humano", "marketing"]}
                        }
                    }
                }
            },
            "required": ["passos"]
        }
    }
]

# ─── EXECUTOR ────────────────────────────────────────────────────────────────

class EstadoQualificacao:
    def __init__(self):
        self.perfil = {}
        self.bant = {}
        self.score = 0
        self.recomendacao = ""
        self.mensagem = ""
        self.canal = ""
        self.assunto = ""
        self.passos = []

def executar_ferramenta(nome: str, params: dict, estado: EstadoQualificacao) -> str:
    if nome == "analisar_perfil":
        estado.perfil = params
        return json.dumps({"ok": True, "fit_score_inicial": params.get("fit_score_inicial")})

    elif nome == "qualificar_bant":
        estado.bant        = params
        estado.score       = params.get("score_final", 0)
        estado.recomendacao = params.get("recomendacao", "nurturing_30_dias")
        return json.dumps({"ok": True, "score": estado.score, "recomendacao": estado.recomendacao})

    elif nome == "gerar_mensagem_prospeccao":
        estado.mensagem = params.get("mensagem", "")
        estado.canal    = params.get("canal", "email")
        estado.assunto  = params.get("assunto", "")
        return json.dumps({"ok": True, "chars": len(estado.mensagem)})

    elif nome == "definir_proximos_passos":
        estado.passos = params.get("passos", [])
        return json.dumps({"ok": True, "passos": len(estado.passos)})

    return json.dumps({"erro": f"Ferramenta '{nome}' não encontrada."})

# ─── AGENTE PRINCIPAL ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Você é Max, um SDR (Sales Development Representative) especializado em vender soluções de IA para empresas.

Seu produto: implementação de agentes de IA para automatizar processos empresariais.
Ticket médio: R$ 15.000 a R$ 80.000 por projeto.
Público-alvo: gestores e diretores de PMEs com 10-500 funcionários.

PROCESSO OBRIGATÓRIO:
1. analisar_perfil — estude o lead: setor, porte, dores prováveis, fit inicial
2. qualificar_bant — avalie Budget, Authority, Need, Timeline com justificativa
3. gerar_mensagem_prospeccao — crie mensagem 100% personalizada (LinkedIn ou e-mail)
4. definir_proximos_passos — planeje as ações concretas com prazos

CRITÉRIOS BANT para IA:
BUDGET ✓: empresa tem 10+ funcionários OU processo caro (ex: >5 pessoas em tarefa manual) OU cargo indica orçamento (diretor, sócio, CEO)
BUDGET ✗: MEI, freelancer, startup pré-revenue
AUTHORITY ✓: CEO, diretor, sócio, gerente sênior, head de área
AUTHORITY ✗: analista, assistente, estagiário (nutrir e buscar o decisor)
NEED ✓: menciona processos repetitivos, muita equipe em tarefas manuais, crescimento sem contratar, perda de clientes por lentidão
NEED ✗: empresa recém-automatizada, startup early-stage sem processo definido
TIMELINE ✓: dor urgente, crescimento rápido, meta de corte de custos no trimestre
TIMELINE ✗: sem pressão clara, "talvez no ano que vem"

SCORE final:
80-100: agendar_reuniao (4 BANTs ok ou 3 ok com urgência alta)
60-79: nurturing_30_dias (2-3 BANTs ok)
40-59: nurturing_90_dias (1-2 BANTs ok)
0-39: descartar (lead fora do ICP)

MENSAGEM de prospecção — REGRAS INVIOLÁVEIS:
- Máximo 150 palavras para LinkedIn, 200 para e-mail
- Primeira linha: referência específica ao negócio/cargo da pessoa
- Segunda linha: dor específica que você identificou
- Terceira linha: resultado concreto que você já gerou (ex: "Implementei isso em [tipo de empresa] e economizou X horas/mês")
- CTA: simples e sem pressão ("15 minutos essa semana?")
- NUNCA: "Espero que esteja bem", "Trabalho com IA", pitch genérico, mais de 1 CTA
"""

@retry(max_attempts=3, base_delay=1.0)
async def qualificar_lead(dados_lead: dict) -> dict:
    logger = StructuredLogger("sdr")
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    estado = EstadoQualificacao()

    prompt = f"""Qualifique este lead para venda de soluções de IA:

Nome: {dados_lead.get('nome', 'Não informado')}
Cargo: {dados_lead.get('cargo', 'Não informado')}
Empresa: {dados_lead.get('empresa', 'Não informado')}
Setor: {dados_lead.get('setor', 'Não informado')}
Contexto/Notas: {dados_lead.get('contexto', 'Sem informações adicionais')}
Canal preferido: {dados_lead.get('canal', 'linkedin')}
"""

    messages = [{"role": "user", "content": prompt}]

    with Timer(logger, "lead_qualification"):
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
    bant = estado.bant
    lead_id = str(uuid.uuid4())
    with Session(engine) as db:
        lead = Lead(
            id=lead_id,
            nome=dados_lead.get("nome"),
            cargo=dados_lead.get("cargo"),
            empresa=dados_lead.get("empresa"),
            setor=estado.perfil.get("setor_identificado"),
            email=dados_lead.get("email"),
            linkedin=dados_lead.get("linkedin"),
            contexto=dados_lead.get("contexto"),
            score=estado.score,
            status="qualificado" if estado.score >= 60 else "nurturing" if estado.score >= 40 else "descartado",
            budget_ok=int(bant.get("budget", {}).get("qualificado", False)),
            authority_ok=int(bant.get("authority", {}).get("qualificado", False)),
            need_ok=int(bant.get("need", {}).get("qualificado", False)),
            timeline_ok=int(bant.get("timeline", {}).get("qualificado", False)),
            justificativa=json.dumps(bant, ensure_ascii=False),
            proximos_passos=json.dumps(estado.passos, ensure_ascii=False),
            mensagem_gerada=estado.mensagem,
        )
        db.add(lead)
        db.commit()

    logger.info(
        "lead_qualified",
        lead_id=lead_id,
        score=estado.score,
        recomendacao=estado.recomendacao,
        empresa=dados_lead.get("empresa"),
    )

    return {
        "lead_id": lead_id,
        "score": estado.score,
        "recomendacao": estado.recomendacao,
        "bant": {
            "budget":    {"ok": bool(bant.get("budget",{}).get("qualificado")),    "motivo": bant.get("budget",{}).get("justificativa")},
            "authority": {"ok": bool(bant.get("authority",{}).get("qualificado")), "motivo": bant.get("authority",{}).get("justificativa")},
            "need":      {"ok": bool(bant.get("need",{}).get("qualificado")),      "motivo": bant.get("need",{}).get("justificativa")},
            "timeline":  {"ok": bool(bant.get("timeline",{}).get("qualificado")),  "motivo": bant.get("timeline",{}).get("justificativa")},
        },
        "dores_identificadas": estado.perfil.get("dores_provaveis", []),
        "mensagem_prospeccao": {
            "canal": estado.canal,
            "assunto": estado.assunto,
            "texto": estado.mensagem,
        },
        "proximos_passos": estado.passos,
    }

# ─── API FASTAPI ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    StructuredLogger("sdr").info("agent_started", agent="sdr")
    yield

app = FastAPI(title="Agente SDR de Vendas", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class LeadRequest(BaseModel):
    nome: str
    cargo: str
    empresa: str
    setor: Optional[str] = None
    email: Optional[str] = None
    linkedin: Optional[str] = None
    contexto: Optional[str] = None
    canal: Optional[str] = "linkedin"

@app.post("/qualificar")
async def qualificar(req: LeadRequest):
    """Qualifica um lead e gera mensagem de prospecção personalizada."""
    try:
        resultado = await qualificar_lead(req.model_dump())
        return success_response(resultado)
    except Exception as e:
        StructuredLogger("sdr").error("api_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/pipeline")
async def pipeline(status: Optional[str] = None):
    """Visão completa da pipeline de leads."""
    with Session(engine) as db:
        query = db.query(Lead)
        if status:
            query = query.filter(Lead.status == status)
        leads = query.order_by(Lead.score.desc()).all()

    return success_response([{
        "id": l.id,
        "nome": l.nome,
        "cargo": l.cargo,
        "empresa": l.empresa,
        "score": l.score,
        "status": l.status,
        "bant": f"B{'✓' if l.budget_ok else '✗'} A{'✓' if l.authority_ok else '✗'} N{'✓' if l.need_ok else '✗'} T{'✓' if l.timeline_ok else '✗'}",
        "criado_em": l.criado_em.isoformat(),
    } for l in leads])

@app.get("/pipeline/stats")
async def pipeline_stats():
    """Estatísticas da pipeline."""
    with Session(engine) as db:
        total    = db.query(Lead).count()
        por_status = {}
        for status in ["qualificado", "nurturing", "descartado", "fechado"]:
            por_status[status] = db.query(Lead).filter(Lead.status == status).count()
        score_medio = db.query(Lead).with_entities(Lead.score).all()
        media = round(sum(s[0] for s in score_medio) / len(score_medio), 1) if score_medio else 0

    return success_response({
        "total_leads": total,
        "por_status": por_status,
        "score_medio": media,
        "taxa_qualificacao": round(por_status.get("qualificado", 0) / total * 100, 1) if total else 0,
    })

@app.put("/pipeline/{lead_id}/status")
async def atualizar_status(lead_id: str, novo_status: str):
    """Atualiza o status de um lead na pipeline."""
    status_validos = ["novo", "qualificado", "reuniao", "nurturing", "descartado", "fechado"]
    if novo_status not in status_validos:
        raise HTTPException(status_code=400, detail=f"Status inválido. Use: {status_validos}")
    with Session(engine) as db:
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead não encontrado")
        lead.status = novo_status
        lead.atualizado_em = datetime.utcnow()
        db.commit()
    return success_response({"id": lead_id, "status": novo_status})

@app.get("/health")
async def health():
    return {"status": "ok", "agent": "sdr", "timestamp": datetime.utcnow().isoformat()}


if __name__ == "__main__":
    import asyncio
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table as RichTable

    console = Console()

    LEAD_DEMO = {
        "nome": "Fernanda Oliveira",
        "cargo": "Diretora de Operações",
        "empresa": "Rede MedCenter (15 clínicas)",
        "setor": "Saúde",
        "linkedin": "linkedin.com/in/fernandaoliveira",
        "contexto": "Vi que postou recentemente sobre dificuldades de escalar o atendimento sem contratar mais recepcionistas. A rede cresceu 40% em 2024 mas a equipe administrativa não acompanhou.",
        "canal": "linkedin"
    }

    console.print(Panel.fit(
        "[bold yellow]Agente SDR — Modo Demo[/bold yellow]\n"
        f"Lead: {LEAD_DEMO['nome']} | {LEAD_DEMO['cargo']} @ {LEAD_DEMO['empresa']}",
        border_style="yellow"
    ))

    async def demo():
        console.print("[dim]Qualificando lead...[/dim]")
        resultado = await qualificar_lead(LEAD_DEMO)

        t = RichTable(title="Resultado BANT", show_header=True)
        t.add_column("Critério"); t.add_column("Status"); t.add_column("Motivo")
        for k, v in resultado["bant"].items():
            t.add_row(k.upper(), "✓" if v["ok"] else "✗", v["motivo"] or "—")
        console.print(t)

        console.print(f"\n[bold]Score:[/bold] {resultado['score']}/100")
        console.print(f"[bold]Recomendação:[/bold] {resultado['recomendacao']}")
        console.print(Panel(
            resultado["mensagem_prospeccao"]["texto"],
            title=f"[bold yellow]Mensagem ({resultado['mensagem_prospeccao']['canal']})[/bold yellow]",
            border_style="yellow"
        ))

    asyncio.run(demo())
