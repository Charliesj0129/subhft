
import shioaji as sj

print(dir(sj.constant))
try:
    print(f"StockPriceType: {dir(sj.constant.StockPriceType)}")
except Exception:
    print("StockPriceType not found")

try:
    print(f"OrderType: {dir(sj.constant.OrderType)}")
except Exception:
    print("OrderType not found")
