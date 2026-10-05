"""Ozon Seller FBS: product IDs and posting SKU are deliberately distinct."""

from copy import deepcopy

from fbe_flow.core.errors import Conflict, InvalidInput
from fbe_flow.core.models import NormalizedBatch, OperationResult, Order, Product, Supply, Warehouse
from fbe_flow.integrations.chz.http import RemoteError
from fbe_flow.integrations.commerce import ProtectedAdapter, objects, positive

EXEMPLAR_FIELDS = (
    "exemplar_id",
    "gtd",
    "is_gtd_absent",
    "is_rnpt_absent",
    "marks",
    "rnpt",
    "weight",
)


def exemplar_wire(value):
    return {
        "posting_number": value["posting_number"],
        **({"multi_box_qty": value["multi_box_qty"]} if "multi_box_qty" in value else {}),
        "products": [
            {
                "product_id": positive(p["product_id"]),
                "exemplars": [
                    {k: deepcopy(v[k]) for k in EXEMPLAR_FIELDS if k in v}
                    for v in objects(p, "exemplars")
                ],
            }
            for p in objects(value, "products")
        ],
    }


class OzonAdapter(ProtectedAdapter):
    key, label = "ozon", "Ozon FBS"
    host, interval = "https://api-seller.ozon.ru", 0.6
    phases = ("warehouses", "products", "orders", "supplies", "returns")

    def headers(self, config, token):
        return {"Client-Id": config.account_id, "Api-Key": token}

    def identity(self, config):
        roles = self._call(config, "POST", "/v1/roles", body={})
        objects(roles, "roles")
        value = self._call(config, "POST", "/v1/seller/info", body={})
        company = value.get("company") if isinstance(value, dict) else None
        if not isinstance(company, dict) or not isinstance(company.get("inn"), str):
            raise RemoteError(None, "ozon_identity_invalid")
        tin = company["inn"]
        if not tin.isascii() or not tin.isdigit() or len(tin) not in {10, 12}:
            raise RemoteError(None, "ozon_identity_invalid")
        return {"account_id": config.account_id, "tin": tin, "company": company, "roles": roles}

    def product(self, source):
        return Product(
            external_id=str(positive(source["id"])),
            title=source["name"],
            sku=source.get("offer_id"),
            identifiers={
                "ozon_sku": [str(positive(source["sku"]))],
                "barcode": list(source.get("barcodes", [])),
            },
            category={"external_id": str(source.get("description_category_id", ""))},
            attributes={"source": source, "variant": str(positive(source["sku"]))},
        )

    @staticmethod
    def catalog_variants(record):
        from fbe_flow.integrations.catalog import variants

        return variants(record, "ozon")

    def catalog_schema(self, context, category):
        config = self.config(context)
        self.account(config)
        parts = category.split(":")
        if len(parts) != 2 or any(not v.isascii() or not v.isdigit() for v in parts):
            raise InvalidInput("Для Ozon укажите description_category_id:type_id")
        description, type_id = (positive(int(v)) for v in parts)
        response = self._call(config, "POST", "/v1/description-category/attribute", body={
            "description_category_id": description, "type_id": type_id, "language": "DEFAULT",
        })
        if not isinstance(response, dict) or not isinstance(response.get("result"), list):
            raise RemoteError(None, "ozon_attribute_schema_invalid")
        return {"category": category, "attributes": response["result"],
                "description_category_id": description, "type_id": type_id,
                "dimension_unit": "mm", "weight_unit": "g",
                "source_url": "https://docs.ozon.ru/api/seller/"}

    def catalog_dictionary(self, context, category, attribute_id, last_value_id=0):
        schema = self.catalog_schema(context, category)
        if not any(v.get("id") == attribute_id and v.get("dictionary_id")
                   for v in schema["attributes"]):
            raise InvalidInput("У характеристики нет словаря в этой категории")
        response = self._call(self.config(context), "POST",
                              "/v1/description-category/attribute/values", body={
            "description_category_id": schema["description_category_id"],
            "type_id": schema["type_id"], "attribute_id": positive(attribute_id),
            "language": "DEFAULT", "limit": 100, "last_value_id": last_value_id,
        })
        if not isinstance(response, dict) or not isinstance(response.get("result"), list):
            raise RemoteError(None, "ozon_attribute_dictionary_invalid")
        return response

    def catalog_details(self, context, record):
        config = self.config(context)
        self.account(config)
        response = self._call(config, "POST", "/v4/product/info/attributes", body={
            "filter": {"product_id": [record["external_id"]], "visibility": "ALL"},
            "limit": 100, "sort_dir": "ASC",
        })
        if not isinstance(response, dict) or not isinstance(response.get("result"), list):
            raise RemoteError(None, "ozon_product_attributes_invalid")
        values = [v for v in response["result"] if str(v.get("id")) == record["external_id"]]
        if len(values) != 1:
            raise RemoteError(None, "ozon_product_attributes_missing")
        return self.product({**record["attributes"]["source"], **values[0]})

    def warehouse(self, source):
        return Warehouse(
            external_id=str(positive(source["warehouse_id"])),
            name=source["name"],
            attributes={"source": source},
        )

    def order(self, source):
        self.items(source)
        return Order(
            external_id=source["posting_number"],
            status=source["status"],
            # Historical postings may refer to warehouses outside the current list.
            attributes={"source": source, "items": self.items(source)},
        )

    def items(self, source):
        values = objects(source, "products")
        result = [
            {
                "item_id": str(positive(v["sku"])),
                "variant": str(positive(v["sku"])),
                "quantity": positive(v["quantity"]),
                "title": v.get("name", v.get("offer_id", str(v["sku"]))),
                "price": v.get("price"),
            }
            for v in values
        ]
        if len({v["item_id"] for v in result}) != len(result):
            raise RemoteError(None, "ozon_duplicate_sku")
        return result

    def sync(self, config, payload):
        self.account(config)
        phase, cursor = payload["phase"], payload.get("cursor", "")
        following, batch, extras = None, NormalizedBatch(), []
        if phase == "warehouses":
            value = self._call(
                config, "POST", "/v2/warehouse/list", body={"limit": 200, "cursor": cursor}
            )
            values = objects(value, "warehouses")
            batch = NormalizedBatch(warehouses=tuple(self.warehouse(v) for v in values))
            if value.get("has_next") is True:
                following = {"phase": phase, "cursor": value.get("cursor")}
        elif phase == "products":
            value = self._call(
                config,
                "POST",
                "/v3/product/list",
                body={"filter": {"visibility": "ALL"}, "last_id": cursor, "limit": 100},
            )
            result = value.get("result", {})
            ids = [positive(v["product_id"]) for v in objects(result, "items")]
            values = []
            if ids:
                reply = self._call(
                    config, "POST", "/v3/product/info/list", body={"product_id": ids}
                )
                values = objects(reply, "items")
                if {positive(v["id"]) for v in values} != set(ids) or len(values) != len(ids):
                    raise RemoteError(None, "ozon_products_incomplete")
            batch = NormalizedBatch(products=tuple(self.product(v) for v in values))
            if len(ids) == 100:
                following = {"phase": phase, "cursor": result.get("last_id")}
        elif phase == "orders":
            offset = payload.get("offset", 0)
            value = self._call(
                config,
                "POST",
                "/v3/posting/fbs/list",
                body={
                    "dir": "ASC",
                    "filter": {"since": payload["date_from"], "to": payload["date_to"]},
                    "limit": 100,
                    "offset": offset,
                    "with": {"analytics_data": True, "financial_data": True, "barcodes": True},
                },
            )
            result = value.get("result", {})
            values = objects(result, "postings")
            batch = NormalizedBatch(orders=tuple(self.order(v) for v in values))
            if result.get("has_next") is True:
                if not values:
                    raise RemoteError(None, "ozon_pagination_stalled")
                following = {"phase": phase, "offset": offset + len(values)}
        elif phase == "supplies":
            value = self._call(
                config, "POST", "/v2/carriage/delivery/list", body={"limit": 100, "cursor": cursor}
            )
            methods, values = objects(value, "methods"), []
            for method in methods:
                for carriage in objects(method, "carriages"):
                    if carriage.get("id") == 0:
                        continue  # A potential carriage is not an existing supply.
                    values.append(
                        Supply(
                            external_id=str(positive(carriage["id"])),
                            status=carriage["status"],
                            attributes={"source": carriage, "delivery_method": method},
                        )
                    )
            batch = NormalizedBatch(supplies=tuple(values))
            if value.get("has_next") is True:
                following = {"phase": phase, "cursor": value.get("cursor")}
        elif phase == "returns":
            value = self._call(
                config,
                "POST",
                "/v1/returns/list",
                body={
                    "filter": {"return_schema": "FBS"},
                    "last_id": int(cursor or 0),
                    "limit": 100,
                },
            )
            values = objects(value, "returns")
            extras = [
                {"kind": "returns", "external_id": str(positive(v["id"])), "value": v}
                for v in values
            ]
            if value.get("has_next") is True:
                following = (
                    {"phase": phase, "cursor": str(positive(values[-1]["id"]))} if values else {}
                )
        else:
            raise InvalidInput("Неизвестный раздел Ozon")
        if (
            following
            and "cursor" in following
            and (not following["cursor"] or following["cursor"] == cursor)
        ):
            raise RemoteError(None, "ozon_pagination_stalled")
        if following == {}:
            raise RemoteError(None, "ozon_pagination_stalled")
        if following is None:
            index = self.phases.index(phase) + 1
            if index < len(self.phases):
                following = {"phase": self.phases[index]}
        return OperationResult(
            batch=batch,
            data={
                "sync": {
                    "phase": phase,
                    "following": following,
                    "count": sum(
                        len(getattr(batch, k))
                        for k in ("products", "orders", "supplies", "warehouses")
                    )
                    + len(extras),
                },
                "objects": extras,
            },
        )

    def get_order(self, config, external_id):
        self.account(config)
        value = self._call(
            config,
            "POST",
            "/v3/posting/fbs/get",
            body={
                "posting_number": external_id,
                "with": {"analytics_data": True, "financial_data": True},
            },
        )
        result = value.get("result") if isinstance(value, dict) else None
        if not isinstance(result, dict) or result.get("posting_number") != external_id:
            raise RemoteError(None, "ozon_posting_mismatch")
        self.items(result)
        return result

    def exemplars(self, config, posting):
        value = self._call(
            config,
            "POST",
            "/v6/fbs/posting/product/exemplar/create-or-get",
            body={"posting_number": posting},
        )
        self.validate_exemplars(value, posting)
        return value

    @staticmethod
    def validate_exemplars(value, posting):
        if not isinstance(value, dict) or value.get("posting_number") != posting:
            raise RemoteError(None, "ozon_exemplars_mismatch")
        seen, ids = set(), set()
        for product in objects(value, "products"):
            sku = positive(product["product_id"])
            if sku in seen:
                raise RemoteError(None, "ozon_exemplars_duplicate")
            seen.add(sku)
            for exemplar in objects(product, "exemplars"):
                number = positive(exemplar["exemplar_id"])
                if number in ids:
                    raise RemoteError(None, "ozon_exemplars_duplicate")
                ids.add(number)
                marks = exemplar.get("marks", [])
                if not isinstance(marks, list) or any(
                    not isinstance(m, dict)
                    or not isinstance(m.get("mark"), str)
                    or not isinstance(m.get("mark_type"), str)
                    for m in marks
                ):
                    raise RemoteError(None, "ozon_marks_invalid")
        return value

    def prepare_codes(self, config, order, item, codes):
        source = self.get_order(config, order["external_id"])
        self.require_packaging(source)
        current = next((v for v in self.items(source) if v["item_id"] == item["item_id"]), None)
        if current is None or current["quantity"] != len(codes):
            raise Conflict("Состав отправления изменился; обновите Ozon")
        initial = self.exemplars(config, order["external_id"])
        products = objects(initial, "products")
        selected = next((v for v in products if str(v["product_id"]) == item["variant"]), None)
        if not selected or len(selected["exemplars"]) != len(codes):
            raise Conflict("Ozon не вернул экземпляры для каждой единицы товара")
        wire, before = exemplar_wire(initial), exemplar_wire(initial)
        target = next(v for v in wire["products"] if str(v["product_id"]) == item["variant"])
        for exemplar, code in zip(target["exemplars"], codes, strict=True):
            marks = exemplar.get("marks", [])
            present = [v["mark"] for v in marks if v["mark_type"] == "mandatory_mark"]
            if present and present != [code["full_code"]]:
                raise Conflict("В Ozon уже назначен другой код; перезапись запрещена")
            exemplar["marks"] = [v for v in marks if v["mark_type"] != "mandatory_mark"] + [
                {"mark": code["full_code"], "mark_type": "mandatory_mark"}
            ]
        return {"wire": wire, "before": before}

    @staticmethod
    def require_packaging(source):
        if source.get("status") != "awaiting_packaging":
            raise Conflict("Отправление должно ожидать сборки")

    def preflight(self, config, kind, body):
        source = self.get_order(config, body["order_external_id"])
        if kind == "codes":
            self.require_packaging(source)
            selected = next(
                (v for v in self.items(source) if v["item_id"] == body["item_id"]), None
            )
            if not selected or selected["quantity"] != len(body["sgtins"]):
                raise Conflict("Состав отправления изменился")
            current = exemplar_wire(self.exemplars(config, body["order_external_id"]))
            if current != body["before"] and current != body["wire"]:
                raise Conflict("Данные экземпляров Ozon изменились; подготовьте действие заново")
        elif kind == "ship":
            self.require_packaging(source)
            if self.items(source) != body["items"] or "ship" not in source.get(
                "available_actions", []
            ):
                raise Conflict("Состав или возможность сборки Ozon изменились")
            requirements = source.get("requirements", {})
            if any(v for k, v in requirements.items() if k != "products_requiring_mandatory_mark"):
                raise InvalidInput("Дополнительные сведения товара внесите в кабинете Ozon")
            if requirements.get("products_requiring_mandatory_mark") or body.get("marking_actions"):
                status = self.status(config, body["order_external_id"])
                if status.get("status") != "ship_available":
                    raise Conflict("Ozon ещё не разрешил сборку маркированного отправления")
        elif kind == "cancel":
            if "cancel" not in source.get("available_actions", []):
                raise Conflict("Ozon не разрешает отмену этого отправления")
            reasons = self.cancel_reasons(config)
            if not any(
                v["id"] == body["cancel_reason_id"] and v.get("is_available_for_cancellation")
                for v in reasons
            ):
                raise Conflict("Причина отмены больше недоступна")
        else:
            raise InvalidInput("Неизвестное действие Ozon")

    def cancel_reasons(self, config):
        self.account(config)
        value = self._call(config, "POST", "/v2/posting/fbs/cancel-reason/list", body={})
        return objects(value, "result")

    def status(self, config, posting):
        value = self._call(
            config,
            "POST",
            "/v5/fbs/posting/product/exemplar/status",
            body={"posting_number": posting},
        )
        return self.validate_exemplars(value, posting)

    def send(self, config, kind, body):
        endpoint = {
            "codes": "/v6/fbs/posting/product/exemplar/set",
            "ship": "/v4/posting/fbs/ship",
            "cancel": "/v2/posting/fbs/cancel",
        }[kind]
        value = self._call(config, "POST", endpoint, body=body["wire"], write=True)
        if kind == "ship":
            results = value.get("result") if isinstance(value, dict) else None
            if not isinstance(results, list) or body["order_external_id"] not in results:
                raise RemoteError(None, "ozon_ship_receipt_invalid")
        elif kind == "cancel" and (not isinstance(value, dict) or value.get("result") is not True):
            raise RemoteError(None, "ozon_cancel_receipt_invalid")
        elif kind == "codes" and value is not None:
            if not isinstance(value, dict) or value.get("code", 0) not in {0, "0"}:
                raise RemoteError(None, "ozon_codes_receipt_invalid")
        return {"receipt": value}

    def reconcile(self, config, kind, body, acknowledged):
        self.account(config)
        if kind == "codes":
            value = self.status(config, body["order_external_id"])
            selected = next(
                (v for v in value["products"] if str(v["product_id"]) == body["variant"]), None
            )
            marks = (
                [
                    m
                    for e in selected["exemplars"]
                    for m in e.get("marks", [])
                    if m["mark_type"] == "mandatory_mark"
                ]
                if selected
                else []
            )
            actual, expected = [v["mark"] for v in marks], body["sgtins"]
            if len(actual) != len(set(actual)):
                raise RemoteError(None, "ozon_marks_duplicate")
            if actual and (set(actual) != set(expected) or len(actual) != len(expected)):
                return {"state": "conflict", "reason": "Коды Ozon отличаются", "metadata": value}
            if not actual:
                return {"state": "pending" if acknowledged else "unknown", "metadata": value}
            planned = next(
                v for v in body["wire"]["products"] if str(v["product_id"]) == body["variant"]
            )
            assignments = {
                e["exemplar_id"]: [
                    m["mark"] for m in e.get("marks", []) if m["mark_type"] == "mandatory_mark"
                ]
                for e in selected["exemplars"]
            }
            required = {
                e["exemplar_id"]: [
                    m["mark"] for m in e["marks"] if m["mark_type"] == "mandatory_mark"
                ]
                for e in planned["exemplars"]
            }
            if assignments != required or any(len(v) != 1 for v in assignments.values()):
                return {
                    "state": "conflict",
                    "reason": "КМ экземпляров Ozon отличаются",
                    "metadata": value,
                }
            if all(v.get("check_status") == "passed" and not v.get("error_codes") for v in marks):
                return {"state": "confirmed", "metadata": value, "remote_verified": True}
            if any(v.get("error_codes") for v in marks):
                return {
                    "state": "rejected",
                    "metadata": value,
                    "reason": "Ozon отклонил проверку КМ",
                }
            # 'failed' means a processing failure, not proof that the write never occurred.
            return {"state": "pending", "metadata": value}
        source = self.get_order(config, body["order_external_id"])
        status = source["status"]
        if kind == "ship":
            if source.get("substatus") == "ship_failed":
                return {"state": "rejected", "source": source, "reason": "Ozon не завершил сборку"}
            if status in {
                "awaiting_deliver",
                "delivering",
                "delivered",
                "driver_pickup",
                "sent_by_seller",
            }:
                return {"state": "confirmed", "source": source, "remote_verified": True}
            if status == "cancelled":
                return {"state": "conflict", "source": source}
        elif kind == "cancel" and status == "cancelled":
            return {"state": "confirmed", "source": source, "remote_verified": True}
        return {"state": "pending" if acknowledged else "unknown", "source": source}
