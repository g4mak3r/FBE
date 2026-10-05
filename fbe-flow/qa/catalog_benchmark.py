"""Repeatable large-assortment export/import check against temporary SQLite."""

import argparse
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from fbe_flow.app import create_app
from fbe_flow.config import AppConfig
from fbe_flow.modules import catalog_xlsx
from tests.conftest import FixtureAdapter
from tests.test_catalog import data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=30000)
    args = parser.parse_args()
    if not 1 <= args.count <= 100000:
        raise ValueError("Count: 1..100000")
    with TemporaryDirectory(prefix="fbe-assortment-benchmark-") as directory:
        app = create_app(AppConfig(Path(directory), worker_enabled=False), [FixtureAdapter()])
        app.state.database.initialize()
        seller = app.state.sellers.create("Benchmark")["id"]
        catalog = app.state.catalog
        started = time.perf_counter()
        with app.state.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for number in range(1, args.count + 1):
                catalog.save_product_in(
                    conn,
                    seller,
                    data(
                        number,
                        length_mm="125.123456",
                        gross_weight_g="250.25",
                        attributes={"Цвет": "Синий", "Объём": number},
                    ),
                )
        products = catalog.export_data(seller)["products"]
        catalog.save_document(
            seller,
            {
                "kind": "declaration",
                "number": "Shared benchmark DoS",
                "product_ids": [p["id"] for p in products],
            },
        )
        del products
        seeded = time.perf_counter()
        content = catalog_xlsx.export(catalog, seller)
        exported = time.perf_counter()
        plan = catalog_xlsx.preview(catalog, seller, content)
        checked = time.perf_counter()
        assert plan["errors"] == [], plan["errors"][:3]
        assert plan["operations"] == [], "Unchanged export must not create changes"
        assert catalog.list(seller)["total"] == args.count
        print(
            json.dumps(
                {
                    "products": args.count,
                    "xlsx_bytes": len(content),
                    "seed_seconds": round(seeded - started, 2),
                    "export_seconds": round(exported - seeded, 2),
                    "preview_seconds": round(checked - exported, 2),
                    "errors": len(plan["errors"]),
                    "changes": len(plan["operations"]),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
