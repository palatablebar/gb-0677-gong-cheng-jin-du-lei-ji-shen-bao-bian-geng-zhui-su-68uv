import csv,io
import pytest

from app import store
from app.archive import build_archive
from app.errors import DomainError
from app.pipeline import calculate,execute,load_sample
from app.identity import delivery_digest


def reorder(raw):
    """按 CSV 逻辑记录换行顺序重投，保留引号内换行。"""
    text = raw.decode("utf-8-sig")
    records = list(csv.reader(io.StringIO(text, newline="")))
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(records[0])
    for record in reversed(records[1:]):
        writer.writerow(record)
    return stream.getvalue().encode()


def test_reordered_delivery_shares_batch_and_seal_number():
    raw = load_sample("clean.csv")
    first = execute(raw, source="upload:first.csv")
    second = execute(reorder(raw), source="upload:second.csv")
    assert first["run_id"] == second["run_id"]
    assert first["seal_number"] == second["seal_number"]
    assert len(store.history()) == 1
    assert len(store.deliveries()) == 2


def test_reordered_legacy_delivery_shares_batch_and_seal_number():
    raw = load_sample("legacy.csv")
    first = execute(raw)
    second = execute(reorder(raw.decode("gb18030").encode("utf-8")), source="upload:reencoded.csv")
    assert first["run_id"] == second["run_id"]
    assert first["seal_number"] == second["seal_number"]
    assert len(store.history()) == 1
    assert len(store.deliveries()) == 2


def test_reordered_delivery_keeps_both_originals():
    raw = load_sample("clean.csv")
    reordered = reorder(raw)
    execute(raw, source="upload:first.csv")
    execute(reordered, source="upload:second.csv")
    originals = {store.delivery_raw(d["delivery_id"])["raw"] for d in store.deliveries()}
    assert originals == {raw, reordered}
    assert len({d["input_sha256"] for d in store.deliveries()}) == 2


def test_retrying_same_delivery_adds_nothing():
    raw = load_sample("clean.csv")
    execute(raw, source="upload:same.csv")
    execute(raw, source="upload:same.csv")
    assert len(store.deliveries()) == 1
    assert len(store.history()) == 1


def test_delivery_digest_is_stable_for_same_bytes():
    raw = load_sample("clean.csv")
    assert delivery_digest(raw, {}) == delivery_digest(raw, {})
    assert delivery_digest(raw, {}) != delivery_digest(reorder(raw), {})


def test_delivery_map_export_names_seal_number():
    run = execute(load_sample("clean.csv"))
    store.seal(run["run_id"])
    mapping = store.delivery_mapping(run["seal_number"])
    assert mapping and all(m["seal_number"] == run["seal_number"] for m in mapping)


def test_mapping_endpoint_exports_csv(client):
    from app.main import create_app
    p = client.post("/api/runs", json={"sample": "clean.csv"}).get_json()
    client.post("/api/runs/" + p["run_id"] + "/seal")
    response = client.get("/api/seals/" + p["seal_number"] + "/deliveries")
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
    assert rows[0][0] == "seal_number" and rows[1][0] == p["seal_number"]
    assert rows[1][4] == p["manifest"]["input_sha256"]


def test_reseal_same_state_is_idempotent(sample):
    run = execute(sample)
    first = store.seal(run["run_id"])
    again = store.seal(run["run_id"])
    assert first["version"] == again["version"] == 1
    assert again["reused"] is True
    assert len(store.seal_versions(run["seal_number"])) == 1


def test_review_then_seal_versions_forward(sample):
    run = execute(sample)
    v1 = store.seal(run["run_id"])
    store.review(run["run_id"], 1, "已复核")
    v2 = store.seal(run["run_id"])
    assert (v1["version"], v2["version"]) == (1, 2)
    old = store.sealed_evidence(run["seal_number"], 1)
    new = store.sealed_evidence(run["seal_number"], 2)
    assert old["evidence"] != new["evidence"]
    assert store.get(run["run_id"])["sealed_version"] == 2


def test_interrupted_seal_preserves_old_and_continues(sample):
    run = execute(sample)
    store.seal(run["run_id"])
    # 模拟中断：留有一条未定稿快照，批次指针已被推高。
    with store.connect() as db:
        db.execute(
            "INSERT INTO seal_versions(seal_number,version,run_id,content_sha256,fingerprint,status,sealed_at,evidence) VALUES (?,?,?,?,?,'pending',?,?)",
            (run["seal_number"], 2, run["run_id"], run["content_sha256"], "broken", "2026-11-30T00:00:00+00:00", "partial"),
        )
        db.execute("UPDATE runs SET sealed_version=2 WHERE id=?", (run["run_id"],))
    assert store.recover()["discarded_pending"] == 1
    assert store.get(run["run_id"])["sealed_version"] == 1
    assert store.sealed_evidence(run["seal_number"], 1)["evidence"]
    # 重试同一投递：沿用已有封存版本，不因中断新增版本。
    retried = store.seal(run["run_id"])
    assert retried["version"] == 1 and retried["reused"] is True
    assert len(store.seal_versions(run["seal_number"])) == 1


def test_recover_restores_pointer_after_crash(sample):
    run = execute(sample)
    store.seal(run["run_id"])
    with store.connect() as db:
        db.execute("UPDATE runs SET sealed_version=9 WHERE id=?", (run["run_id"],))
    assert store.recover()["restored_runs"] == 1
    assert store.get(run["run_id"])["sealed_version"] == 1


def test_seal_unknown_version_404(sample):
    run = execute(sample)
    store.seal(run["run_id"])
    with pytest.raises(DomainError) as error:
        store.sealed_evidence(run["seal_number"], 7)
    assert error.value.status == 404


def test_workbench_shows_seal_number(client):
    p = client.post("/api/runs", json={"sample": "legacy.csv"}).get_json()
    client.post("/api/runs/" + p["run_id"] + "/seal")
    page = client.get("/certificates")
    assert p["seal_number"].encode() in page.data
    assert len(store.seals()) == 1


def test_seal_endpoint_reports_version(client):
    p = client.post("/api/runs", json={"sample": "clean.csv"}).get_json()
    response = client.post("/api/runs/" + p["run_id"] + "/seal")
    assert response.status_code == 200
    body = response.get_json()
    assert body["seal_number"] == p["seal_number"] and body["version"] == 1
    assert client.get("/api/runs/" + p["run_id"] + "/seal").status_code == 200


def test_failed_seal_keeps_old_evidence_and_resumes(sample, monkeypatch):
    run = execute(sample)
    store.seal(run["run_id"])
    old = store.sealed_evidence(run["seal_number"], 1)["evidence"]
    store.review(run["run_id"], 1, "已复核")

    def explode(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "build_archive", explode)
    with pytest.raises(RuntimeError):
        store.seal(run["run_id"])
    # 失败不覆盖旧版定稿证据，新版本只停留在未定稿状态。
    assert store.sealed_evidence(run["seal_number"], 1)["evidence"] == old
    assert [v["status"] for v in store.seal_versions(run["seal_number"])] == ["sealed", "pending"]
    assert store.get(run["run_id"])["sealed_version"] == 1

    monkeypatch.setattr(store, "build_archive", build_archive)
    assert store.recover()["discarded_pending"] == 1
    assert store.get(run["run_id"])["sealed_version"] == 1
    # 恢复后从已有版本续号，而不是从 1 重来。
    resumed = store.seal(run["run_id"])
    assert resumed["version"] == 2
    assert store.sealed_evidence(run["seal_number"], 1)["evidence"] == old


def test_retry_after_failure_finalizes_same_version(sample, monkeypatch):
    run = execute(sample)
    store.seal(run["run_id"])
    store.review(run["run_id"], 1, "已复核")
    real = store.build_archive

    def explode(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "build_archive", explode)
    with pytest.raises(RuntimeError):
        store.seal(run["run_id"])
    monkeypatch.setattr(store, "build_archive", real)
    # 不显式恢复：重试同一状态直接定稿那条 pending，不新增版本。
    retried = store.seal(run["run_id"])
    assert retried["version"] == 2
    assert [v["status"] for v in store.seal_versions(run["seal_number"])] == ["sealed", "sealed"]


def test_retry_after_interruption_does_not_add_version(sample):
    run = execute(sample)
    store.seal(run["run_id"])
    # 模拟中断：未定稿快照已落库，证据未生成。
    with store.connect() as db:
        db.execute(
            "INSERT INTO seal_versions(seal_number,version,run_id,content_sha256,fingerprint,status,sealed_at,evidence) VALUES (?,?,?,?,?,'pending',?,'')",
            (run["seal_number"], 2, run["run_id"], run["content_sha256"], "broken", "2026-11-30T00:00:00+00:00"),
        )
    store.recover()
    retried = store.seal(run["run_id"])
    assert retried["version"] == 1 and retried["reused"] is True
    assert len(store.seal_versions(run["seal_number"])) == 1


def test_reordered_redelivery_after_seal_stays_under_one_number():
    raw = load_sample("clean.csv")
    run = execute(raw, source="upload:first.csv")
    first = store.seal(run["run_id"])
    execute(reorder(raw), source="upload:second.csv")
    again = store.seal(run["run_id"])
    # 同一组内容始终一个月末编号；后到的原件在同一编号下追加封存版本。
    assert again["seal_number"] == first["seal_number"]
    assert again["version"] == 2 and again["reused"] is False
    assert len({s["seal_number"] for s in store.seals()}) == 1
    # 旧版证据仍可恢复，且新版证据同时含两份原件。
    assert store.sealed_evidence(first["seal_number"], 1)["evidence"]
    latest = store.sealed_evidence(first["seal_number"], 2)["evidence"]
    assert latest.count(",delivery,") == 2
    assert len(store.delivery_mapping(first["seal_number"])) == 2


def test_seal_map_lists_every_delivery_under_one_number():
    raw = load_sample("clean.csv")
    execute(raw, source="upload:a.csv")
    execute(reorder(raw), source="upload:b.csv")
    mappings = store.delivery_mapping()
    assert len(mappings) == 2
    assert len({m["seal_number"] for m in mappings}) == 1
    assert len({m["delivery_id"] for m in mappings}) == 2


def test_distinct_content_gets_distinct_seal_number():
    dirty = execute(load_sample("dirty.csv"))
    clean = execute(load_sample("clean.csv"))
    assert dirty["seal_number"] != clean["seal_number"]
    assert {s["seal_number"] for s in store.seals()} == set()


def test_legacy_run_without_identity_is_backfilled():
    import json
    raw = load_sample("clean.csv")
    fresh = calculate(raw)
    payload = {k: v for k, v in fresh.items() if k not in ("content_sha256", "seal_number")}
    with store.connect() as db:
        db.execute("INSERT INTO runs(id,payload) VALUES (?,?)", ("legacy1", json.dumps(payload, ensure_ascii=False)))
    recovered = store.get("legacy1")
    # 旧库没有封存字段：按已存结果补算，不改写旧证据正文。
    assert recovered["content_sha256"] == fresh["content_sha256"]
    assert recovered["seal_number"] == fresh["seal_number"]
    assert recovered["version"] == 1 and recovered["decision"] == "待复核"
