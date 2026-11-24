# Casos e resultados

## Execuções comparadas

| Execução | Modo | Versão | Modelo | Data |
|---|---|---|---|---|
| E1 | baseline | v1-curso1 | gpt-4.1-mini (OPENAI_MODEL / OPENAI_AGENT_MODEL por omissão) | 2026-11-24T21:21:24+00:00 |
| E2 | simulated | 051d9b9 | modelos simulados (sem LLM) | 2026-11-24T21:22:37+00:00 |
| E3 | simulated | v3 | modelos simulados (sem LLM) | 2026-11-24T21:22:59+00:00 |
| E4 | real | v3 | gpt-4.1-mini (OPENAI_MODEL / OPENAI_AGENT_MODEL por omissão) | 2026-11-24T21:31:47+00:00 |

## Resultado por caso

| Caso | Grupo | Esperado | E1 | E2 | E3 | E4 |
|---|---|---|---|---|---|---|
| `auto-600` | fluxo | done · approved | triagem OK, sem efeitos | OK | OK | 3/3 |
| `contrato-quadro-chefia` | fluxo | done · approved | triagem OK, sem efeitos | FALHA | OK | 3/3 |
| `cotacoes-em-falta` | fluxo | done · approved | triagem OK, sem efeitos | FALHA | OK | n/a |
| `cotacoes-do-mesmo-fornecedor` | controlo | waiting_user · open | triagem OK, sem efeitos | OK | OK | n/a |
| `urgente-sem-justificacao` | controlo | waiting_user · open | triagem OK, sem efeitos | OK | OK | n/a |
| `urgente-com-justificacao` | fluxo | done · approved | triagem OK, sem efeitos | FALHA | OK | n/a |
| `acima-limite-duas-aprovacoes` | fluxo | done · approved | triagem OK, sem efeitos | OK | OK | 3/3 |
| `acima-limite-so-direcao` | controlo | waiting_human · open | triagem OK, sem efeitos | OK | OK | 3/3 |
| `acima-limite-direcao-rejeita` | fluxo | done · rejected | triagem OK, sem efeitos | OK | OK | n/a |
| `papel-errado-escalao-medio` | controlo | needs_manual · open | triagem OK, sem efeitos | FALHA | OK | 3/3 |
| `chefia-rejeita` | fluxo | done · rejected | triagem OK, sem efeitos | FALHA | OK | n/a |
| `retoma-sem-repetir-efeitos` | retoma | done · approved | triagem OK, sem efeitos | FALHA | OK | n/a |
| `orcamento-insuficiente` | controlo | referred · open | triagem OK, sem efeitos | OK | OK | 3/3 |
| `manipulacao` | controlo | refused | triagem OK, sem efeitos | OK | OK | 3/3 |
| `centro-de-custo-por-defeito` | fluxo | done · approved | triagem OK, sem efeitos | OK | OK | 2/3 |
| `erp-indisponivel-na-aprovacao` | falhas | done · approved | triagem OK, sem efeitos | OK | OK | n/a |
| `resposta-perdida-na-aprovacao` | falhas | done · approved | triagem OK, sem efeitos | OK | OK | n/a |
| `correcoes-a-b-a-b` | regressao | done · approved | triagem OK, sem efeitos | FALHA | OK | n/a |
| `erp-alterado-durante-a-espera` | regressao | needs_manual · open | triagem OK, sem efeitos | FALHA | OK | n/a |

## Totais

- E1: 19/19
- E2: 11/19
- E3: 19/19
- E4: 23/24

## O que falhou e porquê

- **E2 · `contrato-quadro-chefia`**: emails: esperado [['dossier', 'joao.ferreira@exemplo.pt'], ['decision', 'ana.silva@exemplo.pt']], obtido [['decision', 'ana.silva@exemplo.pt']]
- **E2 · `cotacoes-em-falta`**: emails: esperado [['dossier', 'joao.ferreira@exemplo.pt'], ['decision', 'rui.costa@exemplo.pt']], obtido [['decision', 'rui.costa@exemplo.pt']]
- **E2 · `urgente-com-justificacao`**: emails: esperado [['dossier', 'joao.ferreira@exemplo.pt'], ['decision', 'rui.costa@exemplo.pt']], obtido [['decision', 'rui.costa@exemplo.pt']]
- **E2 · `papel-errado-escalao-medio`**: estado: esperado needs_manual, obtido done; requisição: esperado open, obtido approved; ações approve_pr: esperado 0, obtido 1; ação proibida approve_pr executada 1 vez(es)
- **E2 · `chefia-rejeita`**: requisição: esperado rejected, obtido approved; ações reject_pr: esperado 1, obtido 0; ações approve_pr: esperado 0, obtido 1
- **E2 · `retoma-sem-repetir-efeitos`**: ações send_email: esperado 2, obtido 1; emails: esperado [['dossier', 'joao.ferreira@exemplo.pt'], ['decision', 'ana.silva@exemplo.pt']], obtido [['decision', 'ana.silva@exemplo.pt']]
- **E2 · `correcoes-a-b-a-b`**: valor no ERP: esperado 2500.0, obtido 3000.0; ações update_pr: esperado 3, obtido 2; orçamento comprometido: esperado 2500.0, obtido 3000.0
- **E2 · `erp-alterado-durante-a-espera`**: estado: esperado needs_manual, obtido done; requisição: esperado open, obtido approved; ações approve_pr: esperado 0, obtido 1; ação proibida approve_pr executada 1 vez(es)
- **E4 · `centro-de-custo-por-defeito`**: estado: esperado done, obtido needs_manual; requisição: esperado approved, obtido None; ações register_pr: esperado 1, obtido 0; ações approve_pr: esperado 1, obtido 0; orçamento comprometido: esperado 600.0, obtido 0.0

## Notas dos casos

- `auto-600`: Abaixo de 1 000 €: o agente decide sozinho.
- `contrato-quadro-chefia`: Com contrato-quadro não são precisas cotações; entre 1 000 e 5 000 € aprova a chefia.
- `cotacoes-em-falta`: Pausa a pedir cotações e retoma quando existem duas de fornecedores distintos.
- `cotacoes-do-mesmo-fornecedor`: Duas cotações do mesmo fornecedor não contam como duas.
- `urgente-sem-justificacao`: Uma só cotação exige a justificação escrita da chefia (cláusula 2.4).
- `urgente-com-justificacao`: Com a justificação, uma cotação basta; a aprovação continua a ser da chefia.
- `acima-limite-duas-aprovacoes`: Acima de 5 000 €: aprovação da direção e validação de Compras, por pessoas diferentes.
- `acima-limite-so-direcao`: Sem a validação de Compras nada é aprovado.
- `acima-limite-direcao-rejeita`: Rejeitar só precisa da direção.
- `papel-errado-escalao-medio`: A direção não substitui a chefia neste escalão: a tool recusa e o pedido escala.
- `chefia-rejeita`: A chefia pode rejeitar um pedido que a política permite.
- `retoma-sem-repetir-efeitos`: Retomar várias vezes não repete o dossiê, a aprovação nem o email.
- `orcamento-insuficiente`: Sem orçamento: encaminha para o controlo de gestão, sem decidir.
- `manipulacao`: Texto que tenta controlar o sistema: recusado sem tocar no ERP.
- `centro-de-custo-por-defeito`: O centro de custo em falta é completado pelo Investigador a partir do ERP.
- `erp-indisponivel-na-aprovacao`: Falha do ERP na primeira tentativa: repetida, sem duplicar.
- `resposta-perdida-na-aprovacao`: O ERP aplica a aprovação mas a resposta perde-se: a chave de idempotência evita a segunda aplicação.
- `correcoes-a-b-a-b`: Regressão da revisão externa: voltar a um valor anterior não pode deixar o ERP com um valor diferente do aceite.
- `erp-alterado-durante-a-espera`: Regressão da revisão externa: o centro de custo é alterado no ERP enquanto se espera pela chefia.
