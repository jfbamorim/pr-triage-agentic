from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pr_triage.agents.communicator import (
    CommunicationBrief,
    EmailDraft,
    Facts,
    build_decision_brief,
    build_dossier_brief,
    check_email,
    deliver,
    numbers_in,
    write_email,
)
from pr_triage.domain.schemas import Evidence
from test_tools import decide, make, register

T1 = "02-escaloes-e-cotacoes#2.1"
T33 = "03-fornecedores-e-categorias#3.3"


def evidence(chunk_id, clause, title, text):
    return Evidence(chunk_id=chunk_id, source="x.md", document="d", clause=clause, title=title,
                    effective_date=None, text=text)


EVIDENCE = [
    evidence(T1, "2.1", "Abaixo de 1 000 €", "Requisições abaixo de 1 000 € podem ser aprovadas sem cotações."),
    evidence(T33, "3.3", "Equipamento informático", "Monitores são comprados através do catálogo de IT."),
]


def setup(tmp_path, value=800.0, approve=True):
    ctx = make(tmp_path)
    pr_id = register(ctx, value)
    if approve:
        decide(ctx, "approve_pr", pr_id, clauses=[T1, T33] if value >= 1000 else [T1])
    return ctx, pr_id


def brief_for(ctx, pr_id, clauses=None):
    pr = ctx.erp.get_pr(pr_id)
    requester = ctx.erp.get_person(pr.requester_email)
    return build_decision_brief(pr, requester, EVIDENCE, 112000.0)


GOOD = EmailDraft(
    subject="Pedido de compra aprovado",
    body="Olá Ana Silva, o seu pedido de 2 Monitores, no valor de 800 €, foi aprovado. "
         "Pela cláusula 2.1, requisições abaixo de 1 000 € podem ser aprovadas sem cotações.",
)


def scripted(*drafts, seen=None):
    def respond(messages, info: AgentInfo):
        if seen is not None:
            seen.append(info.function_tools)
        turn = sum(isinstance(m, ModelResponse) for m in messages)
        draft = drafts[min(turn, len(drafts) - 1)]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, draft.model_dump())])

    return FunctionModel(respond)


def test_number_parsing_handles_portuguese_formats():
    assert numbers_in("1 000 € e 1.000,50 e 600,00") == {1000.0, 1000.5, 600.0}


def test_a_good_decision_email_passes(tmp_path):
    ctx, pr_id = setup(tmp_path)
    assert check_email(GOOD, brief_for(ctx, pr_id)) == []


def test_the_communicator_has_no_tools(tmp_path):
    ctx, pr_id = setup(tmp_path)
    seen = []
    result = write_email(brief_for(ctx, pr_id), scripted(GOOD, seen=seen))
    assert result.failure is None and result.draft == GOOD
    assert all(tools == [] for tools in seen)


def test_a_missing_clause_citation_is_sent_back(tmp_path):
    ctx, pr_id = setup(tmp_path)
    no_clause = GOOD.model_copy(update={"body": "O pedido de 2 Monitores, de 800 €, foi aprovado."})
    result = write_email(brief_for(ctx, pr_id), scripted(no_clause, GOOD))
    assert result.draft == GOOD and len(result.rejections) == 1
    assert "clause 2.1" in result.rejections[0][0]


def test_invented_numbers_are_refused(tmp_path):
    ctx, pr_id = setup(tmp_path)
    invented = GOOD.model_copy(update={"body": GOOD.body + " Aplicámos um desconto de 15 %."})
    problems = check_email(invented, brief_for(ctx, pr_id))
    assert any("15.0" in p for p in problems)


def test_the_value_must_be_stated(tmp_path):
    ctx, pr_id = setup(tmp_path)
    vague = GOOD.model_copy(update={"body": GOOD.body.replace("800 €", "um valor baixo")})
    assert "the email must state the value of the request" in check_email(vague, brief_for(ctx, pr_id))


def test_links_and_addresses_are_refused(tmp_path):
    ctx, pr_id = setup(tmp_path)
    leaking = GOOD.model_copy(update={"body": GOOD.body + " Detalhes em https://exemplo.pt/x ou a@b.pt"})
    assert "the email must not contain links or email addresses" in check_email(leaking, brief_for(ctx, pr_id))


def test_the_text_must_agree_with_the_decision(tmp_path):
    ctx, pr_id = setup(tmp_path)
    wrong = GOOD.model_copy(update={"body": GOOD.body.replace("foi aprovado", "foi rejeitado")})
    assert any("approval" in p for p in check_email(wrong, brief_for(ctx, pr_id)))


def test_a_rejection_email_must_say_so(tmp_path):
    ctx = make(tmp_path)
    pr_id = register(ctx, 3000)
    decide(ctx, "reject_pr", pr_id, "blocks", [T33])
    brief = brief_for(ctx, pr_id)
    assert brief.decision == "rejected"
    good = EmailDraft(
        subject="Pedido de compra rejeitado",
        body="Olá Ana Silva, o pedido de 2 Monitores, de 3000 €, foi rejeitado. "
             "A cláusula 3.3 diz que os monitores são comprados através do catálogo de IT.",
    )
    assert check_email(good, brief) == []
    bad = good.model_copy(update={"body": good.body.replace("foi rejeitado", "foi aprovado")})
    assert any("rejection" in p for p in check_email(bad, brief))


def test_a_dossier_needs_the_cost_center_and_the_requester(tmp_path):
    ctx, pr_id = setup(tmp_path, value=14000.0, approve=False)
    pr = ctx.erp.get_pr(pr_id)
    brief = build_dossier_brief(
        pr, ctx.erp.get_person(pr.requester_email), ctx.erp.get_person("maria.santos@exemplo.pt"),
        EVIDENCE, [T33], ["O centro de custo tem orçamento disponível."], 112000.0,
    )
    thin = EmailDraft(subject="Aprovação necessária", body="Peço a sua decisão sobre 2 Monitores de 14 000 €, cláusula 3.3.")
    problems = check_email(thin, brief)
    assert "a dossier must name the cost center" in problems
    assert "a dossier must name the requester" in problems
    full = EmailDraft(
        subject="Aprovação necessária",
        body="Olá Maria Santos, Ana Silva pede 2 Monitores por 14 000 € no centro de custo CC-TI-100, "
             "com 112 000 € de orçamento disponível. Cláusula 3.3: compra pelo catálogo de IT. Peço a sua decisão.",
    )
    assert check_email(full, brief) == []
    assert brief.recipient_email == "maria.santos@exemplo.pt"


def test_persistent_bad_output_is_a_failure_with_the_reasons(tmp_path):
    ctx, pr_id = setup(tmp_path)
    bad = GOOD.model_copy(update={"body": "O pedido de 2 Monitores, de 800 €, foi aprovado, sem mais."})
    result = write_email(brief_for(ctx, pr_id), scripted(bad))
    assert result.draft is None and result.failure.code == "invalid_output"
    assert "must be cited" in result.failure.message and result.failure.retryable


def test_model_outage_is_retryable(tmp_path):
    ctx, pr_id = setup(tmp_path)

    def down(messages, info):
        raise ModelHTTPError(503, "gpt")

    result = write_email(brief_for(ctx, pr_id), FunctionModel(down))
    assert result.failure.code == "llm_unavailable" and result.failure.retryable


def test_delivery_goes_through_the_authorized_tool(tmp_path):
    ctx, pr_id = setup(tmp_path, approve=False)
    pr = ctx.erp.get_pr(pr_id)
    early = CommunicationBrief(
        kind="decision", recipient_email=pr.requester_email, recipient_name="Ana Silva",
        facts=Facts(pr_id=pr_id, requester_name="Ana Silva", item=pr.item, quantity=pr.quantity,
                    value_eur=pr.value_eur, cost_center=pr.cost_center),
        decision="approved",
    )
    refused = deliver(ctx, early, GOOD)
    assert refused.error.detail == "nothing_executed"
    assert ctx.erp.get_outbox() == []

    decide(ctx, "approve_pr", pr_id, clauses=[T1])
    brief = brief_for(ctx, pr_id)
    sent = deliver(ctx, brief, GOOD)
    assert sent.ok
    mail = ctx.erp.get_outbox(pr_id)[0]
    assert mail.to_addr == "ana.silva@exemplo.pt" and mail.cited_clauses == [T1]


def test_a_missing_api_key_is_a_configuration_error(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ctx, pr_id = setup(tmp_path)
    result = write_email(brief_for(ctx, pr_id))
    assert result.failure.code == "configuration_error" and not result.failure.retryable


def test_names_that_are_not_in_the_briefing_are_refused(tmp_path):
    ctx, pr_id = setup(tmp_path)
    brief = brief_for(ctx, pr_id)
    invented = GOOD.model_copy(update={"body": GOOD.body + " Aprovado por Carlos Mendes."})
    assert any("Carlos Mendes" in p for p in check_email(invented, brief))
    assert check_email(GOOD, brief) == []


def test_the_briefing_tells_who_decided(tmp_path):
    ctx, pr_id = setup(tmp_path)
    pr = ctx.erp.get_pr(pr_id)
    requester = ctx.erp.get_person(pr.requester_email)
    automatic = build_decision_brief(pr, requester, EVIDENCE)
    assert automatic.decided_by == "o sistema automático de aprovação"
    maria = ctx.erp.get_person("maria.santos@exemplo.pt")
    human = build_decision_brief(pr, requester, EVIDENCE, human_approver=maria)
    assert "Maria Santos" in human.decided_by and "Maria Santos" in human.known_names
