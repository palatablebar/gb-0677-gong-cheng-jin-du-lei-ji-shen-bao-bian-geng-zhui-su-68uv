from flask import Flask,jsonify,request
from app.config import MODULES
from app.errors import DomainError
from app import pipeline,store
from app.web import register_pages
import importlib
def create_app():
    application=Flask(__name__,template_folder="templates",static_folder="static")
    application.config["MAX_CONTENT_LENGTH"]=4*1024*1024
    # 上次进程中断可能留下未定稿快照；启动时恢复，旧版已封存证据保持可读。
    try: store.recover()
    except Exception: pass
    @application.errorhandler(DomainError)
    def domain_error(error): return jsonify({"code":error.code,"message":error.message}),error.status
    @application.errorhandler(KeyError)
    @application.errorhandler(TypeError)
    @application.errorhandler(ValueError)
    def invalid_shape(error): return jsonify({"code":"INVALID_INPUT","message":"字段或参数无效，请核对输入"}),422
    @application.errorhandler(413)
    def too_large(error): return jsonify({"code":"TOO_LARGE","message":"文件超过4MB"}),413
    @application.get("/healthz")
    def health(): return {"status":"ok"}
    @application.post("/api/runs")
    def runs():
        payload=request.get_json(silent=True)
        if not isinstance(payload,dict): raise DomainError("BAD_BODY","请求需要对象")
        params=payload.get("parameters",{})
        if not isinstance(params,dict): raise DomainError("BAD_PARAMETERS","参数需要对象")
        raw=payload.get("csv")
        if raw is not None and not isinstance(raw,str): raise DomainError("BAD_CSV","CSV需要文本")
        sample=payload.get("sample","dirty.csv")
        if raw is not None: return pipeline.execute(raw.encode(),params,source="api:csv")
        return pipeline.execute(pipeline.load_sample(sample),params,source="sample:"+sample)
    @application.post("/api/upload")
    def upload():
        file=request.files.get("file")
        if not file: raise DomainError("NO_FILE","请选择CSV文件")
        return pipeline.execute(file.read(),source="upload:"+(file.filename or "csv"))
    @application.get("/api/runs")
    def history(): return {"runs":store.history()}
    @application.get("/api/runs/<run_id>")
    def run_detail(run_id): return store.get(run_id)
    @application.post("/api/runs/<run_id>/review")
    def review(run_id):
        p=request.get_json(silent=True) or {}
        if not isinstance(p.get("version"),int): raise DomainError("BAD_VERSION","请提供版本号")
        return store.review(run_id,p["version"],p.get("decision"))
    @application.get("/api/runs/<run_id>/archive")
    def archive(run_id):
        from app.archive import build_archive
        run=store.get(run_id)
        return application.response_class(build_archive(run),mimetype="text/csv",headers={"Content-Disposition":'attachment; filename="evidence.csv"'})
    # 月末封存：同一组申报内容只有一个编号，版本随原件到达或复核追加。
    @application.post("/api/runs/<run_id>/seal")
    def seal_run(run_id): return store.seal(run_id)
    @application.get("/api/runs/<run_id>/seal")
    def sealed_evidence(run_id):
        run=store.get(run_id)
        latest=store.sealed_evidence(run["seal_number"])
        return application.response_class(latest["evidence"],mimetype="text/csv",headers={"Content-Disposition":f'attachment; filename="{run["seal_number"]}-v{latest["version"]}.csv"'})
    @application.get("/api/seal-versions/<seal_number>")
    def seal_version(seal_number):
        version=request.args.get("version",type=int)
        item=store.sealed_evidence(seal_number,version)
        return application.response_class(item["evidence"],mimetype="text/csv",headers={"Content-Disposition":f'attachment; filename="{seal_number}-v{item["version"]}.csv"'})
    @application.get("/api/seals")
    def seals(): return {"seals":store.seals()}
    @application.get("/api/seals/<seal_number>")
    def seal_detail(seal_number):
        return {"seal_number":seal_number,"versions":store.seal_versions(seal_number),"deliveries":store.delivery_mapping(seal_number)}
    @application.get("/api/seals/<seal_number>/deliveries")
    def seal_deliveries(seal_number):
        from app.archive import build_delivery_map
        return application.response_class(build_delivery_map(store.delivery_mapping(seal_number)),mimetype="text/csv",headers={"Content-Disposition":f'attachment; filename="{seal_number}-deliveries.csv"'})
    # 投递与封存编号的对应关系（工作台导出）。
    @application.get("/api/seal-map")
    def seal_map(): return {"mappings":store.delivery_mapping(request.args.get("seal"))}
    @application.get("/api/seal-map.csv")
    def seal_map_csv():
        from app.archive import build_delivery_map
        return application.response_class(build_delivery_map(store.delivery_mapping(request.args.get("seal"))),mimetype="text/csv",headers={"Content-Disposition":'attachment; filename="delivery-seal-map.csv"'})
    @application.get("/api/deliveries")
    def deliveries(): return {"deliveries":store.deliveries(request.args.get("run_id"))}
    @application.get("/api/deliveries/<delivery_id>/raw")
    def delivery_raw(delivery_id):
        item=store.delivery_raw(delivery_id)
        return application.response_class(item["raw"],mimetype="text/csv",headers={"Content-Disposition":f'attachment; filename="{delivery_id[:12]}.csv"'})
    @application.post("/api/recover")
    def recover(): return store.recover()
    for index,(slug,title) in enumerate(MODULES,1):
        module=importlib.import_module(f"app.modules.m{index}_{slug}.handler")
        application.register_blueprint(module.bp)
    register_pages(application)
    return application
app=create_app()
if __name__=="__main__": app.run(host="0.0.0.0",port=8000,threaded=True)
