"""Console entrypoint for the dbscope MCP server."""

from __future__ import annotations

import sys

from .config import load_settings
from .errors import DbScopeError
from .server import create_server
from .service import DbScopeService


def main() -> None:
    try:
        settings = load_settings()
    except DbScopeError as exc:
        print(f"dbscope: {exc.code}: {exc.safe_message}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("dbscope: erro ao carregar a configuração.", file=sys.stderr)
        raise SystemExit(1) from None

    try:
        service = DbScopeService(settings)
        server = create_server(service)
        server.run(transport="stdio")
    except KeyboardInterrupt:
        return
    except DbScopeError as exc:
        print(f"dbscope: {exc.code}: {exc.safe_message}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("dbscope: erro interno ao iniciar o servidor.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
