from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from backend.app.memory import SQLiteMemoryStore  # noqa: E402
from backend.app.transformer_embedding import (  # noqa: E402
    LocalTransformerMemoryEmbeddingProvider,
)


DEFAULT_DATABASE = REPOSITORY_ROOT / "backend" / "data" / "agent_memory.sqlite3"


def _owner_ids(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT owner_id FROM agent_memories ORDER BY owner_id"
            ).fetchall()
        ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="批量生成已保存长期记忆的本地 Transformer 向量。"
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--owner-id", action="append", default=[])
    parser.add_argument("--all-owners", action="store_true")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--limit-per-owner", type=int)
    parser.add_argument(
        "--verify-hashes",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    args = parser.parse_args()

    database = args.database.expanduser().resolve()
    if not database.is_file():
        raise SystemExit(f"Memory database does not exist: {database}")
    owners = list(dict.fromkeys(str(item).strip() for item in args.owner_id if item.strip()))
    if args.all_owners:
        owners = _owner_ids(database)
    if not owners:
        raise SystemExit("Provide --owner-id or explicitly use --all-owners.")

    provider = LocalTransformerMemoryEmbeddingProvider(
        args.model_path,
        batch_size=args.batch_size,
        verify_hashes=args.verify_hashes,
    )
    store = SQLiteMemoryStore(
        database,
        embedding_provider=provider,
        semantic_enabled=True,
    )
    report: dict[str, object] = {
        "model_id": provider.model_id,
        "dimension": provider.dimension,
        "owners": {},
    }
    try:
        owner_reports = report["owners"]
        assert isinstance(owner_reports, dict)
        for owner_id in owners:
            owner_reports[owner_id] = store.backfill_memory_embeddings(
                owner_id=owner_id,
                batch_size=args.batch_size,
                limit=args.limit_per_owner,
            )
    finally:
        store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
