"""Read-only source-card projections; canonical fields require operator review."""
import re
from decimal import Decimal, InvalidOperation
from fbe_flow.core.catalog_models import normalize_gtin

def gtins(values):
    result = []
    for value in values:
        try:
            result.append(normalize_gtin(value))
        except (ValueError, TypeError):
            pass
    return list(dict.fromkeys(result))

def amount(value, scale=1):
    try:
        number = Decimal(str(value)) * scale
        return str(number) if number.is_finite() and number > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None

def variants(record, channel):
    source = record.get("attributes", {}).get("source", {})
    identifiers = record.get("identifiers", {})
    fields = {"title": record["title"], "sku": record.get("sku") or record["external_id"],
              "barcodes": identifiers.get("barcode", []), "attributes": {}}
    fields["gtins"] = gtins(identifiers.get("gtin", []) + fields["barcodes"])
    if channel == "wb":
        fields.update(brand=source.get("brand"), description=source.get("description"))
        if type(source.get("kizMarked")) is bool:
            fields["marking_attestation"] = source["kizMarked"]
        dimensions = source.get("dimensions") or {}
        for target, key, scale in [
            ("length_mm", "length", 10), ("width_mm", "width", 10),
            ("height_mm", "height", 10), ("gross_weight_g", "weightBrutto", 1000),
        ]:
            fields[target] = amount(dimensions.get(key), scale)
        for attribute in source.get("characteristics", []):
            name, value = attribute.get("name"), attribute.get("value")
            if name:
                fields["attributes"]["wb:" + str(attribute.get("id", name))] = value
                if re.sub(r"[^а-яa-z]", "", name.lower()) in {"тнвэд", "кодтнвэд"}:
                    text = value[0] if isinstance(value, list) and value else value
                    if isinstance(text, str) and re.fullmatch(r"[0-9]{10}", text):
                        fields["tnved"] = text
        result = []
        for size in source.get("sizes", []):
            if size.get("chrtID") is None:
                continue
            codes, key = size.get("skus", []), str(size["chrtID"])
            result.append({
                "key": key, "label": str(size.get("techSize") or key),
                "fields": {**fields, "sku": fields["sku"] + ":" + key, "family": fields["sku"],
                           "barcodes": codes, "gtins": gtins(codes)},
                "identifiers": {"barcode": codes, "gtin": gtins(codes)},
            })
        return result
    if channel == "ozon":
        if source.get("dimension_unit") == "mm":
            for target, key in [
                ("length_mm", "depth"), ("width_mm", "width"), ("height_mm", "height")
            ]:
                fields[target] = amount(source.get(key))
        if source.get("weight_unit") == "g":
            fields["gross_weight_g"] = amount(source.get("weight"))
        for attribute in source.get("attributes", []):
            fields["attributes"]["ozon:" + str(attribute["id"])] = attribute.get("values", [])
    if channel == "chz":
        fields["brand"] = source.get("brand_name")
        for attribute in source.get("good_attrs", []):
            name, value = attribute.get("attr_name", ""), attribute.get("attr_value")
            fields["attributes"]["nk:" + str(attribute.get("attr_id", name))] = value
            normalized = re.sub(r"[^а-яa-z0-9]", "", name.lower())
            if normalized in {"кодтнвэд", "тнвэд"} and re.fullmatch(r"[0-9]{10}", str(value)):
                fields["tnved"] = value
            if normalized in {"кодокпд2", "окпд2"}:
                fields["okpd2"] = value
            if normalized == "состав":
                fields["composition"] = value
    key = str(record.get("attributes", {}).get("variant", ""))
    return [{"key": key, "label": record["title"], "fields": fields,
             "identifiers": {**identifiers, "gtin": fields["gtins"]}}]
