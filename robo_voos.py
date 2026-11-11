import os
import re
import time
import urllib.parse
import requests
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

# =====================================================================
# PAINEL DE CONTROLE (MUDE SUAS REGRAS AQUI QUANDO QUISER!)
# =====================================================================
MINHAS_BUSCAS = [
    {
        "origem": "GIG",                   # Viagem 1: Saída
        "destino": "THE",                  # Viagem 1: Chegada
        "data_inicio": "2026-12-12",       # Início do período disponível
        "data_fim": "2027-01-31",          # Fim do período disponível
        "dias_min": 7,                     # Mínimo de 7 dias
        "dias_max": 12,                    # Máximo de 15 dias
        "preco_maximo": 1200,              # Seu limite real de preço (R$)
        "aceita_escalas": True,            # True = aceita conexões | False = só direto
        "sem_troca_aeroporto": True,       # True = proíbe mudar de aeroporto na conexão
        "passo_dias_ida": 2,               # Testa datas de ida de 2 em 2 dias
        "passo_dias_duracao": 2            # Testa durações de 2 em 2 dias (7, 9, 11, 13, 15)
    },
    {
        "origem": "SDU",                   # Viagem 2 (Opcional): Outra rota que queira vigiar
        "destino": "THE",
        "data_inicio": "2026-12-12",
        "data_fim": "2027-01-31",
        "dias_min": 7,
        "dias_max": 12,
        "preco_maximo": 1200,
        "aceita_escalas": True,
        "sem_troca_aeroporto": True,
        "passo_dias_ida": 2,
        "passo_dias_duracao": 2
    }
]

ARQUIVO_HISTORICO = "historico.txt"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def carregar_historico():
    """Lê os voos que já foram avisados antes para não repetir mensagem."""
    if not os.path.exists(ARQUIVO_HISTORICO):
        return set()
    with open(ARQUIVO_HISTORICO, "r", encoding="utf-8") as f:
        return set(linha.strip() for linha in f if linha.strip())


def salvar_no_historico(chave):
    """Anota o voo avisado no caderninho de memória."""
    with open(ARQUIVO_HISTORICO, "a", encoding="utf-8") as f:
        f.write(chave + "\n")


def enviar_telegram(mensagem):
    """Manda o alerta para o seu celular."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Aviso: TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID não configurados.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }, timeout=15)


def gerar_combinacoes_datas(inicio_str, fim_str, dias_min, dias_max, passo_ida, passo_duracao):
    """Gera todas as combinações de Ida e Volta dentro dos seus meses e duração."""
    inicio = datetime.strptime(inicio_str, "%Y-%m-%d")
    fim = datetime.strptime(fim_str, "%Y-%m-%d")
    combinacoes = []

    data_ida = inicio
    while data_ida <= fim:
        for duracao in range(dias_min, dias_max + 1, passo_duracao):
            data_volta = data_ida + timedelta(days=duracao)
            if data_volta <= fim:
                combinacoes.append((
                    data_ida.strftime("%Y-%m-%d"),
                    data_volta.strftime("%Y-%m-%d"),
                    duracao
                ))
        data_ida += timedelta(days=passo_ida)
    return combinacoes


def analisar_voos_na_pagina(page, origem, destino, ida, volta, regra):
    """Abre o Google Flights na data escolhida e aplica os 5 filtros."""
    consulta = f"Flights from {origem} to {destino} on {ida} through {volta}"
    url = f"https://www.google.com/travel/flights?q={urllib.parse.quote(consulta)}&curr=BRL&hl=pt-BR"

    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    time.sleep(4)

    # Se aparecer tela de cookies do Google na primeira abertura, clica em aceitar
    for texto_botao in ["Aceitar tudo", "Accept all", "Concordo"]:
        botao = page.locator(f"button:has-text('{texto_botao}')")
        if botao.count() > 0:
            botao.first.click()
            time.sleep(2)
            break

    # Lê os cartões de resultados de voos na página
    cartoes = page.locator("li").all()
    ofertas_aprovadas = []

    for cartao in cartoes:
        texto = cartao.inner_text()
        if "R$" not in texto:
            continue

        # 1. Extrai o preço em Reais (Ex: R$ 3.450 -> 3450)
        achou_preco = re.search(r"R\$\s*([\d\.]+)", texto)
        if not achou_preco:
            continue
        preco_texto = achou_preco.group(1).replace(".", "")
        if not preco_texto.isdigit():
            continue
        preco = int(preco_texto)

        # FILTRO 1: Preço dentro do limite máximo?
        if preco > regra["preco_maximo"] or preco < 100:
            continue

        # 2. Verifica escalas (voo direto vs com paradas)
        texto_minusculo = texto.lower()
        eh_direto = ("direto" in texto_minusculo) or ("sem escalas" in texto_minusculo) or ("nonstop" in texto_minusculo)

        # FILTRO 4: Se você NÃO aceita escalas e o voo não é direto, descarta
        if not regra["aceita_escalas"] and not eh_direto:
            continue

        # FILTRO 5: Sem troca de aeroporto na conexão
        # O Google Flights avisa "Troca de aeroporto" / "Change of airport" no cartão quando há mudança
        trocou_aeroporto = (
            "troca de aeroporto" in texto_minusculo
            or "change of airport" in texto_minusculo
            or "mudança de aeroporto" in texto_minusculo
        )
        if regra["sem_troca_aeroporto"] and trocou_aeroporto:
            continue

        resumo_escala = "Voo Direto" if eh_direto else "Com escala (mesmo aeroporto)"
        ofertas_aprovadas.append({
            "preco": preco,
            "escala": resumo_escala,
            "link": url
        })

    # Ordena do mais barato para o mais caro e pega o melhor daquela data
    ofertas_aprovadas.sort(key=lambda x: x["preco"])
    return ofertas_aprovadas[:1]


def main():
    historico = carregar_historico()

    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True)
        contexto = navegador.new_context(locale="pt-BR")
        pagina = contexto.new_page()

        for regra in MINHAS_BUSCAS:
            janelas = gerar_combinacoes_datas(
                regra["data_inicio"],
                regra["data_fim"],
                regra["dias_min"],
                regra["dias_max"],
                regra.get("passo_dias_ida", 2),
                regra.get("passo_dias_duracao", 2)
            )

            print(f"Iniciando busca {regra['origem']} -> {regra['destino']} ({len(janelas)} combinações)...")

            for ida, volta, duracao in janelas:
                try:
                    melhores = analisar_voos_na_pagina(
                        pagina, regra["origem"], regra["destino"], ida, volta, regra
                    )
                    for voo in melhores:
                        chave = f"{regra['origem']}-{regra['destino']}_{ida}_{volta}_{voo['preco']}"
                        if chave in historico:
                            continue

                        mensagem = (
                            f"🚨 *PASSAGEM BARATA ENCONTRADA!*\n\n"
                            f"✈️ *Rota:* {regra['origem']} ➔ {regra['destino']}\n"
                            f"📅 *Ida:* {ida}\n"
                            f"📅 *Volta:* {volta} ({duracao} dias)\n"
                            f"🔄 *Conexão:* {voo['escala']}\n"
                            f"💰 *Preço:* R$ {voo['preco']}\n\n"
                            f"🔗 [Ver voo no Google Flights]({voo['link']})"
                        )
                        enviar_telegram(mensagem)
                        salvar_no_historico(chave)
                        historico.add(chave)
                        print(f"Alerta enviado: {chave}")
                except Exception as erro:
                    print(f"Erro ao checar {ida} a {volta}: {erro}")

        navegador.close()


if __name__ == "__main__":
    main()
