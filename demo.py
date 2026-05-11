"""
demo.py — Roda demonstração interativa de todos os agentes
Execute: python demo.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.columns import Columns
from rich import box

console = Console()

MENU = """
[bold]Escolha qual agente demonstrar:[/bold]

  [cyan]1[/cyan]  Atendimento ao Cliente  — chat interativo com memória e escalada
  [cyan]2[/cyan]  Analítico de Dados      — perguntas em português sobre banco de vendas
  [cyan]3[/cyan]  Processador de Docs     — extrai campos de contrato demo
  [cyan]4[/cyan]  SDR de Vendas           — qualifica lead e gera mensagem de prospecção
  [cyan]0[/cyan]  Sair
"""

async def rodar_atendimento():
    from atendimento.main import processar_mensagem
    import uuid
    session_id = str(uuid.uuid4())
    console.print(Panel.fit(
        "[bold blue]Agente de Atendimento — Ana[/bold blue]\n"
        "Experimente perguntar: status do pedido PED-001, política de troca, horário de atendimento\n"
        "[dim]Digite 'menu' para voltar[/dim]",
        border_style="blue"
    ))
    while True:
        msg = console.input("\n[bold green]Você:[/bold green] ")
        if msg.lower() == "menu":
            break
        console.print("[dim]Digitando...[/dim]")
        resp = await processar_mensagem(session_id, msg)
        console.print(Panel(resp, title="[blue]Ana[/blue]", border_style="blue"))

async def rodar_analitico():
    from analitico.main import processar_pergunta
    console.print(Panel.fit(
        "[bold cyan]Agente Analítico — DataBot[/bold cyan]\n"
        "Experimente: 'Quais os 5 produtos mais vendidos?' / 'Compare vendas por região'\n"
        "[dim]Digite 'menu' para voltar[/dim]",
        border_style="cyan"
    ))
    while True:
        pergunta = console.input("\n[bold green]Pergunta:[/bold green] ")
        if pergunta.lower() == "menu":
            break
        console.print("[dim]Analisando...[/dim]")
        resultado = await processar_pergunta(pergunta)
        console.print(Panel(resultado["resposta"], title="[cyan]DataBot[/cyan]", border_style="cyan"))
        if resultado.get("sql_executado"):
            console.print(f"[dim]SQL: {resultado['sql_executado'][:100]}[/dim]")

async def rodar_documentos():
    import json
    from documentos.main import processar_documento

    CONTRATO = """CONTRATO DE PRESTAÇÃO DE SERVIÇOS DE DESENVOLVIMENTO DE SOFTWARE
Contratante: Grupo Varejo Brasil Ltda., CNPJ 55.444.333/0001-22, Av. Comercial, 500, Rio de Janeiro - RJ.
Contratada: Dev Solutions ME, CNPJ 11.222.333/0001-44, Rua Tech, 200, São Paulo - SP.
Objeto: Desenvolvimento de plataforma de e-commerce com IA para recomendação de produtos.
Valor total: R$ 120.000,00 em 4 parcelas de R$ 30.000,00.
Prazo: 16 semanas. Início: 01/02/2025. Término previsto: 30/05/2025.
Multa por atraso da contratada: 1% ao dia sobre o valor total.
Multa rescisória: 35% do valor total contratado.
Reajuste: IPCA anual.
Exclusividade: A contratada fica impedida de prestar serviços a concorrentes do setor varejista por 12 meses.
Foro: Comarca do Rio de Janeiro."""

    console.print(Panel.fit("[bold magenta]Agente de Documentos — Processando contrato demo[/bold magenta]", border_style="magenta"))
    console.print("[dim]Processando contrato com R$ 120k...[/dim]")

    resultado = await processar_documento(CONTRATO, "contrato_varejo_brasil.txt")

    console.print(Panel(resultado["resumo"], title="[magenta]Resumo executivo[/magenta]", border_style="magenta"))

    t = Table(title="Campos Extraídos", box=box.ROUNDED)
    t.add_column("Campo", style="bold"); t.add_column("Valor")
    for k, v in resultado["campos"].items():
        if v and v != "null":
            t.add_row(str(k), str(v)[:80])
    console.print(t)

    if resultado["alertas"]:
        console.print("\n[bold red]Alertas identificados:[/bold red]")
        for a in resultado["alertas"]:
            cor = {"alto": "red", "medio": "yellow", "baixo": "blue"}.get(a.get("nivel",""), "white")
            console.print(f"  [{cor}][{a.get('nivel','').upper()}][/{cor}] {a.get('descricao','')}")

    console.input("\n[dim]Pressione Enter para voltar ao menu...[/dim]")

async def rodar_sdr():
    from sdr.main import qualificar_lead

    LEAD = {
        "nome": "Ricardo Almeida",
        "cargo": "CEO",
        "empresa": "AlmeidaCorp Distribuidora (80 funcionários)",
        "setor": "Distribuição e logística",
        "linkedin": "linkedin.com/in/ricardoalmeida",
        "contexto": "Comentou em post sobre custos crescentes com equipe de backoffice. Empresa cresceu 60% em 2024 e está contratando muito. Mencionou que processa mais de 500 pedidos/dia manualmente.",
        "canal": "linkedin"
    }

    console.print(Panel.fit(
        f"[bold yellow]Agente SDR — Qualificando lead[/bold yellow]\n"
        f"{LEAD['nome']} | {LEAD['cargo']} @ {LEAD['empresa']}",
        border_style="yellow"
    ))
    console.print("[dim]Analisando perfil e qualificando...[/dim]")

    resultado = await qualificar_lead(LEAD)

    t = Table(title=f"BANT Score: {resultado['score']}/100", box=box.ROUNDED)
    t.add_column("Critério", style="bold"); t.add_column("Status"); t.add_column("Motivo")
    for k, v in resultado["bant"].items():
        t.add_row(k.upper(), "[green]✓ Ok[/green]" if v["ok"] else "[red]✗ Não[/red]", v.get("motivo","") or "—")
    console.print(t)

    console.print(f"\n[bold]Recomendação:[/bold] [yellow]{resultado['recomendacao']}[/yellow]")
    console.print(f"[bold]Dores identificadas:[/bold] {', '.join(resultado['dores_identificadas'])}")
    console.print(Panel(
        resultado["mensagem_prospeccao"]["texto"],
        title=f"[yellow]Mensagem LinkedIn para copiar[/yellow]",
        border_style="yellow"
    ))

    console.input("\n[dim]Pressione Enter para voltar ao menu...[/dim]")

async def main():
    console.print(Panel.fit(
        "[bold]Sistema de Agentes de IA[/bold]\n"
        "4 agentes prontos para demonstrar para clientes",
        border_style="bright_white"
    ))

    handlers = {
        "1": rodar_atendimento,
        "2": rodar_analitico,
        "3": rodar_documentos,
        "4": rodar_sdr,
    }

    while True:
        console.print(MENU)
        escolha = console.input("[bold]Escolha:[/bold] ").strip()
        if escolha == "0":
            console.print("[dim]Encerrando...[/dim]")
            break
        if escolha in handlers:
            try:
                await handlers[escolha]()
            except Exception as e:
                console.print(f"[red]Erro: {e}[/red]")
                console.print("[dim]Verifique se ANTHROPIC_API_KEY está configurada no .env[/dim]")
        else:
            console.print("[red]Opção inválida[/red]")

if __name__ == "__main__":
    asyncio.run(main())
