from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any


DOCS_INDEX_URL = "https://akshare.akfamily.xyz/data/index.html"
TARGET = Path(__file__).resolve().parents[1] / "sats" / "data" / "akshare_datasets.py"


DOMAIN_RULES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("stock_zh_a", "股票数据", "A股", ("stock", "a-share", "china")),
    ("stock_a", "股票数据", "A股", ("stock", "a-share", "china")),
    ("stock_hk", "股票数据", "港股", ("stock", "hk")),
    ("stock_us", "股票数据", "美股", ("stock", "us")),
    ("stock_board", "股票数据", "板块题材", ("stock", "sector")),
    ("stock_sector", "股票数据", "板块题材", ("stock", "sector")),
    ("stock_gg", "股票数据", "港股", ("stock", "hk")),
    ("stock_", "股票数据", "股票", ("stock",)),
    ("index_", "指数数据", "指数", ("index",)),
    ("fund_", "基金数据", "基金", ("fund",)),
    ("bond_", "债券数据", "债券", ("bond",)),
    ("futures_", "期货数据", "期货", ("futures",)),
    ("option_", "期权数据", "期权", ("option",)),
    ("macro_", "宏观经济", "宏观", ("macro",)),
    ("news_", "新闻数据", "新闻", ("news",)),
    ("article_", "新闻数据", "文章指标", ("news", "article")),
    ("fx_", "外汇数据", "外汇", ("fx",)),
    ("forex_", "外汇数据", "外汇", ("fx",)),
    ("currency_", "货币数据", "货币", ("currency",)),
    ("crypto_", "加密货币", "加密货币", ("crypto",)),
    ("energy_", "商品数据", "能源", ("commodity", "energy")),
    ("spot_", "商品数据", "现货", ("commodity", "spot")),
    ("car_", "行业数据", "汽车", ("industry", "car")),
    ("air_", "另类数据", "空气质量", ("alternative", "air")),
    ("bank_", "银行数据", "银行", ("bank",)),
    ("amac_", "基金数据", "基金业协会", ("fund", "amac")),
)

REALTIME_TERMS = (
    "spot",
    "realtime",
    "minute",
    "min",
    "tick",
    "depth",
    "bid",
    "ask",
    "watch",
)


def main() -> None:
    import akshare as ak  # type: ignore

    rows = []
    for name in sorted(dir(ak)):
        if name.startswith("_"):
            continue
        obj = getattr(ak, name, None)
        if not inspect.isfunction(obj):
            continue
        rows.append(_row(name, obj))
    TARGET.write_text(_render(rows), encoding="utf-8")
    print(f"generated {len(rows)} AkShare dataset specs at {TARGET}")


def _row(name: str, obj: Any) -> dict[str, Any]:
    domain, category, tags = _classify(name)
    input_fields, required_fields = _signature_fields(obj)
    return {
        "dataset": name,
        "function_name": name,
        "title": name.replace("_", " "),
        "domain": domain,
        "category": category,
        "tags": tuple(sorted(set((*tags, *_extra_tags(name))))),
        "input_fields": tuple(input_fields),
        "required_fields": tuple(required_fields),
        "doc_url": DOCS_INDEX_URL,
        "realtime": _is_realtime(name),
        "default_limit": 200,
    }


def _classify(name: str) -> tuple[str, str, tuple[str, ...]]:
    lowered = name.lower()
    for prefix, domain, category, tags in DOMAIN_RULES:
        if lowered.startswith(prefix):
            return domain, category, tags
    return "AkShare", name.split("_", 1)[0] if "_" in name else "通用", ("akshare",)


def _extra_tags(name: str) -> tuple[str, ...]:
    lowered = name.lower()
    tags = []
    for token in ("em", "sina", "ths", "eastmoney", "cninfo", "cffex", "dce", "czce", "shfe", "gfex"):
        if token in lowered:
            tags.append(token)
    if _is_realtime(name):
        tags.append("realtime")
    return tuple(tags)


def _is_realtime(name: str) -> bool:
    lowered = name.lower()
    return any(term in lowered for term in REALTIME_TERMS)


def _signature_fields(obj: Any) -> tuple[list[str], list[str]]:
    try:
        signature = inspect.signature(obj)
    except (TypeError, ValueError):
        return [], []
    input_fields = []
    required_fields = []
    for param in signature.parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        input_fields.append(param.name)
        if param.default is param.empty:
            required_fields.append(param.name)
    return input_fields, required_fields


def _render(rows: list[dict[str, Any]]) -> str:
    lines = [
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "from typing import Any",
        "",
        f'AKSHARE_DOCS_INDEX_URL = "{DOCS_INDEX_URL}"',
        "",
        "",
        "@dataclass(frozen=True, slots=True)",
        "class AkShareDatasetSpec:",
        "    dataset: str",
        "    function_name: str",
        "    title: str",
        "    domain: str",
        "    category: str",
        "    tags: tuple[str, ...]",
        "    input_fields: tuple[str, ...]",
        "    required_fields: tuple[str, ...]",
        "    doc_url: str",
        "    realtime: bool",
        "    default_limit: int = 200",
        "",
        "    def to_dict(self, *, compact: bool = False) -> dict[str, Any]:",
        "        payload = {",
        '            "dataset": self.dataset,',
        '            "function_name": self.function_name,',
        '            "title": self.title,',
        '            "domain": self.domain,',
        '            "category": self.category,',
        '            "tags": list(self.tags),',
        '            "input_fields": list(self.input_fields),',
        '            "required_fields": list(self.required_fields),',
        '            "doc_url": self.doc_url,',
        '            "realtime": self.realtime,',
        '            "default_limit": self.default_limit,',
        "        }",
        "        if compact:",
        '            payload["tags"] = payload["tags"][:6]',
        '            payload["input_fields"] = payload["input_fields"][:8]',
        '            payload["required_fields"] = payload["required_fields"][:8]',
        "        return payload",
        "",
        "",
        "_RAW_AKSHARE_DATASETS: tuple[tuple[Any, ...], ...] = (",
    ]
    for row in rows:
        lines.append(
            "    "
            + repr(
                (
                    row["dataset"],
                    row["function_name"],
                    row["title"],
                    row["domain"],
                    row["category"],
                    row["tags"],
                    row["input_fields"],
                    row["required_fields"],
                    row["doc_url"],
                    row["realtime"],
                    row["default_limit"],
                )
            )
            + ","
        )
    lines.extend(
        [
            ")",
            "",
            "AKSHARE_DATASETS: dict[str, AkShareDatasetSpec] = {",
            "    row[0]: AkShareDatasetSpec(",
            "        dataset=row[0],",
            "        function_name=row[1],",
            "        title=row[2],",
            "        domain=row[3],",
            "        category=row[4],",
            "        tags=tuple(row[5]),",
            "        input_fields=tuple(row[6]),",
            "        required_fields=tuple(row[7]),",
            "        doc_url=row[8],",
            "        realtime=bool(row[9]),",
            "        default_limit=int(row[10]),",
            "    )",
            "    for row in _RAW_AKSHARE_DATASETS",
            "}",
            "",
            "",
            "def get_akshare_dataset(dataset: str) -> AkShareDatasetSpec:",
            "    key = str(dataset or '').strip()",
            "    spec = AKSHARE_DATASETS.get(key)",
            "    if spec is None:",
            "        raise KeyError(f'unknown AkShare dataset: {key}')",
            "    return spec",
            "",
            "",
            "def list_akshare_datasets(",
            "    *,",
            "    domain: str | None = None,",
            "    category: str | None = None,",
            "    tags: list[str] | tuple[str, ...] | str | None = None,",
            "    query: str | None = None,",
            "    realtime: bool | None = None,",
            "    compact: bool = False,",
            ") -> list[dict[str, Any]]:",
            "    domain_key = str(domain or '').strip().lower()",
            "    category_key = str(category or '').strip().lower()",
            "    query_key = str(query or '').strip().lower()",
            "    tag_values = _tag_values(tags)",
            "    rows: list[dict[str, Any]] = []",
            "    for spec in AKSHARE_DATASETS.values():",
            "        if domain_key and domain_key not in spec.domain.lower():",
            "            continue",
            "        if category_key and category_key not in spec.category.lower():",
            "            continue",
            "        if realtime is not None and spec.realtime is not bool(realtime):",
            "            continue",
            "        if tag_values and not tag_values.issubset({item.lower() for item in spec.tags}):",
            "            continue",
            "        if query_key and query_key not in _search_text(spec):",
            "            continue",
            "        rows.append(spec.to_dict(compact=compact))",
            "    return rows",
            "",
            "",
            "def _tag_values(tags: list[str] | tuple[str, ...] | str | None) -> set[str]:",
            "    if tags is None:",
            "        return set()",
            "    if isinstance(tags, str):",
            "        raw = [tags]",
            "    else:",
            "        raw = list(tags)",
            "    return {str(item).strip().lower() for item in raw if str(item).strip()}",
            "",
            "",
            "def _search_text(spec: AkShareDatasetSpec) -> str:",
            "    return ' '.join(",
            "        str(part).lower()",
            "        for part in (",
            "            spec.dataset,",
            "            spec.function_name,",
            "            spec.title,",
            "            spec.domain,",
            "            spec.category,",
            "            ' '.join(spec.tags),",
            "            ' '.join(spec.input_fields),",
            "        )",
            "    )",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
