"""Run R47 on ONE day of TMFD6. Outputs one JSON line to stdout."""
from __future__ import annotations
import json, math, os, sys, time
from pathlib import Path
import numpy as np

os.environ.setdefault("HFT_STRICT_PRICE_MODE", "0")
_R = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_R / "src"))
import structlog; structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))
import logging; logging.disable(logging.WARNING)

from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest
from hftbacktest.order import GTC, LIMIT

PS = 10_000; EL = 100_000_000; TK = 1.0; SYM = "TMFD6"

def main():
    fpath, spr, mp, qm, pq = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]), int(sys.argv[5])
    date = Path(fpath).stem.replace("TMFD6_","").replace("_l2.hftbt","")

    from hft_platform.contracts.strategy import IntentType, Side, OrderIntent
    from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent
    from hft_platform.strategy.base import StrategyContext
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    st = R47MakerStrategy(strategy_id="r47", pe_danger_threshold=0.0, pe_window=100,
        queue_cancel_threshold=1.0, mfg_skew_z_threshold=100.0,
        spread_threshold_pts=spr, toxicity_max=9999, max_pos=mp)
    asset = (BacktestAsset().data([fpath]).linear_asset(1.0)
        .constant_order_latency(36_000_000, 36_000_000).power_prob_queue_model(qm)
        .tick_size(TK).lot_size(1.0).partial_fill_exchange())
    hbt = HashMapMarketDepthBacktest([asset])

    pos = {SYM: 0}; iseq = [0]; cap = []
    def ifact(**kw): iseq[0]+=1; i=OrderIntent(intent_id=iseq[0],**kw); cap.append(i); return i
    def spx(s,p):
        from decimal import Decimal
        return p if isinstance(p,int) else int(Decimal(str(p))*Decimal(PS))
    ctx = StrategyContext(positions=pos, strategy_id=st.strategy_id, intent_factory=ifact, price_scaler=spx)

    oid=0; abi=None; asi=None; abp=0.0; asp=0.0
    fills=0; prev=0; q=0; c=0; pxc=0; eq=[]; ss=0.0; sc=0; sg=0

    while hbt.elapse(EL)==0:
        dp=hbt.depth(0); bb=dp.best_bid; ba=dp.best_ask
        if bb!=bb or ba!=ba or bb<=0 or ba>=2147483647 or bb>=ba: continue
        ts=int(hbt.current_timestamp); sp=ba-bb; ss+=sp; sc+=1
        if sp>=spr: sg+=1
        cur=int(hbt.position(0))
        if cur!=prev: fills+=abs(cur-prev)
        prev=cur; sv=hbt.state_values(0); eq.append(sv.balance+cur*(bb+ba)/2); pos[SYM]=cur
        bq=int(getattr(dp,'best_bid_qty',0)or 0); aq=int(getattr(dp,'best_ask_qty',0)or 0)
        bs=int(round(bb*PS)); aks=int(round(ba*PS))
        tq=bq+aq; imb=(bq-aq)/tq if tq>0 else 0
        lob=LOBStatsEvent(symbol=SYM,ts=ts,imbalance=imb,best_bid=bs,best_ask=aks,bid_depth=bq,ask_depth=aq,mid_price_x2=bs+aks,spread_scaled=aks-bs)
        v=[0]*27; v[0]=bs; v[1]=aks; v[8]=bq; v[9]=aq
        v[10]=int((bq-aq)*1_000_000/tq) if tq>0 else 0
        fids=tuple(f'f{i}' for i in range(27))
        feat=FeatureUpdateEvent(symbol=SYM,ts=ts,local_ts=ts,seq=0,feature_set_id='lob_shared_v3',
            schema_version=3,changed_mask=0xFFFFFFFF,warmup_ready_mask=0xFFFFFFFF,quality_flags=0,
            feature_ids=fids,values=tuple(v))
        # Reset pending counters — backtest harness manages orders externally,
        # so strategy's internal pending tracking would block after first quote.
        st._pending_buy.clear(); st._pending_sell.clear()
        st._last_bid.clear(); st._last_ask.clear()
        cap.clear(); st.handle_event(ctx,feat); st.handle_event(ctx,lob)
        dbp=None; dsp=None
        for i in cap:
            if i.intent_type!=IntentType.NEW: continue
            p=round(i.price/PS)
            if i.side==Side.BUY and dbp is None: dbp=p
            elif i.side==Side.SELL and dsp is None: dsp=p
        if pq:
            if dbp is not None:
                if abi is not None and abp!=dbp: hbt.cancel(0,abi,False); abi=None; c+=1; pxc+=1
                if abi is None: oid+=1; hbt.submit_buy_order(0,oid,dbp,1.0,GTC,LIMIT,False); abi=oid; abp=dbp; q+=1
            else:
                if abi is not None: hbt.cancel(0,abi,False); abi=None; c+=1
            if dsp is not None:
                if asi is not None and asp!=dsp: hbt.cancel(0,asi,False); asi=None; c+=1; pxc+=1
                if asi is None: oid+=1; hbt.submit_sell_order(0,oid,dsp,1.0,GTC,LIMIT,False); asi=oid; asp=dsp; q+=1
            else:
                if asi is not None: hbt.cancel(0,asi,False); asi=None; c+=1
        else:
            if abi is not None: hbt.cancel(0,abi,False); abi=None; c+=1
            if asi is not None: hbt.cancel(0,asi,False); asi=None; c+=1
            if dbp is not None: oid+=1; hbt.submit_buy_order(0,oid,dbp,1.0,GTC,LIMIT,False); abi=oid; abp=dbp; q+=1
            if dsp is not None: oid+=1; hbt.submit_sell_order(0,oid,dsp,1.0,GTC,LIMIT,False); asi=oid; asp=dsp; q+=1
        hbt.clear_inactive_orders(0)
        np2=int(hbt.position(0))
        if np2!=cur:
            if np2>cur: abi=None; abp=0
            else: asi=None; asp=0

    hbt.close()
    ea=np.array(eq) if eq else np.array([0.0]); pa=ea-ea[0]; pnl=float(pa[-1])
    rm=np.maximum.accumulate(pa); dd=float(np.max(rm-pa))
    avs=ss/sc if sc>0 else 0; pct=sg/sc*100 if sc>0 else 0
    pf=pnl/fills if fills>0 else 0
    r={"date":date,"pnl":round(pnl,2),"fills":fills,"quotes":q,"cancels":c,"px_chg":pxc,
       "max_dd":round(dd,2),"avg_spr":round(avs,2),"pct_spr":round(pct,2),"pnl_per_fill":round(pf,3)}
    # Print both human readable (stderr) and JSON (stdout)
    print(f'  {date}: PnL={pnl:>+9.1f} fills={fills:>5} PnL/f={pf:>+7.2f} q={q:>6} spr={avs:.1f} spr>={spr}:{pct:>5.1f}%', file=sys.stderr)
    print(json.dumps(r))

if __name__=="__main__":
    main()
