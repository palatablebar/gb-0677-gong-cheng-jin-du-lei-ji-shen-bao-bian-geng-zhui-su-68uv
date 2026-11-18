"""投递、内容与月末封存编号的身份规则。

三层身份必须分开：
- 投递摘要：原始文件字节 + 计算参数。重试同一投递得到同一摘要，不得新增记录。
- 内容摘要：剔除来源行号等传输痕迹后、与行顺序无关的业务内容摘要。
  换行顺序重投仍是同一组申报内容，必须落到同一封存编号。
- 封存编号：内容摘要派生的唯一月末编号，同一内容永远得到同一编号。
"""
import hashlib
import json
from datetime import datetime, timezone

from app.config import RULE_VERSION


def _canonical(value):
    """去掉 _line/_sources/_raw 等传输痕迹，并稳定排序对象键。"""
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in sorted(value.items(), key=lambda item: str(item[0])) if not str(k).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def _dumps(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def content_payload(stages, params):
    """一组申报内容的规范形态：采纳行与隔离行都参与，但不含物理行号。"""
    last = stages[-1] if stages else {}
    accepted = [_canonical(row) for row in last.get("rows", [])]
    quarantine = [_canonical(row) for row in last.get("cumulative_quarantine", [])]
    # 行顺序不属于业务内容；按规范行排序后再取摘要。
    return {
        "rule_version": RULE_VERSION,
        "parameters": _canonical(params or {}),
        "accepted": sorted(accepted, key=_dumps),
        "quarantine": sorted(quarantine, key=_dumps),
    }


def content_digest(stages, params):
    return hashlib.sha256(_dumps(content_payload(stages, params)).encode("utf-8")).hexdigest()


def seal_month(rows):
    """月末封存所属月份：取申报周期中的最晚月份，避免依赖生成时刻。"""
    months = sorted({str(row.get("period", "")) for row in rows if len(str(row.get("period", ""))) == 7})
    return months[-1] if months else datetime.now(timezone.utc).strftime("%Y-%m")


def seal_number(digest, rows):
    return f"MS-{seal_month(rows).replace('-', '')}-{digest[:8].upper()}"


def delivery_digest(raw, params):
    """原始投递身份：文件字节 + 规范化参数。重试同一投递保持稳定。"""
    return hashlib.sha256(raw + _dumps(params or {}).encode("utf-8")).hexdigest()
