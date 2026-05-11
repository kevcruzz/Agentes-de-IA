# Sistema de Agentes de IA — Template Profissional

Quatro agentes prontos para demonstrar e vender como serviço.

## Estrutura

```
agentes/
├── atendimento/     # Agente de atendimento ao cliente (chat/WhatsApp)
├── analitico/       # Agente analítico de dados (SQL em linguagem natural)
├── documentos/      # Agente processador de documentos (PDF, NF-e, contratos)
├── sdr/             # Agente SDR de vendas (prospecção e qualificação)
└── shared/          # Utilitários compartilhados (log, retry, config)
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
