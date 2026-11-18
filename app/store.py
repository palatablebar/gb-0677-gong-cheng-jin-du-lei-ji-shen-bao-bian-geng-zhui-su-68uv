"""批次、投递与封存版本存储。

三层事实分别落库，任何一层都不能覆盖另一层：
- runs：按业务内容归并后的批次（同一组申报内容只有一条）。
- deliveries：每次原始投递的字节证据，只追加、按投递摘要幂等。
- seal_versions：月末封存的证据快照，按版本追加；失败重试续用已有版本号。
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.archive import build_archive
from app.config import DATA_DIR
from app.errors import DomainError
from app.identity import content_digest, delivery_digest, seal_number as seal_number_for


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA_DIR / "records.db", timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    _schema(db)
    return db


def _schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,payload TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,decision TEXT NOT NULL DEFAULT '待复核')")
    columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
    for name, kind in (("content_sha256", "TEXT"), ("seal_number", "TEXT"), ("sealed_version", "INTEGER"), ("sealed_at", "TEXT")):
        if name not in columns:
            db.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind}")
    db.execute("CREATE TABLE IF NOT EXISTS deliveries (delivery_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,input_sha256 TEXT NOT NULL,params TEXT NOT NULL,source TEXT,received_at TEXT NOT NULL,byte_size INTEGER NOT NULL,raw BLOB NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS deliveries_run ON deliveries(run_id)")
    db.execute("CREATE TABLE IF NOT EXISTS seal_versions (seal_number TEXT NOT NULL,version INTEGER NOT NULL,run_id TEXT NOT NULL,content_sha256 TEXT NOT NULL,fingerprint TEXT NOT NULL,status TEXT NOT NULL,sealed_at TEXT NOT NULL,evidence TEXT NOT NULL,PRIMARY KEY (seal_number,version))")


def _now():
    return datetime.now(timezone.utc).isoformat()


# 未定稿封存快照的租约：超过此时长仍为 pending，视为中断残留。
PENDING_LEASE_SECONDS = 60


def _params(delivery):
    return json.dumps(delivery.get("params") or {}, sort_keys=True, ensure_ascii=False)


def _delivery_id(delivery):
    if delivery.get("delivery_id"):
        return delivery["delivery_id"]
    return delivery_digest(delivery["raw"], delivery.get("params"))


def _record_delivery(db, run_id, delivery):
    raw = delivery["raw"]
    db.execute(
        "INSERT OR IGNORE INTO deliveries(delivery_id,run_id,input_sha256,params,source,received_at,byte_size,raw) VALUES (?,?,?,?,?,?,?,?)",
        (
            _delivery_id(delivery),
            run_id,
            hashlib.sha256(raw).hexdigest(),
            _params(delivery),
            delivery.get("source"),
            delivery.get("received_at") or _now(),
            len(raw),
            raw,
        ),
    )


def save(run_id, payload, delivery=None):
    """保存批次并按投递摘要追加原始投递。同一投递重试不会新增。"""
    meta = payload.get("manifest", {})
    content = meta.get("content_sha256") or payload.get("content_sha256") or run_id
    seal_number = payload.get("seal_number") or meta.get("seal_number")
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO runs(id,payload,version,decision,content_sha256,seal_number) VALUES (?,?,1,'待复核',?,?)",
            (run_id, json.dumps(payload, ensure_ascii=False), content, seal_number),
        )
        # 兼容早期库：已有批次补齐内容与封存编号，但不改写其余字段。
        db.execute("UPDATE runs SET content_sha256=COALESCE(content_sha256,?),seal_number=COALESCE(seal_number,?) WHERE id=?", (content, seal_number, run_id))
        if delivery is not None:
            _record_delivery(db, run_id, delivery)
    return get(run_id)


def _delivery_rows(db, run_id):
    return [
        {"delivery_id": d, "input_sha256": h, "params": json.loads(p or "{}"), "source": s, "received_at": t, "byte_size": n}
        for d, h, p, s, t, n in db.execute("SELECT delivery_id,input_sha256,params,source,received_at,byte_size FROM deliveries WHERE run_id=? ORDER BY rowid", (run_id,))
    ]


def _legacy_identity(data):
    """旧库批次没有内容摘要与封存编号：能从存量结果补算就补算，不改写证据正文。"""
    if data.get("content_sha256") and data.get("seal_number"):
        return data["content_sha256"], data["seal_number"]
    stages = data.get("stages") or []
    if not stages:
        return data.get("content_sha256"), data.get("seal_number")
    content = data.get("content_sha256") or content_digest(stages, data.get("parameters") or {})
    seal_number = data.get("seal_number") or seal_number_for(content, stages[-1].get("rows", []))
    return content, seal_number


def get(run_id):
    with connect() as db:
        row = db.execute("SELECT payload,version,decision,content_sha256,seal_number,sealed_version,sealed_at FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise DomainError("NOT_FOUND", "批次不存在", 404)
        data = json.loads(row[0])
        deliveries = _delivery_rows(db, run_id)
        content, seal_number = _legacy_identity({**data, "content_sha256": row[3], "seal_number": row[4]})
        if (row[3] is None and content) or (row[4] is None and seal_number):
            db.execute("UPDATE runs SET content_sha256=COALESCE(content_sha256,?),seal_number=COALESCE(seal_number,?) WHERE id=?", (content, seal_number, run_id))
    data.update(version=row[1], decision=row[2], content_sha256=content, seal_number=seal_number, sealed_version=row[5], sealed_at=row[6], deliveries=deliveries)
    return data


def latest():
    with connect() as db:
        row = db.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
    return get(row[0]) if row else None


def history():
    with connect() as db:
        return [
            {"run_id": i, "version": v, "decision": d, "seal_number": s, "sealed_version": sv, "deliveries": n, "content_sha256": c}
            for i, v, d, s, sv, n, c in db.execute(
                "SELECT r.id,r.version,r.decision,r.seal_number,r.sealed_version,(SELECT COUNT(*) FROM deliveries x WHERE x.run_id=r.id),r.content_sha256 FROM runs r ORDER BY r.rowid DESC"
            )
        ]


def review(run_id, version, decision):
    if decision not in {"已复核", "退回复核"}:
        raise DomainError("BAD_DECISION", "复核状态无效")
    with connect() as db:
        cursor = db.execute("UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?", (decision, run_id, version))
        if cursor.rowcount != 1:
            if not db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise DomainError("NOT_FOUND", "批次不存在", 404)
            raise DomainError("STALE_VERSION", "版本已更新，请刷新", 409)
    return get(run_id)


def _seal_fingerprint(run):
    # 业务结论与投递集合共同决定证据内容：同一投递原样重试集合不变，
    # 幂等复用已有版本；后到的换序原件则在同一封存编号下追加版本，
    # 保证每次原始投递都留在封存证据里，而不会产生第二个月末编号。
    payload = json.dumps(
        {
            "content_sha256": run.get("content_sha256") or run.get("manifest", {}).get("content_sha256"),
            "version": run.get("version"),
            "decision": run.get("decision"),
            "rule_version": run.get("manifest", {}).get("rule_version"),
            "deliveries": sorted(d["delivery_id"] for d in run.get("deliveries", [])),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _latest_sealed(db, seal_number):
    return db.execute(
        "SELECT version,fingerprint,evidence,sealed_at FROM seal_versions WHERE seal_number=? AND status='sealed' ORDER BY version DESC LIMIT 1",
        (seal_number,),
    ).fetchone()


def _discard_pending(db, seal_number=None, stale_after=None):
    """清理未定稿快照。recover 传 stale_after=None 时清全部；
    seal 只清理超过租约期的残留，避免误删另一个正在生成的快照。"""
    if seal_number and stale_after is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_after)).isoformat()
        return db.execute("DELETE FROM seal_versions WHERE status='pending' AND seal_number=? AND sealed_at<?", (seal_number, cutoff)).rowcount
    if seal_number:
        return db.execute("DELETE FROM seal_versions WHERE status='pending' AND seal_number=?", (seal_number,)).rowcount
    return db.execute("DELETE FROM seal_versions WHERE status='pending'").rowcount


def _seal_view(run, version, reused):
    return {"run_id": run["run_id"], "seal_number": run["seal_number"], "version": version, "status": "sealed", "sealed_at": run.get("sealed_at"), "reused": reused, "deliveries": len(run.get("deliveries", []))}


def seal(run_id):
    """提交一份月末封存快照。

    同一状态重复封存复用已有版本；换序原件到达或复核变化时在同一编号下
    追加版本；失败或中断后重试沿用已有版本号，不会跳号也不会新建编号。
    """
    run = get(run_id)
    seal_number = run.get("seal_number") or run.get("manifest", {}).get("seal_number")
    if not seal_number:
        raise DomainError("NO_SEAL_NUMBER", "批次缺少月末封存编号", 409)
    fingerprint = _seal_fingerprint(run)
    # 第一步：只落“正在生成”的意图并提交。证据生成失败或进程中断时，
    # pending 留库而旧版 sealed 证据完全不受影响。
    with connect() as db:
        # 仅清理超过租约期的残留；另一个正在生成的快照由唯一版本号挡住。
        _discard_pending(db, seal_number, stale_after=PENDING_LEASE_SECONDS)
        latest = _latest_sealed(db, seal_number)
        if latest and latest[1] == fingerprint:
            db.execute("UPDATE runs SET sealed_version=?,sealed_at=? WHERE id=?", (latest[0], latest[3], run_id))
            run.update(sealed_version=latest[0], sealed_at=latest[3])
            return _seal_view(run, latest[0], True)
        version = (latest[0] if latest else 0) + 1
        pending = db.execute(
            "SELECT fingerprint FROM seal_versions WHERE seal_number=? AND version=? AND status='pending'",
            (seal_number, version),
        ).fetchone()
        if pending is None:
            try:
                db.execute(
                    "INSERT INTO seal_versions(seal_number,version,run_id,content_sha256,fingerprint,status,sealed_at,evidence) VALUES (?,?,?,?,?,'pending',?,'')",
                    (seal_number, version, run_id, run.get("content_sha256"), fingerprint, _now()),
                )
            except sqlite3.IntegrityError:
                raise DomainError("SEAL_BUSY", "封存版本正在生成，请稍后重试", 409)
        elif pending[0] != fingerprint:
            # 同一编号下一版本已被别的证据占用，不得覆盖。
            raise DomainError("SEAL_BUSY", "封存版本正在生成，请稍后重试", 409)
    evidence = build_archive(run, seal_number=seal_number, seal_version=version)
    # 第二步：定稿并推进批次指针，同一事务提交，任一失败整体回滚。
    with connect() as db:
        cursor = db.execute(
            "UPDATE seal_versions SET status='sealed',evidence=?,sealed_at=? WHERE seal_number=? AND version=? AND status='pending'",
            (evidence, _now(), seal_number, version),
        )
        if cursor.rowcount != 1:
            sealed = db.execute(
                "SELECT fingerprint,sealed_at FROM seal_versions WHERE seal_number=? AND version=? AND status='sealed'",
                (seal_number, version),
            ).fetchone()
            if not sealed or sealed[0] != fingerprint:
                raise DomainError("SEAL_CONFLICT", "封存版本已被其他操作占用，请重试", 409)
            reused = True
        else:
            reused = False
        row = db.execute("SELECT version,sealed_at FROM seal_versions WHERE seal_number=? AND version=?", (seal_number, version)).fetchone()
        db.execute("UPDATE runs SET seal_number=?,sealed_version=?,sealed_at=? WHERE id=?", (seal_number, row[0], row[1], run_id))
    run.update(sealed_version=row[0], sealed_at=row[1])
    return _seal_view(run, row[0], reused)


def recover():
    """清理中断遗留的 pending 快照，并把批次指针恢复到最新已封存版本。"""
    with connect() as db:
        discarded = _discard_pending(db)
        restored = 0
        for (seal_number,) in db.execute("SELECT DISTINCT seal_number FROM seal_versions WHERE status='sealed'").fetchall():
            latest = _latest_sealed(db, seal_number)
            if not latest:
                continue
            cursor = db.execute("UPDATE runs SET sealed_version=?,sealed_at=? WHERE seal_number=? AND (sealed_version IS NULL OR sealed_version!=?)", (latest[0], latest[3], seal_number, latest[0]))
            restored += cursor.rowcount
    return {"recovered": True, "discarded_pending": discarded, "restored_runs": restored}


def seal_versions(seal_number=None):
    with connect() as db:
        if seal_number:
            rows = db.execute("SELECT seal_number,version,run_id,status,sealed_at,length(evidence) FROM seal_versions WHERE seal_number=? ORDER BY version", (seal_number,))
        else:
            rows = db.execute("SELECT seal_number,version,run_id,status,sealed_at,length(evidence) FROM seal_versions ORDER BY seal_number,version")
        return [{"seal_number": s, "version": v, "run_id": r, "status": st, "sealed_at": t, "evidence_bytes": n} for s, v, r, st, t, n in rows]


def sealed_evidence(seal_number, version=None):
    with connect() as db:
        if version is None:
            row = db.execute("SELECT version,sealed_at,evidence FROM seal_versions WHERE seal_number=? AND status='sealed' ORDER BY version DESC LIMIT 1", (seal_number,)).fetchone()
        else:
            row = db.execute("SELECT version,sealed_at,evidence FROM seal_versions WHERE seal_number=? AND version=? AND status='sealed'", (seal_number, version)).fetchone()
    if not row:
        raise DomainError("NOT_FOUND", "封存版本不存在", 404)
    return {"seal_number": seal_number, "version": row[0], "sealed_at": row[1], "evidence": row[2]}


def seals():
    with connect() as db:
        rows = db.execute(
            "SELECT s.seal_number,MAX(s.version),s.run_id,MAX(s.sealed_at),(SELECT COUNT(*) FROM deliveries d WHERE d.run_id=s.run_id) FROM seal_versions s WHERE s.status='sealed' GROUP BY s.seal_number,s.run_id ORDER BY s.seal_number"
        ).fetchall()
    return [{"seal_number": s, "latest_version": v, "run_id": r, "sealed_at": t, "deliveries": n} for s, v, r, t, n in rows]


def deliveries(run_id=None):
    with connect() as db:
        if run_id:
            return _delivery_rows(db, run_id)
        return [
            {"delivery_id": d, "run_id": r, "input_sha256": h, "params": json.loads(p or "{}"), "source": s, "received_at": t, "byte_size": n}
            for d, r, h, p, s, t, n in db.execute("SELECT delivery_id,run_id,input_sha256,params,source,received_at,byte_size FROM deliveries ORDER BY rowid")
        ]


def delivery_raw(delivery_id):
    with connect() as db:
        row = db.execute("SELECT run_id,input_sha256,source,received_at,raw FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
    if not row:
        raise DomainError("NOT_FOUND", "投递不存在", 404)
    return {"delivery_id": delivery_id, "run_id": row[0], "input_sha256": row[1], "source": row[2], "received_at": row[3], "raw": row[4]}


def delivery_mapping(seal_number=None):
    """投递与月末封存编号的对应关系，供导出与工作台核对。"""
    with connect() as db:
        sql = "SELECT r.seal_number,r.content_sha256,d.run_id,d.delivery_id,d.input_sha256,d.source,d.received_at,d.byte_size FROM deliveries d JOIN runs r ON r.id=d.run_id"
        args = ()
        if seal_number:
            sql += " WHERE r.seal_number=?"
            args = (seal_number,)
        sql += " ORDER BY d.rowid"
        return [
            {"seal_number": s, "content_sha256": c, "run_id": r, "delivery_id": d, "input_sha256": h, "source": src, "received_at": t, "byte_size": n}
            for s, c, r, d, h, src, t, n in db.execute(sql, args)
        ]
