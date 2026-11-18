import csv,io,unicodedata
from app.config import COLUMNS
from app.errors import DomainError
from app.reasons import reject
from app.items import ITEMS
def decode(raw):
    for encoding in ("utf-8-sig","gb18030"):
        try: return raw.decode(encoding),encoding
        except UnicodeDecodeError: continue
    raise DomainError("ENCODING","无法识别文件编码")
def _canonical(cell):
    return unicodedata.normalize("NFKC",str(cell)).strip()
def _order(cells):
    """与投递行序无关的规范顺序：同一组记录无论怎么排都得到同一结果。

    清单项先归一为登记编码，再比较其余字段，避免“钢筋/001”等别名
    在换序投递后裁决出不同赢家。"""
    cells=[_canonical(c) for c in cells]
    item=ITEMS.get(cells[1],cells[1]) if len(cells)>1 else ""
    rest=cells[:1]+cells[2:] if len(cells)>1 else cells
    return (cells[0] if cells else "",item,tuple(rest))
def evaluate(raw,context=None):
    text,encoding=decode(raw)
    if not text: raise DomainError("EMPTY_FILE","文件为空")
    if not text.strip(): raise DomainError("BLANK_ONLY","全部为空行")
    stream=csv.reader(io.StringIO(text,newline=""));header=next(stream,None)
    if header!=COLUMNS: raise DomainError("BAD_HEADER","表头不匹配")
    winners={};losers=[];bad=[];duplicate=blank=repeated=count=0
    try:
        for cells in stream:
            count+=1;line=stream.line_num
            if not cells or not any(c.strip() for c in cells): blank+=1;continue
            if cells==header: repeated+=1;continue
            row={"_line":line,"_sources":[line],"_raw":cells}
            if len(cells)!=len(header): bad.append(reject(row,"BAD_COLUMNS","列数不匹配"));continue
            row.update(dict(zip(header,[x.strip() for x in cells])))
            if not row[header[0]]: bad.append(reject(row,"EMPTY_VALUE","业务键为空"));continue
            key=row[header[0]]
            chosen=winners.get(key)
            if chosen is None: winners[key]=row;continue
            if cells==chosen["_raw"]: duplicate+=1;continue
            # 同键冲突：按规范顺序保留，不因投递行序改变采纳结果。
            if _order(cells)<_order(chosen["_raw"]):
                losers.append(reject(chosen,"DUPLICATE_CONFLICT","同键不同值"))
                winners[key]=row
            else: losers.append(reject(row,"DUPLICATE_CONFLICT","同键不同值"))
    except csv.Error: raise DomainError("BAD_CSV","引号未闭合或文件损坏")
    if count==0: raise DomainError("HEADER_ONLY","仅有表头")
    if not winners and not bad and not losers: raise DomainError("BLANK_ONLY","全部为空行")
    rows=sorted(winners.values(),key=lambda r:_order(r["_raw"]))
    bad=sorted(bad+losers,key=lambda r:_order([str(x) for x in r.get("_raw",[])]))
    return {"rows":rows,"quarantine":bad,"metrics":{"input":count,"accepted":len(rows),"quarantine":len(bad),"duplicate":duplicate,"blank":blank,"repeated_headers":repeated,"encoding":encoding}}
