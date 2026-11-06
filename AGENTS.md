# AGENTS.md — dbscope

Instruções para agentes que trabalharem neste repositório.

## Objetivo

Servidor MCP local, somente leitura, para MySQL/MariaDB em `dev`, `qa` e
`prod`, sem expor host, usuário, senha ou DSN ao LLM. O OpenCode chama as tools
por stdio; o dbscope é o único processo com acesso às credenciais.

## Stack

- Python 3.12, `uv`, `mcp` (SDK oficial), `pymysql`, `sqlglot`, `pydantic` v2.
- Testes com `pytest` e mocks; integração opcional fora do CI.
- `ruff` para lint, `mypy --strict` para tipos.
- Docker opcional para o modo container.

## Comandos

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy src
docker build -t dbscope:local .
./scripts/dbscope-mcp.sh
DBSCOPE_INTEGRATION_CONFIG=~/.config/dbscope/config.toml uv run pytest -m integration
```

## Estrutura

- `src/dbscope/config.py`: carrega TOML, valida ambientes, resolve segredos e
  caminhos relativos; nunca serializa senhas.
- `src/dbscope/db.py`: conexão PyMySQL, TLS, timeouts e transação read-only.
- `src/dbscope/safety.py`: parser SQL, allow/deny, redação e erros sanitizados.
- `src/dbscope/audit.py`: JSONL com rotação, sem credenciais.
- `src/dbscope/service.py`: regras das tools, limites, máscaras e auditoria.
- `src/dbscope/server.py`: tools MCP expostas por stdio.
- `src/dbscope/__main__.py`: entrypoint `python -m dbscope`.
- `tests/`: unitários com mocks; `tests/integration/` é opcional.

## Invariantes de segurança

1. Nunca retornar host, usuário, senha, DSN ou stack trace bruto ao LLM.
2. Nunca colocar credenciais em `argv`, variáveis de ambiente, logs, auditoria,
   `.env`, `opencode.json` ou no repositório.
3. Config e arquivos de senha ficam fora do repo, com permissão `600`.
4. Nenhuma tool de escrita, DDL/DML, admin ou HTTP público.
5. A leitura é garantida pela role do banco **e** por
   `START TRANSACTION READ ONLY`; o parser é defesa adicional.
6. `prod` nunca é default e usa limites mais rígidos que `dev`/`qa`.
7. Resultados são limitados por linhas, bytes e tamanho de célula; indique
   `truncated`/`truncated_cells`.
8. Erros do driver passam por `sanitize_driver_error` e redação antes de sair.
   Se a auditoria não puder ser gravada, a operação é interrompida.
9. Testes unitários não conectam em banco real; CI não usa banco real.
10. Repositório é público: use apenas placeholders em docs, testes e commits.

## Convenções de código

- Python 3.12, type hints em todas as funções públicas, `from __future__ import annotations`.
- Prefira Pydantic para validação de config; não use `dataclass` para entrada TOML.
- Mantenha mensagens de erro em português, curtas e sem dados de conexão.
- Não adicione dependências sem necessidade; atualize `uv.lock` com `uv sync`.
- Novas tools devem ser read-only e passar por `DbScopeService` + auditoria.

## Ao alterar código

1. Atualize os testes relevantes.
2. Rode `uv run ruff check .`, `uv run mypy src` e `uv run pytest`.
3. Atualize o `README.md` para refletir qualquer mudança de comportamento,
   configuração, tool ou segurança.
4. Atualize este `AGENTS.md` se mudar comandos, estrutura ou invariantes.
5. Não faça commit/push sem confirmação explícita do usuário.
