# Sistema de Agentes de IA 

QSistema multi-agente construído com Python, Claude API (Anthropic) e FastAPI.
4 agentes especializados prontos para automação empresarial.

## Agentes disponíveis

```
| Agente | Descrição |
|--------|-----------|
| 💬 Atendimento | Chat inteligente com memória, FAQ e escalada para humano |
| 📊 Analítico | Responde perguntas sobre dados em português natural |
| 📄 Documentos | Extrai campos de contratos, NF-e e PDFs automaticamente |
| 🎯 SDR de Vendas | Qualifica leads (BANT) e gera mensagens personalizadas |

```

## Instalação rápida

```bash
pip install anthropic fastapi uvicorn sqlalchemy aiofiles python-dotenv rich pydantic pypdf2 pandas openpyxl
cp .env.example .env
# Edite .env com sua ANTHROPIC_API_KEY
```

## Rodar cada agente

```bash
# Atendimento
uvicorn atendimento.main:app --port 8001 --reload

# Analítico
uvicorn analitico.main:app --port 8002 --reload

# Documentos
uvicorn documentos.main:app --port 8003 --reload

# SDR
uvicorn sdr.main:app --port 8004 --reload
```

## Padrões usados em todos os agentes

- **Retry com backoff exponencial** — resiliência automática
- **Logging estruturado** — toda ação registrada em JSON
- **Timeout por chamada** — nunca trava indefinidamente
- **Limite de iterações** — proteção contra loops infinitos
- **Tratamento de erros** — fallback claro em caso de falha
- **Histórico de conversa** — memória de curto prazo

## Tecnologias

![Python](https://img.shields.io/badge/Python-3.14-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)
![Anthropic](https://img.shields.io/badge/Claude-API-orange)
![SQLite](https://img.shields.io/badge/SQLite-banco%20de%20dados-lightgrey)

**IA:** Anthropic Claude API + Tool Use + ReAct loop
- **Backend:** FastAPI + Uvicorn + Pydantic
- **Banco:** SQLite + SQLAlchemy
- **Dados:** Pandas + PyPDF2
- **Qualidade:** Retry exponencial + Logging JSON estruturado

## Como rodar

**1. Clone o repositório**
```bash
git clone https://github.com/kevcruzz/Agentes-de-IA-.git
cd Agentes-de-IA-
```

**2. Crie o ambiente virtual**
```bash
python -m venv venv
venv\Scripts\activate  # Windows
source venv/bin/activate  # Mac/Linux
```

**3. Instale as dependências**
```bash
pip install -r requirements.txt
```

**4. Configure as variáveis de ambiente**
```bash
copy .env.example .env
# Edite o .env com sua ANTHROPIC_API_KEY
```

**5. Rode a demonstração**
```bash
python demo.py
```

##  APIs disponíveis

Cada agente expõe uma API REST independente:

```bash
uvicorn atendimento.main:app --port 8001 --reload
uvicorn analitico.main:app   --port 8002 --reload
uvicorn documentos.main:app  --port 8003 --reload
uvicorn sdr.main:app         --port 8004 --reload
```
Acesse a documentação interativa em `http://localhost:800X/docs`

## Arquitetura

agentes/
├── atendimento/   # Agente de atendimento ao cliente
├── analitico/     # Agente analítico de dados
├── documentos/    # Agente processador de documentos
├── sdr/           # Agente SDR de vendas
├── shared/        # Utilitários compartilhados
├── demo.py        # Menu interativo de demonstração
└── requirements.txt

## Padrões de resiliência

Todos os agentes implementam:
- ✅ Retry com backoff exponencial (1s → 2s → 4s)
- ✅ Limite de iterações (proteção contra loops)
- ✅ Logging estruturado em JSON
- ✅ Timeout por chamada de API
- ✅ Tratamento de erros com fallback

## Autor

**Kevin da Cruz**