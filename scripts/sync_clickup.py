#!/usr/bin/env python3
"""
Sincroniza o % de progresso dos projetos vinculados ao ClickUp no index.html
do Escritório de Projetos - Binario Cloud.

Para cada projeto mapeado em PROJECTS, busca a lista correspondente no
ClickUp (API v2), calcula % = tarefas com status 'fechado' / total, e
atualiza:
  - o card na grade (card-pct e data-pct da barra)
  - o objeto de dados do modal (pct:, tag de %, e a linha "Progresso (ClickUp)")

Não mexe em nenhum outro texto (checklist, escopo, status manual, etc.) —
só nos números derivados do ClickUp.

Uso:
    CLICKUP_TOKEN=xxx python3 scripts/sync_clickup.py [--dry-run] [--file index.html]
"""
import os
import re
import sys
import json
import argparse
import urllib.request
import urllib.error

CLICKUP_API = "https://api.clickup.com/api/v2"

# key = chave do objeto de dados no index.html (ex: migstorware:{...})
# list_id = ID da lista no ClickUp
PROJECTS = {
    "migstorware":     "901328225370",
    "ceph30":          "901328082201",
    "claudiamelhoria": "901328225252",
    "obsmelhoria":     "901328225263",
    "soprema":         "901328281164",
    "senai":           "901328281738",
    "pleion":          "901328281869",
    # "psdovidro" removido intencionalmente: projeto foi finalizado manualmente
    # em 2026-08-31 por decisão do escritório de projetos, mesmo com 2 tarefas
    # ainda abertas no ClickUp. Não deve ser sobrescrito pela sincronização.
}


def fetch_list_tasks(list_id, token):
    """Busca todas as tarefas (com paginação e closed) de uma lista do ClickUp."""
    tasks = []
    page = 0
    while True:
        url = f"{CLICKUP_API}/list/{list_id}/task?include_closed=true&subtasks=true&page={page}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": token,
                "User-Agent": "escritorio-projetos-sync/1.0",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {e.code} na lista {list_id} (page {page}): {body[:300]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Erro de rede na lista {list_id} (page {page}): {e.reason}")
        page_tasks = data.get("tasks", [])
        tasks.extend(page_tasks)
        if not page_tasks or len(page_tasks) < 100:
            break
        page += 1
        if page > 20:  # trava de segurança
            break
    return tasks


def calc_pct(tasks):
    """% = tarefas com status 'closed' (fechado) / total. Retorna (pct, fechado, total)."""
    total = len(tasks)
    if total == 0:
        return 0, 0, 0
    fechado = sum(1 for t in tasks if (t.get("status", {}).get("status") or "").strip().lower() in ("fechado", "closed", "complete"))
    pct = round(100 * fechado / total)
    return pct, fechado, total


def update_card_pct(html, key, new_pct):
    """Atualiza card-pct e data-pct do card na grade, localizado via openModal('key')."""
    anchor = f"openModal('{key}')"
    idx = html.find(anchor)
    if idx == -1:
        print(f"  ⚠️  Card de '{key}' não encontrado (openModal não existe) — pulando card.")
        return html, False

    window_start = max(0, idx - 500)
    window = html[window_start:idx]

    changed = False

    # card-pct">NN%</span>  (pega a ÚLTIMA ocorrência antes do anchor)
    matches = list(re.finditer(r'card-pct">(\d+)%</span>', window))
    if matches:
        m = matches[-1]
        old_pct = m.group(1)
        if old_pct != str(new_pct):
            abs_start = window_start + m.start()
            abs_end = window_start + m.end()
            html = html[:abs_start] + f'card-pct">{new_pct}%</span>' + html[abs_end:]
            changed = True
            # reconstroi window/idx pois o html mudou de tamanho
            idx = html.find(anchor)
            window_start = max(0, idx - 500)
            window = html[window_start:idx]

    # data-pct="NN" (bar-fill, última ocorrência antes do anchor)
    matches2 = list(re.finditer(r'data-pct="(\d+)"', window))
    if matches2:
        m = matches2[-1]
        old_pct = m.group(1)
        if old_pct != str(new_pct):
            abs_start = window_start + m.start()
            abs_end = window_start + m.end()
            html = html[:abs_start] + f'data-pct="{new_pct}"' + html[abs_end:]
            changed = True

    return html, changed


def update_modal_pct(html, key, new_pct, fechado, total):
    """Atualiza pct:, a tag de % e a linha 'Progresso (ClickUp)' no objeto de dados do modal."""
    marker = f"  {key}:{{"
    idx = html.find(marker)
    if idx == -1:
        print(f"  ⚠️  Modal de '{key}' não encontrado — pulando modal.")
        return html, False

    # janela de trabalho: da chave até o início do PRÓXIMO objeto de projeto
    # (linha "  outraChave:{" no início da linha), nunca ultrapassando esse limite
    next_key_match = re.search(r"\n  [a-zA-Z0-9_]+:\{", html[idx + len(marker):])
    if next_key_match:
        window_end = idx + len(marker) + next_key_match.start()
    else:
        window_end = min(len(html), idx + 4000)
    block = html[idx:window_end]
    changed = False

    # pct:NN,  logo após title:'...'
    new_block, n = re.subn(r"(title:'[^']*',\s*pct:)(\d+)", rf"\g<1>{new_pct}", block, count=1)
    if n and new_block != block:
        changed = True
        block = new_block

    # tag de porcentagem dentro de tags:[...]  ex: ['4%','blue']  ou ['Em progresso','green'],['4%','green']
    new_block2, n2 = re.subn(r"\['(\d+)%','(blue|green|orange)'\]", lambda m: f"['{new_pct}%','{m.group(2)}']", block, count=1)
    if n2 and new_block2 != block:
        changed = True
        block = new_block2

    # linha "Progresso (ClickUp)": ['📊','NN% — A de B tarefas concluídas']
    new_block3, n3 = re.subn(
        r"\['📊','\d+% — \d+ de \d+ tarefas concluídas'\]",
        f"['📊','{new_pct}% — {fechado} de {total} tarefas concluídas']",
        block, count=1
    )
    if n3 and new_block3 != block:
        changed = True
        block = new_block3

    if changed:
        html = html[:idx] + block + html[window_end:]

    return html, changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="index.html")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = os.environ.get("CLICKUP_TOKEN")
    if not token:
        print("ERRO: variável de ambiente CLICKUP_TOKEN não definida.")
        sys.exit(1)

    if not os.path.exists(args.file):
        print(f"ERRO: arquivo {args.file} não encontrado.")
        sys.exit(1)

    with open(args.file, "r", encoding="utf-8") as f:
        html = f.read()

    any_change = False
    any_error = False
    report = []

    for key, list_id in PROJECTS.items():
        try:
            tasks = fetch_list_tasks(list_id, token)
        except Exception as e:
            print(f"  ❌ {key} (lista {list_id}): falha ao buscar tarefas — {e}")
            any_error = True
            continue

        pct, fechado, total = calc_pct(tasks)

        html, changed_card = update_card_pct(html, key, pct)
        html, changed_modal = update_modal_pct(html, key, pct, fechado, total)
        changed = changed_card or changed_modal

        status = "ATUALIZADO" if changed else "sem mudança"
        print(f"  {key}: {pct}% ({fechado}/{total}) — {status}")
        report.append({"key": key, "list_id": list_id, "pct": pct, "fechado": fechado, "total": total, "changed": changed})

        if changed:
            any_change = True

    if args.dry_run:
        print("\n[DRY-RUN] Nenhum arquivo foi escrito.")
    elif any_change:
        with open(args.file, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\n✅ {args.file} atualizado.")
    else:
        print("\nNada mudou — nenhuma escrita necessária.")

    # saída em JSON pra facilitar debug/logs do Actions
    print("\n" + json.dumps(report, ensure_ascii=False))

    if any_error:
        print("\n⚠️  Uma ou mais listas falharam ao buscar dados do ClickUp — verifique o CLICKUP_TOKEN e a conectividade.")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
