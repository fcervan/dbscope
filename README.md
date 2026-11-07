# dbscope

[![CI](https://github.com/fcervan/dbscope/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/fcervan/dbscope/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-stdio-blueviolet)](https://modelcontextprotocol.io/)
[![MySQL/MariaDB](https://img.shields.io/badge/MySQL%2FMariaDB-read--only-orange)](https://www.mysql.com/)

Servidor MCP local, somente leitura, para consultar MySQL/MariaDB nos ambientes
`dev`, `qa` e `prod` sem entregar host, usuário, senha ou DSN ao modelo LLM.

O OpenCode fala com o dbscope via **stdio**. O dbscope é o único processo que
carrega as credenciais e conversa com o banco:

```text
OpenCode / LLM  --stdio/MCP-->  dbscope (container ou processo local)  -->  MySQL/MariaDB
       ^                                      |
       |                                      +-- config.toml 600 fora do repo
       +-- recebe SQL, schema, erro e linhas; nunca credenciais
```

## Por que isso existe

Um agente de programação vê o prompt e **tudo que as ferramentas retornam**.
Se o MCP devolver uma senha, um DSN ou um erro com credenciais, isso entra no
contexto do LLM e pode ser registrado, repetido ou exfiltrado. O dbscope expõe
apenas SQL, metadados, erros sanitizados e resultados limitados.

## Ferramentas

| Ferramenta | O que faz |
| --- | --- |
| `list_environments` | Lista nomes, descrição, limites e se é produção. Não retorna host/usuário/database. |
| `list_schemas(environment?)` | Lista schemas acessíveis. Sem ambiente, usa o padrão (`dev` por default). |
| `list_tables(environment?, schema)` | Lista tabelas e views do schema. |
| `describe_table(environment?, schema, table)` | Colunas, tipos, defaults, chaves e índices. |
| `query(environment?, sql, max_rows?)` | Executa **um** `SELECT` (incluindo `WITH ... SELECT`/CTE) somente leitura. |
| `explain(environment?, sql)` | Executa `EXPLAIN` de um `SELECT` validado. |

Regras:

- `environment` é explícito; quando omitido, usa o ambiente padrão (`dev` por
  default); **prod nunca é default**.
- Apenas um statement por chamada, com no máximo 20 KB. DML/DDL, `SELECT ... INTO`,
  locks, funções perigosas, comentários executáveis e schemas de sistema são
  recusados.
- `prod` usa a réplica e limites mais rígidos: 100 linhas, 256 KB e timeout de 3 s.
- `dev`/`qa`: 200 linhas, 1 MB e timeout de 10 s. O `max_rows` pedido nunca
  ultrapassa o teto do ambiente.
- A garantia de leitura vem do banco (role read-only) **e** da sessão
  (`START TRANSACTION READ ONLY`), não só do parser.

## Requisitos

- Python 3.12 e [uv](https://docs.astral.sh/uv/) para desenvolvimento local.
- Docker (recomendado para o modo container).
- MySQL 5.7+/8.x ou MariaDB 10.x.
- Conectividade de rede até os bancos, quando aplicável.

## Instalação

```bash
git clone <url-do-repositorio> dbscope
cd dbscope
uv sync --extra dev
```

## Configuração e segredos

O arquivo de configuração fica **fora do repositório**:

```bash
mkdir -p ~/.config/dbscope/secrets ~/.config/dbscope/certs
chmod 700 ~/.config/dbscope ~/.config/dbscope/secrets ~/.config/dbscope/certs
cp config.example.toml ~/.config/dbscope/config.toml
chmod 600 ~/.config/dbscope/config.toml
```

Preencha os placeholders. O dbscope valida todos os ambientes na
inicialização; remova do TOML os ambientes que não usa ou crie os arquivos de
senha/CA correspondentes. Caminhos relativos no TOML são resolvidos a partir da
pasta que contém o `config.toml` — a mesma config funciona localmente e no
container montado em `/config`.

Fontes de senha (escolha exatamente uma por ambiente):

```toml
password_file = "secrets/dev.pass"  # recomendado
# password = "..."                  # inline, permitido só no arquivo 600
# use_keyring = true                # requer o extra keyring
```

```bash
printf '%s' 'sua-senha' > ~/.config/dbscope/secrets/dev.pass
chmod 600 ~/.config/dbscope/secrets/dev.pass
```

`use_keyring` é útil na execução local; no container prefira `password_file`,
pois o keyring do host não é montado.

Nunca coloque host, usuário, senha ou database no `opencode.json`, em `.env`
versionado, em `argv` ou no README. O `.gitignore` cobre os arquivos usuais,
mas a config deve continuar fora do repositório.

### TLS

`sslmode` aceita:

- `disable` — sem TLS (recusado em prod);
- `require` — exige TLS, sem validar certificado;
- `verify-ca` — valida o certificado contra `ssl_ca` (ou CAs do sistema);
- `verify-identity` — valida certificado e hostname; exige `ssl_ca`.

O padrão é `verify-ca`. Para produção, use `verify-ca`/`verify-identity` e
mantenha a CA em `certs/`.

### Role read-only

O dbscope espera um usuário de banco só com `SELECT` (e metadados). Exemplo
para o DBA, com placeholders:

```sql
CREATE USER 'dbscope_ro_dev'@'%' IDENTIFIED BY '<senha-forte>';
GRANT SELECT ON app_dev.* TO 'dbscope_ro_dev'@'%';
-- repita por ambiente; em prod, aponte para a réplica
```

O dbscope também fixa a sessão em leitura e usa `START TRANSACTION READ ONLY`,
mas a role é a barreira principal. Views e grants mais restritos são a forma
recomendada de limitar colunas sensíveis.

`allowed_schemas` é opcional e vem vazio por padrão: quando vazio, o controle é
o RBAC do banco. Quando preenchido, funciona como uma segunda barreira contra
prompt injection, restringindo também as tabelas referenciadas no SQL.

## Rodar localmente

```bash
DBSCOPE_CONFIG=~/.config/dbscope/config.toml uv run python -m dbscope
```

O processo fala MCP por stdio e fica aguardando o cliente. Para uso real, o
OpenCode inicia o comando. A config precisa ter permissão `600`; caso contrário
o processo recusa iniciar.

## Rodar em container

```bash
docker build -t dbscope:local .
./scripts/dbscope-mcp.sh
```

O wrapper:

- monta **apenas** a pasta da config em `/config` (read-only);
- monta a pasta de auditoria em `/audit`;
- não monta o workspace do projeto;
- roda com o UID/GID do seu usuário para ler a config `600` e gravar a auditoria;
- usa `--network host` por padrão para o container compartilhar a rede do host
  e alcançar os bancos conforme a infraestrutura.

Se o seu Docker for rootless ou o `--network host` não alcançar os bancos,
ajuste `DBSCOPE_NETWORK` ou rode o MCP nativamente sob um usuário de SO separado.

## Configurar no OpenCode

1. Copie `opencode.example.json` para o seu config global
   (`~/.config/opencode/opencode.json`) ou de projeto, preservando campos que
   já existem.
2. Troque `/absolute/path/to/dbscope` pelo caminho absoluto do repositório.
3. Reinicie o OpenCode — a config é carregada só na inicialização.

O exemplo configura `mcp.dbscope` e nega leitura de `~/.config/dbscope/**` e
`~/.local/state/dbscope/**` via `permission.external_directory`. Isso é defesa
em profundidade; se o agente roda como o mesmo usuário e controla Docker, a
fronteira real precisa ser um usuário de SO separado ou container rootless sob
outro usuário. Se você mover a config ou a auditoria para outro caminho,
ajuste esses padrões no `opencode.json`.

## Teste manual

Depois de reiniciar o OpenCode, peça ao agente:

```text
Use dbscope para listar os ambientes.
Use dbscope para listar os schemas do ambiente dev.
Use dbscope para listar as tabelas do schema app_dev.
Use dbscope para descrever a tabela users em dev.
Use dbscope para executar: SELECT 1 AS ok
Use dbscope para executar: DELETE FROM users
```

O último comando deve retornar `denied_sql`. Confirme também que nenhuma
resposta contém host, usuário, senha ou DSN.

## Testes

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

Os testes unitários usam mocks e não acessam banco real. O teste de integração é
opcional e fica fora do CI:

```bash
DBSCOPE_INTEGRATION_CONFIG=~/.config/dbscope/config.toml \
  uv run pytest -m integration
```

## Auditoria

Cada chamada gera uma linha JSON em `~/.local/state/dbscope/audit.jsonl`
(configurável por `DBSCOPE_AUDIT_LOG`), com timestamp, ferramenta, ambiente,
duração, linhas, truncamento, SQL com literais redigidos e código de erro. O
arquivo é criado com permissão `600`, a pasta com `700`, e há rotação por
tamanho. Se a auditoria não puder ser criada ou gravada, a operação é
interrompida. Senhas e dados de conexão nunca são gravados.

## Limitações e riscos

- O resultado da query **vai** para o LLM. Limite linhas/bytes, use
  `mask_columns` como apoio e prefira views/ACL no banco para dados sensíveis.
- `mask_columns` casa pelo nome da coluna de saída; aliases podem contornar a
  máscara. Não substitui controle de acesso no banco.
- Timeouts são melhor esforço e variam entre MySQL (`max_execution_time`) e
  MariaDB (`max_statement_time`); o driver também aplica timeout de socket.
- Container não é uma fronteira rígida se o agente pode ler a config ou
  controlar o Docker. Para isolamento forte, use usuário de SO dedicado/rootless.
- Não há ferramentas de escrita, administração ou HTTP público.

## Repositório público

- Use apenas placeholders em docs, issues e commits.
- Não versione `config.toml`, `secrets/`, `certs/` com chaves, `.env` ou logs de
  auditoria.
- Rode `git status` antes de commitar e revise se algum host real escapou.

## Licença

MIT. Veja [LICENSE](LICENSE).
