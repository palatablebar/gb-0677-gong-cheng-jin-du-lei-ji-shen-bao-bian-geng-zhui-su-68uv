import csv,io,json
def build_archive(run,seal_number=None,seal_version=None):
    """一份完整证据：来源行、隔离行与本次投递清单，末列之外保持旧格式兼容。"""
    stream=io.StringIO(newline="");writer=csv.writer(stream)
    writer.writerow(["run_id","input_sha256","rule_version","kind","source_lines","reason","evidence","seal_number","seal_version","content_sha256"])
    meta=run["manifest"]
    seal=seal_number or run.get("seal_number") or meta.get("seal_number") or ""
    sealed=seal_version if seal_version is not None else (run.get("sealed_version") or "")
    content=run.get("content_sha256") or meta.get("content_sha256") or ""
    def emit(kind,row,reason):
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],kind,json.dumps(row.get("_sources",[])),reason,json.dumps(row,ensure_ascii=False),seal,sealed,content])
    for row in run["stages"][-1]["rows"]: emit("accepted",row,row.get("status",""))
    for row in run["stages"][-1].get("cumulative_quarantine",[]): emit("quarantine",row,row.get("reason",""))
    for delivery in run.get("deliveries",[]): emit("delivery",{"_sources":[],**delivery},"")
    return "﻿"+stream.getvalue()
def build_delivery_map(rows):
    """投递与月末封存编号的对应关系。"""
    stream=io.StringIO(newline="");writer=csv.writer(stream)
    writer.writerow(["seal_number","content_sha256","run_id","delivery_id","input_sha256","source","received_at","byte_size"])
    for row in rows:
        writer.writerow([row.get("seal_number",""),row.get("content_sha256",""),row.get("run_id",""),row.get("delivery_id",""),row.get("input_sha256",""),row.get("source",""),row.get("received_at",""),row.get("byte_size","")])
    return "﻿"+stream.getvalue()
