"""
AGENTE 1 — ATENDIMENTO AO CLIENTE
===================================
Agente de atendimento inteligente com:
- Memória de conversa (histórico por sessão)
- Ferramentas: consultar FAQ, verificar pedido, escalar para humano
- API FastAPI pronta para conectar ao WhatsApp, chat ou qualquer frontend
- Retry automático, logging estruturado, limite de iterações

Rota principal: POST /chat
Rota de histórico: GET /chat/{session_id}/history
Rota de escalada: GET /escaladas

Como conectar ao WhatsApp:
    Use a Evolution API (open source) ou Twilio como webhook
    aponte o webhook para POST /chat
"""

import json
import uuid
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Text, DateTime, Boolean
from sqlalchemy.orm import DeclarativeBase, Session

import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.utils import config, StructuredLogger, retry, Timer, success_response, error_response

# ─── BANCO DE DADOS ──────────────────────────────────────────────────────────

engine = create_engine("sqlite:///./atendimento.db", connect_args={"check_same_thread": False})

class Base(DeclarativeBase):
    pass

class Conversa(Base):
    __tablename__ = "conversas"
    id           = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id   = Column(String, index=True)
    role         = Column(String)   # "user" | "assistant"
    content      = Column(Text)
    timestamp    = Column(DateTime, default=datetime.utcnow)

class Escalada(Base):
    __tablename__ = "escaladas"
    id           = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id   = Column(String)
    motivo       = Column(Text)
    ultima_msg   = Column(Text)
    resolvida    = Column(Boolean, default=False)
    timestamp    = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

# ─── DADOS DE DEMONSTRAÇÃO ───────────────────────────────────────────────────

FAQ_BASE = {
    "horario": "Atendemos de segunda a sexta, das 9h às 18h. Fora deste horário, este agente está disponível 24/7.",
    "entrega": "O prazo de entrega é de 3 a 7 dias úteis para todo o Brasil. Frete grátis acima de R$ 150.",
    "troca": "Aceitamos trocas em até 30 dias após a compra. O produto deve estar sem uso e na embalagem original.",
    "pagamento": "Aceitamos cartão de crédito (até 12x), PIX (5% de desconto) e boleto bancário.",
    "cancelamento": "Cancelamentos podem ser feitos em até 24h após a compra pelo site ou por aqui.",
}

PEDIDOS_DEMO = {
    "PED-001": {"status": "Em separação", "previsao": "2 dias úteis", "produto": "Kit Office Pro"},
    "PED-002": {"status": "Enviado", "previsao": "Amanhã até 20h", "produto": "Cadeira Ergonômica", "rastreio": "BR123456789"},
    "PED-003": {"status": "Entregue", "previsao": "—", "produto": "Monitor 27\""},
}

# ─── FERRAMENTAS DO AGENTE ───────────────────────────────────────────────────

TOOLS = [
    {
        "name": "consultar_faq",
        "description": "Consulta a base de conhecimento da empresa para responder perguntas frequentes sobre horários, entrega, troca, pagamento e cancelamento.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tema": {
                    "type": "string",
                    "description": "Tema da dúvida. Opções: horario, entrega, troca, pagamento, cancelamento",
                    "enum": ["horario", "entrega", "troca", "pagamento", "cancelamento"]
                }
            },
            "required": ["tema"]
        }
    },
    {
        "name": "verificar_pedido",
        "description": "Verifica o status de um pedido pelo número. Use quando o cliente mencionar número de pedido.",
        "input_schema": {
            "type": "object",
            "properties": {
                "numero_pedido": {
                    "type": "string",
                    "description": "Número do pedido no formato PED-XXX"
                }
            },
            "required": ["numero_pedido"]
        }
    },
    {
        "name": "escalar_para_humano",
        "description": "Escala o atendimento para um agente humano. Use quando: o cliente está frustrado, o problema é complexo, envolve reembolso acima de R$500, ou o cliente pedir explicitamente.",
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {
                    "type": "string",
                    "description": "Motivo detalhado da escalada para orientar o atendente humano"
                }
            },
            "required": ["motivo"]
        }
    }
]

# ─── EXECUTOR DE FERRAMENTAS ─────────────────────────────────────────────────

def executar_ferramenta(nome: str, params: dict, session_id: str) -> str:
    logger = StructuredLogger("atendimento")

    if nome == "consultar_faq":
        tema = params.get("tema", "")
        resposta = FAQ_BASE.get(tema, "Informação não encontrada na base de conhecimento.")
        logger.info("tool_faq", session_id=session_id, tema=tema)
        return json.dumps({"resposta": resposta}, ensure_ascii=False)

    elif nome == "verificar_pedido":
        num = params.get("numero_pedido", "").upper()
        pedido = PEDIDOS_DEMO.get(num)
        if pedido:
            logger.info("tool_pedido", session_id=session_id, pedido=num, status=pedido["status"])
            return json.dumps(pedido, ensure_ascii=False)
        return json.dumps({"erro": f"Pedido {num} não encontrado."})

    elif nome == "escalar_para_humano":
        motivo = params.get("motivo", "")
        with Session(engine) as db:
            escalada = Escalada(session_id=session_id, motivo=motivo)
            db.add(escalada)
            db.commit()
        logger.info("tool_escalada", session_id=session_id, motivo=motivo)
        return json.dumps({
            "status": "escalada_criada",
            "mensagem": "Um atendente humano entrará em contato em até 30 minutos durante o horário comercial."
        }, ensure_ascii=False)

    return json.dumps({"erro": f"Ferramenta '{nome}' não encontrada."})

# ─── AGENTE PRINCIPAL ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Você é Ana, assistente virtual de atendimento ao cliente da [Nome da Empresa].

Seu objetivo é resolver as dúvidas e problemas dos clientes de forma ágil, empática e eficiente.

REGRAS OBRIGATÓRIAS:
1. Sempre use as ferramentas disponíveis antes de responder — nunca invente informações
2. Se não souber ou a ferramenta não retornar dados, diga honestamente e escale para humano
3. Seja empática: reconheça a frustração do cliente antes de resolver o problema
4. Respostas curtas e diretas — máximo 3 parágrafos
5. Use linguagem informal mas profissional (você, não tu)
6. Se o cliente mencionar número de pedido, sempre use a ferramenta verificar_pedido
7. Escale para humano imediatamente se: cliente irritado por mais de 2 mensagens, reembolso, problema com entrega atrasada há mais de 5 dias

NUNCA:
- Invente informações sobre preços, prazos ou políticas
- Prometa algo que não está na base de conhecimento
- Ignore sinais de frustração do cliente
"""

@retry(max_attempts=3, base_delay=1.0)
async def processar_mensagem(session_id: str, mensagem_usuario: str) -> str:
    logger = StructuredLogger("atendimento")
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # Carrega histórico do banco
    with Session(engine) as db:
        historico_db = db.query(Conversa)\
            .filter(Conversa.session_id == session_id)\
            .order_by(Conversa.timestamp)\
            .limit(20)\
            .all()

    messages = [{"role": r.role, "content": r.content} for r in historico_db]
    messages.append({"role": "user", "content": mensagem_usuario})

    iteracoes = 0
    with Timer(logger, "agent_loop"):
        while iteracoes < config.MAX_ITERATIONS:
            iteracoes += 1
            logger.info("agent_iteration", session_id=session_id, iteration=iteracoes)

            response = client.messages.create(
                model=config.MODEL,
                max_tokens=config.MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            # Agente terminou — retorna texto
            if response.stop_reason == "end_turn":
                texto = next((b.text for b in response.content if hasattr(b, "text")), "")

                # Salva no banco
                with Session(engine) as db:
                    db.add(Conversa(session_id=session_id, role="user", content=mensagem_usuario))
                    db.add(Conversa(session_id=session_id, role="assistant", content=texto))
                    db.commit()

                logger.info("response_generated", session_id=session_id, chars=len(texto))
                return texto

            # Agente quer usar ferramentas
            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []

                for block in response.content:
                    if block.type == "tool_use":
                        resultado = executar_ferramenta(block.name, block.input, session_id)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": resultado,
                        })

                messages.append({"role": "user", "content": tool_results})

    return "Desculpe, não consegui processar sua solicitação. Por favor, tente novamente ou aguarde um atendente."

# ─── API FASTAPI ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    StructuredLogger("atendimento").info("agent_started", agent="atendimento")
    yield

app = FastAPI(title="Agente de Atendimento", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    mensagem: str

class ChatResponse(BaseModel):
    session_id: str
    resposta: str
    timestamp: str

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Endpoint principal de chat. Conecte seu WhatsApp ou frontend aqui."""
    session_id = req.session_id or str(uuid.uuid4())
    try:
        resposta = await processar_mensagem(session_id, req.mensagem)
        return ChatResponse(
            session_id=session_id,
            resposta=resposta,
            timestamp=datetime.utcnow().isoformat() + "Z",
        )
    except Exception as e:
        StructuredLogger("atendimento").error("chat_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/chat/{session_id}/history")
async def historico(session_id: str):
    """Retorna o histórico completo de uma conversa."""
    with Session(engine) as db:
        msgs = db.query(Conversa)\
            .filter(Conversa.session_id == session_id)\
            .order_by(Conversa.timestamp)\
            .all()
    return success_response([{"role": m.role, "content": m.content, "timestamp": m.timestamp.isoformat()} for m in msgs])

@app.get("/escaladas")
async def listar_escaladas(resolvida: bool = False):
    """Lista escaladas para o painel do atendente humano."""
    with Session(engine) as db:
        esc = db.query(Escalada).filter(Escalada.resolvida == resolvida).all()
    return success_response([{"id": e.id, "session_id": e.session_id, "motivo": e.motivo, "timestamp": e.timestamp.isoformat()} for e in esc])

@app.put("/escaladas/{escalada_id}/resolver")
async def resolver_escalada(escalada_id: str):
    """Marca uma escalada como resolvida."""
    with Session(engine) as db:
        esc = db.query(Escalada).filter(Escalada.id == escalada_id).first()
        if not esc:
            raise HTTPException(status_code=404, detail="Escalada não encontrada")
        esc.resolvida = True
        db.commit()
    return success_response({"id": escalada_id, "resolvida": True})

@app.get("/health")
async def health():
    return {"status": "ok", "agent": "atendimento", "timestamp": datetime.utcnow().isoformat()}


# ─── MODO DEMO (terminal) ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio
    from rich.console import Console
    from rich.panel import Panel

    console = Console()
    session_id = str(uuid.uuid4())

    console.print(Panel.fit(
        "[bold blue]Agente de Atendimento — Modo Demo[/bold blue]\n"
        f"Session: {session_id}\n"
        "Digite 'sair' para encerrar",
        border_style="blue"
    ))

    async def demo():
        while True:
            msg = console.input("\n[bold green]Você:[/bold green] ")
            if msg.lower() == "sair":
                break
            console.print("[dim]Processando...[/dim]")
            resposta = await processar_mensagem(session_id, msg)
            console.print(Panel(resposta, title="[bold blue]Ana (Atendente IA)[/bold blue]", border_style="blue"))

    asyncio.run(demo())
