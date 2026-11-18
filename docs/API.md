# 接口

模块GET使用仓内样例，模块POST可传csv或rows与context，直接计算当前规则。/api/runs使用完整流水线并原子保存。每个阶段返回rows、quarantine、metrics；后续阶段还返回累计隔离。/api/upload接收multipart文件file。

## 批次与证据

- `POST /api/runs`：接受sample或csv，返回批次。批次身份取自内容摘要，换行顺序重投返回同一 `run_id` 与 `seal_number`。
- `GET /api/runs`：历史批次，含seal_number、封存版本与投递次数。
- `GET /api/runs/<id>`：完整批次，含 `deliveries` 原始投递清单。
- `POST /api/runs/<id>/review`：需要version与decision，失效版本返回409。
- `GET /api/runs/<id>/archive`：实时证据CSV，含带清单、隔离行与投递清单。

## 月末封存

- `POST /api/runs/<id>/seal`：封存当前状态。同一状态重复封存复用已有版本（`reused:true`）；状态变化或新原件到达时在同一编号下追加版本。
- `GET /api/runs/<id>/seal`：最新封存证据。
- `GET /api/seals`：所有月末封存编号。
- `GET /api/seals/<编号>`：编号下的版本与投递。
- `GET /api/seal-versions/<编号>?version=N`：指定版本的封存证据，旧版本始终可导出。
- `GET /api/seals/<编号>/deliveries`、`GET /api/seal-map.csv`：投递与封存编号的对应关系CSV。
- `GET /api/deliveries`、`GET /api/deliveries/<投递编号>/raw`：原始投递清单与原件下载。
- `POST /api/recover`：丢弃中断遗留的pending快照，把批次指针恢复到最新已封存版本；启动时也会自动执行。
