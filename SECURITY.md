# Security policy

## Reporting

Não abra issue pública com credenciais, hosts internos, dumps de dados ou
detalhes exploráveis. Reporte de forma privada ao mantenedor do repositório e
inclua apenas o mínimo necessário para reproduzir o problema.

## Modelo de ameaça

O agente/LLM pode ler arquivos, executar shell e chamar ferramentas MCP. Ele vê
o prompt e o retorno das tools. Se uma credencial chegar ao contexto do modelo,
ela pode ser registrada ou exfiltrada. O dbscope assume que:

- o agente não deve receber host, usuário, senha, DSN ou stack trace bruto;
- o banco pode ser induzido a consultar dados além do esperado por prompt
  injection;
- o controle definitivo é a role do banco, complementada por sessão read-only,
  parser, limites, máscaras e auditoria.

## Invariantes

1. Credenciais vivem fora do repositório, em arquivo `600` ou keyring.
2. Nenhuma credencial em `argv`, variáveis de ambiente, logs, auditoria,
   `opencode.json` ou git.
3. Todas as tools são somente leitura. Não há DDL/DML/admin.
4. `prod` nunca é default e não pode desabilitar TLS.
5. Resultados têm limite de linhas, bytes e tamanho de célula.
6. Erros do driver são redigidos antes de retornar ao LLM.
7. O transporte é stdio; não há porta HTTP pública.
8. O CI não usa banco real.

## Limitações conhecidas

- O resultado de uma consulta vai para o LLM. Use `mask_columns` apenas como
  apoio; views e grants restritos são o controle real.
- `mask_columns` casa pelo nome da coluna de saída e pode ser contornado por
  aliases.
- Timeout é melhor esforço e depende da versão do MySQL/MariaDB.
- Container não é uma fronteira rígida quando o agente pode ler a config ou
  controlar o Docker. Para isolamento forte, separe o usuário de SO ou use
  container rootless sob outro usuário.

## Hardening recomendado

- Role dedicada por ambiente, apenas `SELECT` nos objetos necessários.
- `prod` apontando para réplica, com `allowed_schemas` e limites menores.
- TLS `verify-ca`/`verify-identity` com CA interna.
- Config fora do repo, `chmod 600`, pasta `700`.
- OpenCode com `permission.external_directory` negando as pastas de config e
  auditoria.
- Auditoria em arquivo local com rotação e revisão periódica.
- Rotação de senhas e remoção de grants não usados.
