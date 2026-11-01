"""
全量普查：抓遍 women 清仓的每一页，收集每件商品的 asOf。

这是研究工具，不属于 15 分钟监控回路。用途是对整个库存做一次无偏快照，
消除"只看第 1 页"的选择偏差，用来验证后台刷新规律。

约 98 页 / 29MB / 200 秒。输出带时间戳存到 findings/census/。

    python3 tools/census.py            # 跑一次普查
    python3 tools/analyze_census.py <file.json>   # 分析
"""
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pagewatch.http import fetch_text, FetchError
from pagewatch.watchers.nordstrom_rack import extract_initial_config, ParseError

def _base_url() -> str:
    """
    从 config.toml 里取主 watcher 的 URL 作为普查起点。

    不硬编码 —— URL 里带着邮编和门店号这类个人信息，写死在代码里会进版本库。
    """
    from pagewatch.config import load
    cfg = load()
    name = "nrack_women_top"
    w = cfg.watchers.get(name) or next(iter(cfg.enabled_watchers().values()))
    return re.sub(r"&page=\d+", "", w["url"])


B = _base_url()

items={}          # pid -> dict
pages_ok=0; pages_bad=0; wire=0
t0=time.time()
page=1
expected=None
while page <= 120:
    url=f"{B}&page={page}"
    try:
        html,w,_,_st=fetch_text(url, timeout=40); wire+=w
        pr=extract_initial_config(html)['productResults']
    except (FetchError, ParseError, KeyError) as ex:
        print(f"  page {page}: 失败 {ex} — 重试一次", flush=True)
        time.sleep(3)
        try:
            html,w,_,_st=fetch_text(url, timeout=40); wire+=w
            pr=extract_initial_config(html)['productResults']
        except Exception as ex2:
            print(f"  page {page}: 再次失败，跳过 ({ex2})", flush=True)
            pages_bad+=1; page+=1; continue

    rc=pr['query'].get('resultCount')
    if expected is None: expected=rc
    # 残缺结果集检测：总数偏离首页超过 50% 就重试
    if rc and expected and abs(rc-expected)/expected > 0.5:
        print(f"  page {page}: 残缺结果集 total={rc}（期望≈{expected}），重试", flush=True)
        time.sleep(4)
        try:
            html,w,_,_st=fetch_text(url, timeout=40); wire+=w
            pr=extract_initial_config(html)['productResults']
            rc=pr['query'].get('resultCount')
        except Exception: pass
        if rc and abs(rc-expected)/expected > 0.5:
            pages_bad+=1; page+=1; continue

    ui=pr['productUi']['byId']
    if not ui:
        # 站点偶尔会返回空页。不能一见空就收尾——实测因此在第 45 页提前中断，
        # 只拿到 3165/6900 件。重试两次确认是真的到底了再停。
        confirmed_empty=True
        for attempt in range(2):
            time.sleep(3)
            try:
                html,w,_,_st=fetch_text(url, timeout=40); wire+=w
                pr2=extract_initial_config(html)['productResults']
                if pr2['productUi']['byId']:
                    pr=pr2; ui=pr['productUi']['byId']; confirmed_empty=False
                    print(f"  page {page}: 空页是假的，重试第 {attempt+1} 次拿到数据", flush=True)
                    break
            except Exception: pass
        if confirmed_empty:
            # 预期页数 = resultCount/72。远没到就不该停。
            if rc and page < rc/72*0.9:
                print(f"  page {page}: 连续空页，但预期约 {rc/72:.0f} 页，跳过继续", flush=True)
                pages_bad+=1; page+=1; continue
            print(f"  page {page}: 确认到底，收尾", flush=True); break
    for e in ui.values():
        pid=str(e['productId']); p=pr['productsById'].get(pid)
        if not p: continue
        pr0=p['propositions'][0]; av=pr0.get('availability') or {}
        items[pid]=dict(
            asOf=pr0.get('asOf'),
            price=(pr0.get('sellingRetailPriceRange') or {}).get('min'),
            base=(pr0.get('baseRetailPriceRange') or {}).get('max'),
            cmp=(pr0.get('compareAtRetailPriceRange') or {}).get('max'),
            pct=(pr0.get('sellingRetailPercentageOffRange') or {}).get('max'),
            pctbase=(pr0.get('clearancePercentageOffBaseRetailRange') or {}).get('max'),
            ship=av.get('shipQuantity'),
            tags=sorted(t.get('type','') for t in (pr0.get('enticementTags') or [])),
            brand=p.get('labelDisplayName'), title=p.get('copyProductTitle'))
    pages_ok+=1
    if page % 10 == 0:
        print(f"  已抓 {page} 页，累计 {len(items)} 件，{wire/1048576:.0f}MB，"
              f"{time.time()-t0:.0f}s", flush=True)
    page+=1
    time.sleep(0.4)

print(f"\n完成：{pages_ok} 页成功 / {pages_bad} 页丢弃，共 {len(items)} 件，"
      f"{wire/1048576:.1f}MB，耗时 {time.time()-t0:.0f}s", flush=True)
out = ROOT / "findings" / "census" / f"{time.strftime('%Y-%m-%dT%H%M')}-women.json"
out.parent.mkdir(parents=True, exist_ok=True)
json.dump(items, open(out, 'w'))
print(f"已写入 {out}", flush=True)
