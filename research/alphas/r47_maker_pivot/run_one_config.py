"""Run R47 backtest for ONE config on all TMFD6 days. Called by parent script."""
from __future__ import annotations
import gc, json, math, os, sys, time
from pathlib import Path
import numpy as np

os.environ.setdefault("HFT_STRICT_PRICE_MODE", "0")
_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "src"))

import structlog; structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))
import logging; logging.disable(logging.WARNING)

from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest
from hftbacktest.order import GTC, LIMIT

PRICE_SCALE = 10_000; ELAPSE_NS = 100_000_000; TICK = 1.0; SYMBOL = "TMFD6"
_B0, _B1, _BQ, _AQ, _IMB = 0, 1, 8, 9, 10
DATA_DIR = _REPO / "research" / "data" / "raw" / "tmfd6"
DATA_FILES = sorted(DATA_DIR.glob("TMFD6_2026-0*_l2.hftbt.npz"))


def run_day(path: Path, spr: int, mp: int, qm: float, pq: bool, lat: int, qct: float):
    from hft_platform.contracts.strategy import IntentType, Side, OrderIntent
    from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent
    from hft_platform.strategy.base import StrategyContext
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    date = path.stem.replace("TMFD6_", "").replace("_l2.hftbt", "")
    strat = R47MakerStrategy(strategy_id="r47", pe_danger_threshold=0.0, pe_window=100,
        queue_cancel_threshold=qct, mfg_skew_z_threshold=100.0,
        spread_threshold_pts=spr, toxicity_max=9999, max_pos=mp)

    asset = (BacktestAsset().data([str(path)]).linear_asset(1.0)
        .constant_order_latency(lat, lat).power_prob_queue_model(qm)
        .tick_size(TICK).lot_size(1.0).partial_fill_exchange())
    hbt = HashMapMarketDepthBacktest([asset])

    pos = {SYMBOL: 0}; iseq = [0]; cap = []
    def ifact(**kw): iseq[0]+=1; i=OrderIntent(intent_id=iseq[0],**kw); cap.append(i); return i
    def spx(s,p):
        from decimal import Decimal
        return p if isinstance(p,int) else int(Decimal(str(p))*Decimal(PRICE_SCALE))
    ctx = StrategyContext(positions=pos, strategy_id=strat.strategy_id, intent_factory=ifact, price_scaler=spx)

    oid=0; ab=None; asid=None; abp=0.0; asp=0.0
    fills=0; prev=0; q=0; c=0; pxc=0; eq=[]; ssum=0.0; sc=0; sge=0

    while hbt.elapse(ELAPSE_NS)==0:
        dp=hbt.depth(0); bb=dp.best_bid; ba=dp.best_ask
        if bb!=bb or ba!=ba or bb<=0 or ba>=2147483647 or bb>=ba: continue
        ts=int(hbt.current_timestamp); sp=ba-bb; ssum+=sp; sc+=1
        if sp>=spr: sge+=1
        cur=int(hbt.position(0))
        if cur!=prev: fills+=abs(cur-prev)
        prev=cur
        sv=hbt.state_values(0); eq.append(sv.balance+cur*(bb+ba)/2); pos[SYMBOL]=cur
        bq=int(getattr(dp,'best_bid_qty',0) or 0); aq=int(getattr(dp,'best_ask_qty',0) or 0)
        bs=int(round(bb*PRICE_SCALE)); aks=int(round(ba*PRICE_SCALE))
        tq=bq+aq; imb=(bq-aq)/tq if tq>0 else 0
        lob=LOBStatsEvent(symbol=SYMBOL,ts=ts,imbalance=imb,best_bid=bs,best_ask=aks,bid_depth=bq,ask_depth=aq,mid_price_x2=bs+aks,spread_scaled=aks-bs)
        v=[0]*27; v[_B0]=bs; v[_B1]=aks; v[_BQ]=bq; v[_AQ]=aq
        v[_IMB]=int((bq-aq)*1_000_000/tq) if tq>0 else 0
        fids=tuple(f'f{i}' for i in range(27))
        feat=FeatureUpdateEvent(symbol=SYMBOL,ts=ts,local_ts=ts,seq=0,feature_set_id='lob_shared_v3',
            schema_version=3,changed_mask=0xFFFFFFFF,warmup_ready_mask=0xFFFFFFFF,quality_flags=0,
            feature_ids=fids,values=tuple(v))
        # Reset pending counters — backtest harness manages orders externally,
        # so strategy's internal pending tracking would block after first quote.
        strat._pending_buy.clear(); strat._pending_sell.clear()
        strat._last_bid.clear(); strat._last_ask.clear()
        cap.clear(); strat.handle_event(ctx,feat); strat.handle_event(ctx,lob)
        dbp=None; dsp=None
        for i in cap:
            if i.intent_type!=IntentType.NEW: continue
            p=round(i.price/PRICE_SCALE)
            if i.side==Side.BUY and dbp is None: dbp=p
            elif i.side==Side.SELL and dsp is None: dsp=p
        if pq:
            if dbp is not None:
                if ab is not None and abp!=dbp: hbt.cancel(0,ab,False); ab=None; c+=1; pxc+=1
                if ab is None: oid+=1; hbt.submit_buy_order(0,oid,dbp,1.0,GTC,LIMIT,False); ab=oid; abp=dbp; q+=1
            else:
                if ab is not None: hbt.cancel(0,ab,False); ab=None; c+=1
            if dsp is not None:
                if asid is not None and asp!=dsp: hbt.cancel(0,asid,False); asid=None; c+=1; pxc+=1
                if asid is None: oid+=1; hbt.submit_sell_order(0,oid,dsp,1.0,GTC,LIMIT,False); asid=oid; asp=dsp; q+=1
            else:
                if asid is not None: hbt.cancel(0,asid,False); asid=None; c+=1
        else:
            if ab is not None: hbt.cancel(0,ab,False); ab=None; c+=1
            if asid is not None: hbt.cancel(0,asid,False); asid=None; c+=1
            if dbp is not None: oid+=1; hbt.submit_buy_order(0,oid,dbp,1.0,GTC,LIMIT,False); ab=oid; abp=dbp; q+=1
            if dsp is not None: oid+=1; hbt.submit_sell_order(0,oid,dsp,1.0,GTC,LIMIT,False); asid=oid; asp=dsp; q+=1
        hbt.clear_inactive_orders(0)
        np2=int(hbt.position(0))
        if np2!=cur:
            if np2>cur: ab=None; abp=0
            else: asid=None; asp=0

    hbt.close()
    ea=np.array(eq) if eq else np.array([0.0]); pa=ea-ea[0]; pnl=float(pa[-1])
    rm=np.maximum.accumulate(pa); dd=float(np.max(rm-pa))
    avs=ssum/sc if sc>0 else 0; pct=sge/sc*100 if sc>0 else 0
    return {"date":date,"pnl":round(pnl,2),"fills":fills,"quotes":q,"cancels":c,
            "px_chg":pxc,"max_dd":round(dd,2),"avg_spr":round(avs,2),"pct_spr":round(pct,2),
            "final_pos":int(hbt.position(0)) if eq else 0}


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True)
    p.add_argument("--spr", type=int, required=True)
    p.add_argument("--mp", type=int, required=True)
    p.add_argument("--qm", type=float, required=True)
    p.add_argument("--pq", type=int, required=True)  # 1=True, 0=False
    p.add_argument("--lat", type=int, default=36_000_000)
    p.add_argument("--qct", type=float, default=1.0)
    args = p.parse_args()

    results = []
    for f in DATA_FILES:
        t0 = time.monotonic()
        r = run_day(f, args.spr, args.mp, args.qm, bool(args.pq), args.lat, args.qct)
        el = time.monotonic()-t0
        pf = r["pnl"]/r["fills"] if r["fills"]>0 else 0
        print(f'  {r["date"]}: PnL={r["pnl"]:>+9.1f}, fills={r["fills"]:>5}, PnL/f={pf:>+6.2f}, '
              f'q={r["quotes"]:>6}, c={r["cancels"]:>6}, spr={r["avg_spr"]:.1f}, '
              f'spr>={args.spr}:{r["pct_spr"]:>5.1f}%  ({el:.1f}s)')
        results.append(r)
        gc.collect()

    tp = sum(r["pnl"] for r in results)
    tf = sum(r["fills"] for r in results)
    w = sum(1 for r in results if r["pnl"]>0)
    pf = tp/tf if tf>0 else 0
    dpnls = [r["pnl"] for r in results]
    mn = np.mean(dpnls); sd = np.std(dpnls, ddof=1) if len(dpnls)>1 else 0
    t = mn/(sd/math.sqrt(len(dpnls))) if sd>0 else 0
    print(f'\n  TOTAL: PnL={tp:>+.1f} ({tp*10:>+.0f} NTD), fills={tf}, PnL/fill={pf:>+.3f}, '
          f'win={w}/{len(results)}, t={t:.3f}')

    out = {"name": args.name, "config": {"spr":args.spr,"mp":args.mp,"qm":args.qm,"pq":bool(args.pq),"lat":args.lat},
           "summary": {"total_pnl":round(tp,2),"total_fills":tf,"pnl_per_fill":round(pf,3),
                        "winning_days":w,"n_days":len(results),"t_stat":round(t,3),
                        "mean_daily":round(mn,2),"std_daily":round(sd,2)},
           "per_day": results}
    outf = _REPO/"outputs"/"team_artifacts"/"alpha-research"/"R47_maker_pivot"/f"tmfd6_realistic_{args.name}.json"
    outf.parent.mkdir(parents=True, exist_ok=True)
    outf.write_text(json.dumps(out, indent=2))
    print(f'  Saved: {outf}')


if __name__ == "__main__":
    main()
