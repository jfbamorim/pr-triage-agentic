import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from agentic_runtime import get_runtime
from agentic_views import (
    STATUS_LABELS,
    agent_cards,
    assistant_message,
    banner,
    erp_card,
    investigation,
    request_card,
    timeline,
)
from pr_triage.domain.labels import format_eur
from pr_triage.erp.models import Role
from pr_triage.supervisor.actions import record_human_decision, record_quote, record_urgency_justification
from pr_triage.supervisor.moves import pending_roles, quotes_short
from pr_triage.supervisor.state import RunStatus

CSS_PATH = Path(__file__).parent / "style.css"
APPROVERS = {
    "Direção · Maria Santos": ("maria.santos@exemplo.pt", "director"),
    "Compras · Pedro Alves": ("pedro.alves@exemplo.pt", "purchasing"),
    "Chefia · João Ferreira": ("joao.ferreira@exemplo.pt", "department_head"),
}
REQUESTER_ROLE, APPROVER_ROLE = "Requerente", "Aprovador"
ROLE_NAMES = {"director": "Direção", "purchasing": "Compras", "department_head": "Chefia"}
AVATARS = {"user": "🧑", "assistant": "🤖", "approver": "🧑‍💼"}
SCENARIOS = [
    ("Aprovação automática", "ana.silva@exemplo.pt", "Preciso de 2 monitores para a equipa, cerca de 600 euros",
     "Abaixo de 1 000 €: o agente decide sozinho e o efeito é verificado no ERP."),
    ("Contrato-quadro", "ana.silva@exemplo.pt", "Preciso de 5 monitores, total 1445 euros",
     "Há contrato-quadro em vigor, por isso não são precisas cotações; a chefia aprova."),
    ("Cotações em falta", "rui.costa@exemplo.pt", "Preciso de 1 consultoria de 2500 euros, 10 dias, âmbito: auditoria aos processos de faturação, aceitação por Rui Costa, centro de custo CC-FAC-200",
     "Pausa e pede cotações; retoma quando forem registadas e a chefia aprova."),
    ("Acima do limite", "ana.silva@exemplo.pt", "Preciso de 10 portáteis, 14000 euros",
     "Acima de 5 000 €: precisa da aprovação da direção e da validação de Compras."),
    ("Urgência", "rui.costa@exemplo.pt", "Preciso com urgência de 1 consultoria de 2000 euros, 5 dias, âmbito: apoio a uma auditoria legal, aceitação por Rui Costa, centro de custo CC-FAC-200",
     "Com urgência, uma cotação basta se a chefia justificar por escrito (cláusula 2.4); a chefia aprova."),
    ("Memória", "tiago.lopes@exemplo.pt", "Preciso de 3 monitores, 900 euros",
     "Sem centro de custo: sugere o último usado, com origem e validade."),
    ("Orçamento insuficiente", "sofia.matos@exemplo.pt", "Preciso de 5 monitores, 1445 euros",
     "Sem orçamento no centro de custo: encaminha o pedido para o controlo de gestão."),
]


def queue(kind: str, **args) -> None:
    st.session_state.pending = (kind, args)


def use_scenario(email: str, text: str) -> None:
    st.session_state.role = REQUESTER_ROLE
    st.session_state.email = email
    queue("start", text=text, email=email)


def open_run(run_id: str) -> None:
    st.session_state.run_id = run_id


def new_mails(runtime, state) -> list[dict]:
    if not state.pr_id:
        return []
    seen = st.session_state.setdefault("seen_mails", {}).setdefault(state.run_id, set())
    mails = []
    for mail in runtime.erp.get_outbox(state.pr_id):
        if mail.id not in seen:
            seen.add(mail.id)
            mails.append({"to": mail.to_addr, "subject": mail.subject, "body": mail.body})
    return mails


def say(run_id: str, role: str, text: str, mails: list[dict] | None = None) -> None:
    st.session_state.setdefault("chat", {}).setdefault(run_id, []).append(
        {"role": role, "text": text, "mails": mails or []}
    )


def ensure_chat(runtime, state) -> list[dict]:
    chat = st.session_state.setdefault("chat", {})
    if state.run_id not in chat:
        chat[state.run_id] = [{"role": "user", "text": text, "mails": []} for text in state.messages]
        say(state.run_id, "assistant", assistant_message(state, runtime.erp), new_mails(runtime, state))
    return chat[state.run_id]


def render_message(entry: dict) -> None:
    with st.chat_message(entry["role"], avatar=AVATARS[entry["role"]]):
        st.markdown(entry["text"])
        for mail in entry["mails"]:
            with st.expander(f"✉ Email para {mail['to']}: {mail['subject']}"):
                st.write(mail["body"])


def run_pending(runtime, boxes) -> None:
    kind, args = st.session_state.pop("pending")
    run_id = args.get("run_id") or st.session_state.get("run_id")

    def draw(state, working=None):
        boxes.markdown(agent_cards(state, working), unsafe_allow_html=True)

    orchestrator = runtime.orchestrator(
        observer=lambda state: draw(state), on_step=lambda state, choice: draw(state, choice.move)
    )
    if kind == "start":
        with st.chat_message("user", avatar=AVATARS["user"]):
            st.markdown(args["text"])
        with st.chat_message("assistant", avatar=AVATARS["assistant"]):
            st.markdown("A tratar do pedido…")
        state = orchestrator.start(args["text"], args["email"])
        st.session_state.run_id = run_id = state.run_id
        say(run_id, "user", args["text"])
    elif kind == "reply":
        say(run_id, "user", args["text"])
        state = orchestrator.reply(run_id, args["text"])
    elif kind == "quote":
        current = runtime.store.load(run_id)
        record_quote(runtime.erp, current, args["supplier_id"], args["amount"])
        say(run_id, "user", f"Registei uma cotação de {format_eur(args['amount'])} do fornecedor {args['supplier_name']}.")
        state = orchestrator.resume(run_id)
    elif kind == "justify":
        current = runtime.store.load(run_id)
        record_urgency_justification(runtime.erp, current, args["approver"], args["text"])
        person = runtime.erp.get_person(args["approver"])
        say(run_id, "approver", f"{person.name} (Chefia) justificou a urgência por escrito: «{args['text']}»")
        state = orchestrator.resume(run_id)
        st.session_state.run_id = run_id
    elif kind == "decision":
        current = runtime.store.load(run_id)
        record_human_decision(runtime.erp, current, args["approver"], args["approve"])
        person = runtime.erp.get_person(args["approver"])
        verdict = "validou" if person.role.value == "purchasing" else ("aprovou" if args["approve"] else "rejeitou")
        say(run_id, "approver", f"{person.name} ({ROLE_NAMES[person.role.value]}) {verdict} o pedido nº {current.pr_id}.")
        state = orchestrator.resume(run_id)
        st.session_state.run_id = run_id
    else:
        state = orchestrator.resume(run_id)
    say(run_id, "assistant", assistant_message(state, runtime.erp), new_mails(runtime, state))


def sidebar(runtime) -> None:
    st.radio("Agir como", [REQUESTER_ROLE, APPROVER_ROLE], key="role", horizontal=True)
    people = {p.email: p for p in runtime.erp.find_people_by_role(Role.REQUESTER.value)}
    if st.session_state.role == REQUESTER_ROLE:
        st.selectbox("Requerente", list(people), key="email", format_func=lambda e: people[e].name)

    st.markdown('<div class="card-title side-title">Cenários</div>', unsafe_allow_html=True)
    for title, email, text, shows in SCENARIOS:
        st.button(title, key=f"sc-{title}", on_click=use_scenario, args=(email, text), use_container_width=True)
        st.caption(shows)

    st.markdown('<div class="card-title side-title">Pedidos recentes</div>', unsafe_allow_html=True)
    for state in runtime.store.list_recent(6):
        item = state.request.item if state.request and state.request.item else state.messages[0][:24]
        st.button(
            f"{item} · {STATUS_LABELS[state.status]}",
            key=f"run-{state.run_id}",
            on_click=open_run,
            args=(state.run_id,),
            use_container_width=True,
        )


def quote_form(runtime, state) -> None:
    suppliers = [s for s in runtime.erp.list_suppliers() if s.approved]
    with st.chat_message("assistant", avatar=AVATARS["assistant"]):
        st.markdown("Regista as cotações que recebeste:")
        supplier = st.selectbox("Fornecedor", suppliers, key="quote_supplier", format_func=lambda s: f"{s.name}")
        amount = st.number_input("Valor (€)", min_value=0.0, step=50.0, key="quote_amount")
        st.button(
            "Registar cotação e retomar",
            disabled=amount <= 0,
            on_click=queue,
            args=("quote",),
            kwargs={"supplier_id": supplier.id, "supplier_name": supplier.name, "amount": amount},
        )


def conversation(runtime, state) -> None:
    if state is None:
        st.markdown(
            '<div class="muted">Escreve um pedido na caixa em baixo ou escolhe um cenário na barra lateral.</div>',
            unsafe_allow_html=True,
        )
        return
    for entry in ensure_chat(runtime, state):
        render_message(entry)
    if state.status == RunStatus.WAITING_USER and state.investigation is not None and quotes_short(state):
        quote_form(runtime, state)
    elif state.status in (RunStatus.RUNNING, RunStatus.FAILED):
        st.button("Retomar", on_click=queue, args=("resume",))


def approvals(runtime) -> None:
    who = st.radio("Aprovador", list(APPROVERS), key="approver", horizontal=True, label_visibility="collapsed")
    email, role = APPROVERS[who]
    if role == "department_head":
        urgencies(runtime, email)
    mine = []
    for state in runtime.store.list_recent(30):
        if state.status != RunStatus.WAITING_HUMAN:
            continue
        pending = pending_roles(
            state, runtime.erp.get_human_approval(state.pr_id), runtime.erp.get_purchasing_validation(state.pr_id)
        )
        if role in pending:
            mine.append((state, pending))
    if not mine:
        st.markdown('<div class="muted">Nenhum pedido à espera da tua decisão.</div>', unsafe_allow_html=True)
    for state, pending in mine:
        dossier = next((m for m in runtime.erp.get_outbox(state.pr_id) if m.kind == "dossier"), None)
        with st.container(border=True):
            st.markdown(
                f'<div class="card-title">Pedido nº {state.pr_id} · {state.request.item} · '
                f"{format_eur(state.request.estimated_value_eur)}</div>",
                unsafe_allow_html=True,
            )
            others = [ROLE_NAMES[r] for r in pending if r != role]
            if others:
                st.caption("Também falta: " + ", ".join(others))
            elif role == "purchasing":
                st.caption("A direção já aprovou; falta a validação de Compras.")
            if dossier:
                st.write(dossier.body)
            left, right, _ = st.columns([1, 1, 4])
            left.button(
                "Validar" if role == "purchasing" else "Aprovar",
                key=f"ap-{state.run_id}-{role}",
                type="primary",
                on_click=queue,
                args=("decision",),
                kwargs={"run_id": state.run_id, "approve": True, "approver": email},
            )
            if role != "purchasing":
                right.button(
                    "Rejeitar",
                    key=f"rj-{state.run_id}",
                    on_click=queue,
                    args=("decision",),
                    kwargs={"run_id": state.run_id, "approve": False, "approver": email},
                )


def urgencies(runtime, email: str) -> None:
    waiting = [
        s for s in runtime.store.list_recent(30)
        if s.status == RunStatus.WAITING_USER and s.investigation is not None and s.investigation.urgency_unjustified
    ]
    if not waiting:
        st.markdown('<div class="muted">Nenhuma urgência à espera de justificação.</div>', unsafe_allow_html=True)
    for state in waiting:
        with st.container(border=True):
            st.markdown(
                f'<div class="card-title">Pedido nº {state.pr_id} · {state.request.item} · '
                f"{format_eur(state.request.estimated_value_eur)} · urgente</div>",
                unsafe_allow_html=True,
            )
            st.caption("Cláusula 2.4: com urgência, basta uma cotação se a chefia justificar por escrito.")
            text = st.text_area("Justificação", key=f"why-{state.run_id}", height=80)
            st.button(
                "Justificar urgência",
                key=f"jf-{state.run_id}",
                type="primary",
                disabled=not text.strip(),
                on_click=queue,
                args=("justify",),
                kwargs={"run_id": state.run_id, "approver": email, "text": text.strip()},
            )


def how_it_decided(runtime, state) -> None:
    if state is None:
        st.markdown('<div class="muted">Ainda não há nenhum pedido.</div>', unsafe_allow_html=True)
        return
    left, right = st.columns([3, 2], gap="medium")
    with left:
        with st.container(border=True):
            st.markdown('<div class="card-title">Verificações e parecer</div>', unsafe_allow_html=True)
            st.markdown(investigation(state), unsafe_allow_html=True)
        with st.container(border=True):
            st.markdown('<div class="card-title">Passos do supervisor</div>', unsafe_allow_html=True)
            st.markdown(timeline(state), unsafe_allow_html=True)
    with right:
        with st.container(border=True):
            st.markdown('<div class="card-title">Pedido</div>', unsafe_allow_html=True)
            st.markdown(request_card(state), unsafe_allow_html=True)
        with st.container(border=True):
            st.markdown('<div class="card-title">Estado no ERP</div>', unsafe_allow_html=True)
            st.markdown(erp_card(state, runtime.erp), unsafe_allow_html=True)


def memory(runtime, email: str) -> None:
    entries = runtime.memory.history(email)
    st.markdown(f"Memória guardada para **{email}**")
    if not entries:
        st.markdown('<div class="muted">Sem memória guardada.</div>', unsafe_allow_html=True)
    for e in entries:
        origin = f"pedido nº {e.origin_pr_id}" if e.origin_pr_id else e.source
        st.markdown(f"`{e.key}` = **{e.value}** · {e.status} · origem: {origin} · válida até {e.valid_until:%d/%m/%Y}")
    if entries:
        st.button("Esquecer centro de custo", on_click=lambda: runtime.memory.forget(email, "cost_center"))


def main() -> None:
    st.set_page_config(page_title="PR Triage Agentic", page_icon="🧾", layout="wide")
    st.html(f"<style>{CSS_PATH.read_text()}</style>")
    st.markdown(
        '<div class="topbar"><div class="topbar-icon">🧾</div>'
        '<div><div class="topbar-title">PR Triage Agentic</div>'
        '<div class="muted">O modelo propõe, o Python decide.</div></div></div>',
        unsafe_allow_html=True,
    )
    if not os.getenv("OPENAI_API_KEY"):
        st.error("Falta a OPENAI_API_KEY no ficheiro .env.")
        return

    runtime = get_runtime()
    st.session_state.setdefault("role", REQUESTER_ROLE)
    with st.sidebar:
        sidebar(runtime)

    run_id = st.session_state.get("run_id")
    state = runtime.store.load(run_id) if run_id else None
    boxes = st.empty()
    boxes.markdown(agent_cards(state), unsafe_allow_html=True)

    if "pending" in st.session_state:
        run_pending(runtime, boxes)
        st.rerun()

    if state is not None:
        st.markdown(banner(state), unsafe_allow_html=True)

    first = "Conversa" if st.session_state.role == REQUESTER_ROLE else "Aprovações"
    tab_main, tab_how, tab_memory = st.tabs([first, "Como decidiu", "Memória"])
    with tab_main:
        if st.session_state.role == REQUESTER_ROLE:
            conversation(runtime, state)
        else:
            approvals(runtime)
    with tab_how:
        how_it_decided(runtime, state)
    with tab_memory:
        memory(runtime, state.requester_email if state else st.session_state.get("email", "ana.silva@exemplo.pt"))

    if st.session_state.role == REQUESTER_ROLE:
        waiting = state is not None and state.status == RunStatus.WAITING_USER
        prompt = st.chat_input("Responde ao assistente…" if waiting else "Descreve o que precisas…")
        if prompt:
            if waiting:
                queue("reply", text=prompt)
            else:
                queue("start", text=prompt, email=st.session_state.get("email", "ana.silva@exemplo.pt"))
            st.rerun()


main()
