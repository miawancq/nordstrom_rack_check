import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
ET=ZoneInfo("America/New_York"); PT=ZoneInfo("America/Los_Angeles")
path = sys.argv[1] if len(sys.argv) > 1 else None
if not path:
    import glob, os
    cands = sorted(glob.glob(str(ROOT / "findings" / "census" / "*.json")))
    if not cands:
        sys.exit("没有普查数据，先跑 python3 tools/census.py")
    path = cands[-1]
    print(f"（未指定文件，用最新的 {os.path.basename(path)}）\n")
items=json.load(open(path))
now=datetime.now(ET)
rows=[]
for pid,d in items.items():
    if not d.get('asOf'): continue
    u=datetime.fromisoformat(d['asOf'].replace('Z','+00:00'))
    rows.append((pid,u,u.astimezone(ET),u.astimezone(PT),d))
rows.sort(key=lambda r:r[1])
print(f"样本：{len(rows)} 件（全量普查，无页面选择偏差）")
print(f"asOf 跨度：{rows[0][2]:%Y-%m-%d %H:%M} → {rows[-1][2]:%Y-%m-%d %H:%M} ET")
print(f"最老的距今 {(now-rows[0][2]).days} 天 {((now-rows[0][2]).seconds)//3600} 小时\n")

print("="*72)
print("【1】按日期分布（ET）")
byday=Counter(r[2].strftime('%Y-%m-%d %a') for r in rows)
mx=max(byday.values())
for k in sorted(byday):
    n=byday[k]; print(f"  {k}  {'█'*int(n/mx*50):<50} {n:>5}")

print("\n"+"="*72)
print("【2】按小时分布 — 只取最近 7 天，ET vs PT 对照")
cut=now-timedelta(days=7)
recent=[r for r in rows if r[2]>=cut]
het=Counter(r[2].hour for r in recent); hpt=Counter(r[3].hour for r in recent)
mx=max(max(het.values()), max(hpt.values()))
print(f"  （{len(recent)} 件）")
print(f"  {'时':>3} {'ET 你的时间':<42} {'PT 总部时间'}")
for h in range(24):
    a,b=het.get(h,0),hpt.get(h,0)
    print(f"  {h:>2}h {'█'*int(a/mx*34):<36}{a:>5}   {'█'*int(b/mx*20):<22}{b:>5}")

print("\n"+"="*72)
print("【3】同一分钟内被重算的商品数 — 批处理会在这里暴露")
permin=Counter(r[1].replace(second=0,microsecond=0) for r in rows)
top=permin.most_common(15)
print(f"  不同分钟数 {len(permin)}；每分钟件数 中位 {sorted(permin.values())[len(permin)//2]}，最大 {top[0][1]}")
print(f"  {'件数':>5}  ET                    PT")
for k,n in top:
    print(f"  {n:>5}  {k.astimezone(ET):%m-%d %H:%M}        {k.astimezone(PT):%m-%d %H:%M}")

print("\n"+"="*72)
print("【4】asOf 新鲜度 vs NEW_MARKDOWN 标 — 检验 asOf 是否=降价")
fresh=[r for r in rows if (now-r[2]).total_seconds()<3600*6]
old=[r for r in rows if (now-r[2]).total_seconds()>=3600*24]
for name,g in (("asOf 在 6 小时内",fresh),("asOf 超过 24 小时",old)):
    if not g: continue
    nm=sum(1 for r in g if 'NEW_MARKDOWN' in (r[4].get('tags') or []))
    print(f"  {name:<18} {len(g):>5} 件，其中带 NEW_MARKDOWN 的 {nm:>5} 件 ({nm/len(g):.0%})")

print("\n"+"="*72)
print("【5】顺带：'折扣%' 的水分 — 对标价 vs 清仓基准价")
both=[r for r in rows if r[4].get('pct') is not None and r[4].get('pctbase') is not None]
print(f"  {len(both)} 件同时有两个折扣字段")
pb=Counter()
for r in both:
    pb[(r[4]['pctbase']==0)] += 1
print(f"    相对清仓基准价降幅为 0% 的: {pb[True]} 件  ({pb[True]/len(both):.0%})")
print(f"    确实在基准价上又砍了一刀的: {pb[False]} 件  ({pb[False]/len(both):.0%})")
hi=[r for r in both if (r[4]['pct'] or 0)>=90]
if hi:
    z=sum(1 for r in hi if r[4]['pctbase']==0)
    print(f"  显示'90%+ off'的 {len(hi)} 件里，真实降幅为 0 的有 {z} 件 ({z/len(hi):.0%})")
