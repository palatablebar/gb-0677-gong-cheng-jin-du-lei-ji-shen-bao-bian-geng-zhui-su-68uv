import time
from app.config import ROOT,MODULES
from app.errors import DomainError
from app.values import scalar
from app.manifest import manifest
from app.identity import content_digest,seal_number as make_seal_number,seal_month,delivery_digest
from app import store
from app.modules.m1_claims.rules import evaluate as stage1
from app.modules.m2_billitems.rules import evaluate as stage2
from app.modules.m3_changes.rules import evaluate as stage3
from app.modules.m4_increment.rules import evaluate as stage4
from app.modules.m5_deductions.rules import evaluate as stage5
from app.modules.m6_valuation.rules import evaluate as stage6
from app.modules.m7_certificates.rules import evaluate as stage7
STAGES=[stage1,stage2,stage3,stage4,stage5,stage6,stage7]
def load_sample(name):
    if name not in {"dirty.csv","clean.csv","legacy.csv"}: raise DomainError("BAD_SAMPLE","样例不存在",404)
    return (ROOT/"sample_data"/name).read_bytes()
def calculate(raw,params=None,stop=6):
    params=params or {};started=time.monotonic();context={"parameters":params,"raw":raw,"quarantine":[]};stages=[];rows=[]
    for index,stage in enumerate(STAGES[:stop+1]):
        result=stage(raw if index==0 else rows,context)
        result=scalar(result);rows=result["rows"]
        context["quarantine"].extend(result.get("quarantine",[]))
        if index==0: context["intake"]=result["metrics"]
        result["cumulative_quarantine"]=list(context["quarantine"])
        context[MODULES[index][0]]=result
        stages.append(result)
    # 批次身份取自业务内容而非文件字节：换行顺序重投仍落到同一批次与同一封存编号。
    content=content_digest(stages,params)
    final_rows=stages[-1]["rows"]
    seal=make_seal_number(content,final_rows) if stop>=len(STAGES)-1 else None
    review_rows=final_rows
    meta=manifest(raw,params,started,content_sha256=content,seal_number=seal,seal_month=seal_month(review_rows) if seal else None,delivery_sha256=delivery_digest(raw,params))
    return {"run_id":content[:20],"stages":stages,"manifest":meta,"content_sha256":content,"seal_number":seal,"parameters":params}
def execute(raw,params=None,source=None):
    params=params or {}
    result=calculate(raw,params)
    return store.save(result["run_id"],result,delivery={"raw":raw,"params":params,"source":source})
def stage_preview(index,payload):
    if "rows" in payload and index>0:
        if not isinstance(payload["rows"],list) or any(not isinstance(row,dict) for row in payload["rows"]): raise DomainError("BAD_ROWS","记录需要对象列表")
        if not isinstance(payload.get("context",{}),dict): raise DomainError("BAD_CONTEXT","上下文需要对象")
        return scalar(STAGES[index](payload["rows"],payload.get("context",{"parameters":{},"quarantine":[]})))
    raw=payload.get("csv")
    raw=raw.encode() if isinstance(raw,str) else load_sample(payload.get("sample","dirty.csv"))
    return calculate(raw,payload.get("parameters",{}),index)["stages"][-1]
