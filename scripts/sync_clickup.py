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
from datetime import datetime, timezone, timedelta

CLICKUP_API = "https://api.clickup.com/api/v2"

# key = chave do objeto de dados no index.html (ex: migstorware:{...})
# list_id = ID da lista no ClickUp
PROJECTS = {
    "migstorware":     "901328225370",
    "ceph30":          "901328082201",
    # "claudiamelhoria" e "obsmelhoria" removidos em 2026-09-15: projetos colocados
    # em HOLD manualmente (pausados). Não devem ser sobrescritos pela sincronização
    # automática enquanto estiverem em Hold — reativar aqui quando saírem do Hold.
    "soprema":         "901328281164",
    "senai":           "901328281738",
    "pleion":          "901328281869",
    "insper":          "901328340803",
    "neugebauer":      "901328018187",
    "btechcloud":      "901328972420",
    "neoviasolution":  "901328971347",
    "sopremafirewal":  "901328361858",
    "apm":             "901329011062",
    "amigoconnecting": "901329011044",
    "brainlaw":         "901329011091",
    "senaishield":      "901329087162",
    # "psdovidro" removido intencionalmente: projeto foi finalizado manualmente
    # em 2026-08-31 por decisão do escritório de projetos, mesmo com 2 tarefas
    # ainda abertas no ClickUp. Não deve ser sobrescrito pela sincronização.
    # "ceph30" (Migração Ceph) agora usa TASK_CHECKLISTS abaixo, não uma lista.
}

# key = chave do objeto de dados no index.html
# task_id = ID da tarefa no ClickUp cujo checklist define o % (resolved/total)
# OBS: "ceph30" (Migração Ceph) foi removido daqui em 2026-09-11 — o checklist da
# tarefa (0/5) não bate com o % real mostrado no Painel de Metas de Diretoria (63%),
# que aparentemente usa outra fonte/cálculo não exposta pela API. O mesmo vale para
# "desmob20" (Desmobilização da Cloud 2.0, 61% no Painel de Metas, sem checklist).
# Os dois ficam com valor fixo, sincronizado manualmente sempre que o Painel de
# Metas for consultado — não devem ser adicionados de volta sem confirmar a fonte.
TASK_CHECKLISTS = {
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


def fetch_task_checklist(task_id, token):
    """Busca uma tarefa do ClickUp e retorna a lista de itens do(s) checklist(s)."""
    url = f"{CLICKUP_API}/task/{task_id}"
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
        raise RuntimeError(f"HTTP {e.code} na tarefa {task_id}: {body[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Erro de rede na tarefa {task_id}: {e.reason}")

    items = []
    for checklist in data.get("checklists", []):
        items.extend(checklist.get("items", []))
    return items


def calc_pct_checklist(items):
    """% = itens resolvidos / total de itens do checklist. Piso de 2% (nunca 0%)."""
    total = len(items)
    if total == 0:
        return 2, 0, total
    resolvidos = sum(1 for it in items if it.get("resolved"))
    pct = round(100 * resolvidos / total)
    pct = max(pct, 2)
    return pct, resolvidos, total


def update_modal_pct_checklist(html, key, new_pct, resolvidos, total):
    """Mesma lógica do update_modal_pct, mas com texto de checklist (resolvidos/total)."""
    marker = f"  {key}:{{"
    idx = html.find(marker)
    if idx == -1:
        return html, False
    next_key_match = re.search(r"\n  [a-zA-Z0-9_]+:\{", html[idx + len(marker):])
    window_end = idx + len(marker) + next_key_match.start() if next_key_match else min(len(html), idx + 4000)
    block = html[idx:window_end]
    changed = False

    new_block, n = re.subn(r"(title:'[^']*',pct:)(\d+)", rf"\g<1>{new_pct}", block, count=1)
    if n and new_block != block:
        changed = True
        block = new_block

    new_block2, n2 = re.subn(r"\['(\d+)%','(blue|green|orange)'\]", lambda m: f"['{new_pct}%','{m.group(2)}']", block, count=1)
    if n2 and new_block2 != block:
        changed = True
        block = new_block2

    novo_texto = f"{new_pct}% — {resolvidos} de {total} itens do checklist concluídos"
    new_block3, n3 = re.subn(r"\['📊','[^']*'\]", f"['📊','{novo_texto}']", block, count=1)
    if n3 and new_block3 != block:
        changed = True
        block = new_block3

    if changed:
        html = html[:idx] + block + html[window_end:]
    return html, changed


def calc_pct(tasks):
    """
    % = (fechadas * 1.0 + em_andamento * 0.5) / total, arredondado.
    Tarefas 'em andamento' contam como progresso parcial (peso 0.5), não só as fechadas.
    Regra do piso: nenhum projeto vinculado ao ClickUp fica em 0% — o mínimo exibido é 2%
    (representa que o projeto já foi iniciado/cadastrado), e só cresce a partir daí.
    Retorna (pct, fechado, em_andamento, total).
    """
    total = len(tasks)
    if total == 0:
        return 2, 0, 0, 0
    def status_of(t):
        return (t.get("status", {}).get("status") or "").strip().lower()
    fechado = sum(1 for t in tasks if status_of(t) in ("fechado", "closed", "complete", "concluído", "concluido", "concluida", "concluída", "done"))
    em_andamento = sum(1 for t in tasks if status_of(t) in ("em andamento", "in progress", "andamento"))
    pct = round(100 * (fechado * 1.0 + em_andamento * 0.5) / total)
    pct = max(pct, 2)  # piso: nunca mostrar 0%
    return pct, fechado, em_andamento, total


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

    # regra: se % > 1, o card nunca pode ficar com pill "Não iniciado"
    if new_pct > 1:
        matches3 = list(re.finditer(r'<span class="pill pill-prog">○ Não iniciado</span>', window))
        if matches3:
            m = matches3[-1]
            abs_start = window_start + m.start()
            abs_end = window_start + m.end()
            html = html[:abs_start] + '<span class="pill pill-prog">● Em progresso</span>' + html[abs_end:]
            changed = True

    return html, changed


def update_modal_pct(html, key, new_pct, fechado, em_andamento, total):
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

    # linha "Progresso (ClickUp)": ['📊', '...'] — regex resiliente a qualquer formato de texto anterior
    andamento_txt = f" + {em_andamento} em andamento" if em_andamento else ""
    novo_texto = f"{new_pct}% — {fechado} fechadas{andamento_txt} de {total} tarefas"
    new_block3, n3 = re.subn(
        r"\['📊','[^']*'\]",
        f"['📊','{novo_texto}']",
        block, count=1
    )
    if n3 and new_block3 != block:
        changed = True
        block = new_block3

    # regra: se % > 1, o modal nunca pode ficar com pill "Não iniciado"
    if new_pct > 1:
        new_block4, n4 = re.subn(
            r'pill:\'<span class="pill pill-prog">○ Não iniciado</span>\'',
            'pill:\'<span class="pill pill-prog">● Em progresso</span>\'',
            block, count=1
        )
        if n4 and new_block4 != block:
            changed = True
            block = new_block4

    if changed:
        html = html[:idx] + block + html[window_end:]

    return html, changed


def sync_finalized_state(html, key, new_pct):
    """
    Projeto (Cliente ou Estruturante) que chega a 100% deve:
      - ganhar pill "✓ Finalizado" (no card do grid E no modal)
      - ganhar completed:'YYYY-MM-DD' (data de hoje, só na primeira vez que bate 100%)
    Isso faz o card DESAPARECER automaticamente do grid ativo (enforceFinalizedRule,
    no JS do próprio site, já esconde qualquer card com pill-done) e aparecer nas
    visões de "Projetos Finalizados" (que filtram por completed / pill Finalizado).

    Se o projeto reabrir depois (pct cai de volta pra <100), desfaz o Finalizado
    automaticamente: volta o pill pra "Em progresso" e limpa o completed.
    """
    changed = False
    br_tz = timezone(timedelta(hours=-3))
    hoje = datetime.now(br_tz).strftime("%Y-%m-%d")

    # --- CARD no grid (localizado via openModal('key')) ---
    anchor = f"openModal('{key}')"
    idx = html.find(anchor)
    if idx != -1:
        window_start = max(0, idx - 500)
        window = html[window_start:idx]
        if new_pct == 100:
            matches = list(re.finditer(r'<span class="pill pill-prog">[^<]*</span>', window))
        else:
            matches = list(re.finditer(r'<span class="pill pill-done">✓ Finalizado</span>', window))
        if matches:
            m = matches[-1]  # a ÚLTIMA ocorrência antes do anchor é a do card certo
            novo = '<span class="pill pill-done">✓ Finalizado</span>' if new_pct == 100 else '<span class="pill pill-prog">● Em progresso</span>'
            abs_start = window_start + m.start()
            abs_end = window_start + m.end()
            html = html[:abs_start] + novo + html[abs_end:]
            changed = True

    # --- MODAL (objeto de dados) ---
    marker = f"  {key}:{{"
    midx = html.find(marker)
    if midx != -1:
        next_key_match = re.search(r"\n  [a-zA-Z0-9_]+:\{", html[midx + len(marker):])
        window_end = midx + len(marker) + next_key_match.start() if next_key_match else min(len(html), midx + 4000)
        block = html[midx:window_end]
        block_changed = False

        if new_pct == 100:
            new_block, n1 = re.subn(
                r"pill:'<span class=\"pill pill-prog\">[^<]*</span>'",
                "pill:'<span class=\"pill pill-done\">✓ Finalizado</span>'",
                block, count=1
            )
            if n1 and new_block != block:
                block = new_block
                block_changed = True
            # só grava a data na PRIMEIRA vez que chega em 100% (completed:null,)
            new_block2, n2 = re.subn(r"completed:null,", f"completed:'{hoje}',", block, count=1)
            if n2 and new_block2 != block:
                block = new_block2
                block_changed = True
        else:
            new_block, n1 = re.subn(
                r"pill:'<span class=\"pill pill-done\">✓ Finalizado</span>'",
                "pill:'<span class=\"pill pill-prog\">● Em progresso</span>'",
                block, count=1
            )
            if n1 and new_block != block:
                block = new_block
                block_changed = True
            new_block2, n2 = re.subn(r"completed:'\d{4}-\d{2}-\d{2}',", "completed:null,", block, count=1)
            if n2 and new_block2 != block:
                block = new_block2
                block_changed = True

        if block_changed:
            html = html[:midx] + block + html[window_end:]
            changed = True

    return html, changed


def update_sync_timestamp(html):
    """
    Atualiza os spans #syncTimestampClientes e #syncTimestampEstruturante com a
    data/hora atual no horário de Brasília. Roda sempre, mesmo sem mudança de %,
    pra o indicador nunca ficar desatualizado. Não depende de nenhuma chamada de
    rede no navegador do visitante — o valor já vem pronto no HTML.
    """
    br_tz = timezone(timedelta(hours=-3))  # horário de Brasília (sem horário de verão)
    now = datetime.now(br_tz)

    meses = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
    data_curta = now.strftime("%d/%m/%Y")
    hora = now.strftime("%H:%M")
    texto = f"Atualizado em {data_curta}, {hora} (horário de Brasília)"

    changed = False
    for span_id in ("syncTimestampClientes", "syncTimestampEstruturante"):
        pattern = re.compile(r'(<div style="opacity:.75;" id="' + span_id + r'">)[^<]*(</div>)')
        new_html, n = pattern.subn(rf"\g<1>{texto}\g<2>", html, count=1)
        if n:
            html = new_html
            changed = True
        else:
            print(f"  ⚠️  Placeholder #{span_id} não encontrado — pulando.")
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

        pct, fechado, em_andamento, total = calc_pct(tasks)

        html, changed_card = update_card_pct(html, key, pct)
        html, changed_modal = update_modal_pct(html, key, pct, fechado, em_andamento, total)
        html, changed_fin = sync_finalized_state(html, key, pct)
        changed = changed_card or changed_modal or changed_fin

        status = "ATUALIZADO" if changed else "sem mudança"
        print(f"  {key}: {pct}% ({fechado} fechadas + {em_andamento} em andamento de {total}) — {status}")
        report.append({"key": key, "list_id": list_id, "pct": pct, "fechado": fechado, "em_andamento": em_andamento, "total": total, "changed": changed})

        if changed:
            any_change = True

    for key, task_id in TASK_CHECKLISTS.items():
        try:
            items = fetch_task_checklist(task_id, token)
        except Exception as e:
            print(f"  ❌ {key} (tarefa {task_id}): falha ao buscar checklist — {e}")
            any_error = True
            continue

        pct, resolvidos, total = calc_pct_checklist(items)

        html, changed_card = update_card_pct(html, key, pct)
        html, changed_modal = update_modal_pct_checklist(html, key, pct, resolvidos, total)
        html, changed_fin = sync_finalized_state(html, key, pct)
        changed = changed_card or changed_modal or changed_fin

        status = "ATUALIZADO" if changed else "sem mudança"
        print(f"  {key}: {pct}% ({resolvidos}/{total} itens do checklist) — {status}")
        report.append({"key": key, "task_id": task_id, "pct": pct, "resolvidos": resolvidos, "total": total, "changed": changed})

        if changed:
            any_change = True

    html, ts_changed = update_sync_timestamp(html)
    if ts_changed:
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
