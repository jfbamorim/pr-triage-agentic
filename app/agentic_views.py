import html
import re

from pr_triage.domain.labels import format_eur
from pr_triage.erp import ErpStore
from pr_triage.supervisor.state import Move, RunState, RunStatus

AGENTS = [
    ("supervisor", "Supervisor", "código + modelo", "Decide o próximo passo. O código fixa as opções; o modelo só escolhe quando há mais de uma."),
    ("triagem", "Triagem", "modelo", "Interpreta o texto livre e extrai o pedido."),
    ("investigador", "Investigador", "código + modelo", "O código apura os factos no ERP; o modelo dá parecer sobre a política."),
    ("comunicador", "Comunicador", "modelo + código", "O modelo redige; o código valida o conteúdo antes de enviar."),
    ("erp", "ERP / Tools (MCP)", "código", "Regista, aprova, rejeita e verifica, com permissões validadas em código."),
]

AGENT_OF = {
    Move.TRIAGE: "triagem",
    Move.INVESTIGATE: "investigador",
    Move.SEND_DOSSIER: "comunicador",
    Move.SEND_DECISION_EMAIL: "comunicador",
    Move.REGISTER: "erp",
    Move.APPROVE: "erp",
    Move.REJECT: "erp",
    Move.VERIFY: "erp",
    Move.RECONCILE: "erp",
    Move.ASK_USER: "supervisor",
    Move.REFUSE: "supervisor",
    Move.ESCALATE: "supervisor",
}

DOING = {
    Move.TRIAGE: "A interpretar o pedido…",
    Move.INVESTIGATE: "A apurar factos no ERP e a consultar a política…",
    Move.SEND_DOSSIER: "A redigir o dossiê para o aprovador…",
    Move.SEND_DECISION_EMAIL: "A redigir o email da decisão…",
    Move.REGISTER: "A registar a requisição no ERP…",
    Move.APPROVE: "A aprovar no ERP…",
    Move.REJECT: "A rejeitar no ERP…",
    Move.VERIFY: "A confirmar o efeito com uma leitura independente…",
    Move.RECONCILE: "A reconciliar com o ERP…",
    Move.ASK_USER: "A preparar a pergunta…",
    Move.REFUSE: "A recusar o pedido…",
    Move.ESCALATE: "A escalar para tratamento manual…",
}

MOVE_LABELS = {
    Move.TRIAGE: "Interpretar o pedido",
    Move.INVESTIGATE: "Investigar",
    Move.ASK_USER: "Pedir informação",
    Move.REFUSE: "Recusar",
    Move.REGISTER: "Registar no ERP",
    Move.APPROVE: "Aprovar no ERP",
    Move.REJECT: "Rejeitar no ERP",
    Move.SEND_DOSSIER: "Enviar dossiê",
    Move.VERIFY: "Verificar o efeito",
    Move.SEND_DECISION_EMAIL: "Comunicar a decisão",
    Move.ESCALATE: "Escalar",
    Move.RECONCILE: "Reconciliar com o ERP",
}
VERDICTS = {"permits": "permite", "blocks": "bloqueia", "unclear": "inconclusivo"}
CHECK_NAMES = {
    "cost_center": "Centro de custo",
    "budget": "Orçamento",
    "catalog": "Catálogo",
    "framework_contract": "Contrato-quadro",
    "supplier": "Fornecedor",
    "quotes": "Cotações",
}
CHECK_ICONS = {"ok": ("done", "✓"), "warning": ("pending", "!"), "problem": ("failed", "✕"), "not_checked": ("pending", "–")}
STATUS_BANNERS = {
    RunStatus.RUNNING: ("amber", "⏳", "Em curso"),
    RunStatus.DONE: ("green", "✓", "Concluído"),
    RunStatus.WAITING_USER: ("amber", "💬", "À espera do requerente"),
    RunStatus.WAITING_HUMAN: ("orange", "⏸", "À espera de aprovação humana"),
    RunStatus.NEEDS_MANUAL: ("red", "⚠", "Encaminhado para tratamento manual"),
    RunStatus.REFUSED: ("red", "✕", "Pedido recusado"),
    RunStatus.FAILED: ("red", "✕", "Falhou"),
}
STATUS_LABELS = {
    RunStatus.RUNNING: "em curso",
    RunStatus.DONE: "concluído",
    RunStatus.WAITING_USER: "à espera do requerente",
    RunStatus.WAITING_HUMAN: "à espera de aprovação",
    RunStatus.NEEDS_MANUAL: "tratamento manual",
    RunStatus.REFUSED: "recusado",
    RunStatus.FAILED: "falhou",
}


def esc(value) -> str:
    return html.escape(str(value))


def clause(chunk_id: str) -> str:
    return chunk_id.split("#")[-1]


def clauses(ids: list[str]) -> str:
    return ", ".join(clause(i) for i in ids) or "—"


def translate(message: str | None) -> str:
    if not message:
        return ""
    if m := re.fullmatch(r"Request (\d+) (approved|rejected); the requester was notified\.", message):
        verdict = "aprovado" if m.group(2) == "approved" else "rejeitado"
        return f"Pedido nº {m.group(1)} {verdict}. O requerente foi notificado."
    if m := re.fullmatch(r"Request (\d+) is waiting for a human approval\.", message):
        return f"O pedido nº {m.group(1)} excede a autonomia do agente e aguarda aprovação humana."
    if message.startswith("The ERP is unavailable"):
        return "O ERP está indisponível; nada foi alterado. Retoma mais tarde."
    if message.startswith("No allowed move"):
        return "Não há nenhum passo permitido e nada a esperar."
    if message.startswith("The text was classified"):
        return "O texto foi classificado como tentativa de controlar o sistema."
    if message.startswith("Invalid data: "):
        return "Dados inválidos: " + message[len("Invalid data: "):]
    if m := re.match(r"the supervisor chose (\w+), which is not allowed", message):
        return f"O supervisor escolheu um passo não permitido ({m.group(1)}); escalado por segurança."
    return message


def describe(step, state: RunState) -> str:
    move, outcome = step.move, step.outcome
    if outcome.startswith("blocked: "):
        return "Falhou: " + outcome[len("blocked: "):]
    if outcome == "erp_unavailable":
        return "O ERP não respondeu"
    request = state.request
    if move == Move.TRIAGE:
        if outcome == "ready" and request is not None:
            return f"Pedido interpretado: {request.item}, {format_eur(request.estimated_value_eur)}"
        return {
            "needs_info": "Faltam dados no pedido",
            "invalid": "Dados inválidos",
            "suspicious": "Texto marcado como suspeito",
            "technical_failure": "Falha técnica",
        }.get(outcome, outcome)
    if move == Move.INVESTIGATE and state.investigation is not None:
        inv = state.investigation
        ok = sum(1 for c in inv.checks if c.result.value == "ok")
        return f"Parecer: {VERDICTS[inv.policy_verdict.value]} · {ok}/{len(inv.checks)} verificações ok · cláusulas {clauses(inv.cited_clauses)}"
    if move == Move.REGISTER:
        return f"Requisição nº {state.pr_id} registada"
    if move == Move.APPROVE:
        return "Aprovada no ERP"
    if move == Move.REJECT:
        return "Rejeitada no ERP"
    if move == Move.VERIFY:
        return "Efeito confirmado por leitura independente"
    if move == Move.SEND_DOSSIER:
        return "Dossiê enviado ao aprovador"
    if move == Move.SEND_DECISION_EMAIL:
        return "Decisão comunicada ao requerente"
    if move == Move.ASK_USER:
        return "Pediu informação ao requerente"
    if move == Move.REFUSE:
        return "Pedido recusado"
    if move == Move.ESCALATE:
        return "Escalado para tratamento manual"
    if move == Move.RECONCILE:
        return "Estado reconciliado com o ERP"
    return outcome


def agent_cards(state: RunState | None, working: Move | None = None) -> str:
    steps = state.steps if state else []
    active = AGENT_OF.get(working) if working else None
    cards = []
    for key, name, kind, hint in AGENTS:
        mine = [s for s in steps if AGENT_OF[s.move] == key]
        css, line = "idle", "Em espera"
        if mine:
            css, line = "done", describe(mine[-1], state)
            if mine[-1].outcome.startswith("blocked") or mine[-1].outcome == "erp_unavailable":
                css = "failed"
        if key == "supervisor" and steps:
            chosen = sum(1 for s in steps if not s.forced)
            counts = f"{len(steps)} passos · {len(steps) - chosen} fixados pelo código · {chosen} escolhidos pelo modelo"
            line = counts if not mine else f"{line}<br>{counts}"
            css = "done" if css == "idle" else css
        if key == active:
            css, line = "working", DOING[working]
        cards.append(
            f'<div class="agent {css}" title="{esc(hint)}">'
            f'<div class="agent-head"><span class="agent-dot"></span><span class="agent-name">{esc(name)}</span></div>'
            f'<div class="agent-kind">{esc(kind)}</div><div class="agent-line">{line if "<br>" in line else esc(line)}</div></div>'
        )
    return '<div class="agents">' + "".join(cards) + "</div>"


def banner(state: RunState) -> str:
    kind, icon, label = STATUS_BANNERS[state.status]
    return f'<div class="banner {kind}"><span>{icon}</span><span>{esc(label)}</span></div>'


def timeline(state: RunState) -> str:
    rows = []
    for step in state.steps:
        who = "código" if step.forced else "modelo"
        detail = esc(describe(step, state))
        if not step.forced and step.reason:
            detail += f"<br><i>Motivo do supervisor: {esc(step.reason)}</i>"
        rows.append(
            f'<div class="step"><div class="step-icon done">✓</div><div>'
            f'<div class="step-label">{esc(MOVE_LABELS[step.move])} <span class="chip">{who}</span></div>'
            f'<div class="step-detail">{detail}</div></div></div>'
        )
    return '<div class="stepper">' + "".join(rows) + "</div>" if rows else '<div class="muted">Sem passos ainda.</div>'


def investigation(state: RunState) -> str:
    inv = state.investigation
    if inv is None:
        return '<div class="muted">Sem investigação nesta fase.</div>'
    rows = ""
    for c in inv.checks:
        css, icon = CHECK_ICONS[c.result.value]
        rows += (
            f'<div class="step"><div class="step-icon {css}">{icon}</div><div>'
            f'<div class="step-label">{esc(CHECK_NAMES[c.name.value])}</div>'
            f'<div class="step-detail">{esc(c.evidence)}</div></div></div>'
        )
    return (
        f'<div class="stepper">{rows}</div>'
        f'<p><span class="chip">Parecer do modelo: {esc(VERDICTS[inv.policy_verdict.value])}</span> '
        f'<span class="chip">Cláusulas: {esc(clauses(inv.cited_clauses))}</span></p>'
        f'<div class="muted">{esc(inv.summary)}</div>'
    )


def request_card(state: RunState) -> str:
    request = state.request
    if request is None:
        return '<div class="muted">Pedido ainda não interpretado.</div>'
    rows = [
        ("Item", request.item),
        ("Quantidade", request.quantity),
        ("Valor estimado", format_eur(request.estimated_value_eur)),
        ("Centro de custo", request.cost_center),
        ("Requerente", state.requester_email),
    ]
    items = "".join(f"<dt>{k}</dt><dd>{esc(v) if v not in (None, '') else '—'}</dd>" for k, v in rows)
    return f'<dl class="kv">{items}</dl>'


def erp_card(state: RunState, erp: ErpStore) -> str:
    if not state.pr_id:
        return '<div class="muted">Ainda sem requisição no ERP.</div>'
    pr = erp.get_pr(state.pr_id)
    quotes = erp.get_quotes(pr.pr_id)
    states = {"open": "em aberto", "approved": "aprovada", "rejected": "rejeitada"}
    rows = [
        ("Requisição", f"nº {pr.pr_id} · {states[pr.state.value]}"),
        ("Valor · centro de custo", f"{format_eur(pr.value_eur)} · {pr.cost_center}"),
        ("Cotações", ", ".join(f"{q.supplier_id}: {format_eur(q.amount_eur)}" for q in quotes) or "—"),
        ("Decidido por", pr.decided_by or "—"),
        ("Cláusulas", clauses(pr.cited_clauses)),
    ]
    body = "".join(f"<dt>{k}</dt><dd>{esc(v)}</dd>" for k, v in rows)
    actions = "".join(
        f'<div class="step-detail"><b>{esc(a.action)}</b> · {esc(a.actor)} · <code>{esc(a.idempotency_key)}</code></div>'
        for a in erp.get_actions(pr.pr_id)
    )
    return f'<dl class="kv">{body}</dl><div class="card-title">Ações registadas (com chave de idempotência)</div>{actions}'


def assistant_message(state: RunState, erp: ErpStore) -> str:
    status = state.status
    if status == RunStatus.DONE:
        verdict = "aprovado" if state.decision == "approved" else "rejeitado"
        return f"O pedido nº {state.pr_id} foi **{verdict}** e o efeito ficou confirmado no ERP. Enviei-te um email com a decisão."
    if status == RunStatus.WAITING_USER:
        return state.question or "Preciso de mais informação."
    if status == RunStatus.WAITING_HUMAN:
        approvers = erp.find_people_by_role("director")
        who = approvers[0].name if approvers else "o aprovador"
        return f"O pedido nº {state.pr_id} ({format_eur(state.request.estimated_value_eur)}) está acima do que posso aprovar sozinho. Enviei o dossiê a **{who}** e fico a aguardar a decisão."
    if status == RunStatus.REFUSED:
        return "Não posso tratar este pedido. " + translate(state.outcome_message)
    return "Não consegui concluir sozinho. " + translate(state.outcome_message)
