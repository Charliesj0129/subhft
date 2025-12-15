---
title: "Stocks"
source: "https://sinotrade.github.io/tutor/order_deal_event/stocks"
---

# Stocks

Order & Deal Event is a report of order action. When you place order, cancel order and update order, it will return an OrderState. OrderState is order info. If you don't want to receive any report, you can set `subscribe_trade` to False when you login.

In

```
api.login?
```

Out

```
Signature:
    api.login(
        person_id: str,
        passwd: str,
        hashed: bool = False,
        fetch_contract: bool = True,
        contracts_timeout: int = 0,
        contracts_cb: Callable[[], NoneType] = None,
        subscribe_trade: bool = True,
    ) -> None
Docstring:
    login to trading server
```

### Place Order[¶](#place-order "Permanent link")

When you place an order, you will receive an order event, and `op_type` will display the behavior of this event, just like the new order below will display New. When the transaction is completed, you will receive an deal event.

place\_order

```
contract = api.Contracts.Stocks.TSE.TSE2890
order = api.Order(price=12,
                  quantity=10,
                  action=sj.constant.Action.Buy,
                  price_type=sj.constant.StockPriceType.LMT,
                  order_type=sj.constant.TFTOrderType.ROD,
                  custom_field="test",
                  account=api.stock_account
                  )
trade = api.place_order(contract, order)
```

Order Event

```
OrderState.TFTOrder {
    'operation': {
        'op_type': 'New',
        'op_code': '00',
        'op_msg': ''
    },
    'order': {
        'id': 'c21b876d',
        'seqno': '429832',
        'ordno': 'W2892',
        'action': 'Buy',
        'price': 12.0,
        'quantity': 10,
        'order_cond': 'Cash',
        'order_lot': 'Common',
        'custom_field': 'test',
        'order_type': 'ROD',
        'price_type': 'LMT'
    },
    'status': {
        'id': 'c21b876d',
        'exchange_ts': 1583828972,
        'modified_price': 0,
        'cancel_quantity': 0,
        'web_id': '137'
    },
    'contract': {
        'security_type': 'STK',
        'exchange': 'TSE',
        'code': '2890',
        'symbol': '',
        'name': '',
        'currency': 'TWD'
    }
}
```

Deal Event

```
OrderState.TFTDeal {
    'trade_id': '12ab3456', 
    'exchange_seq': '123456', 
    'broker_id': 'your_broker_id', 
    'account_id': 'your_account_id', 
    'action': <Action.Buy: 'Buy'>, 
    'code': '2890', 
    'order_cond': <StockOrderCond.Cash: 'Cash'>, 
    'order_lot': <TFTStockOrderLot.Common: 'Common'>,
    'price': 12, 
    'quantity': 10,
    'web_id': '137',
    'custom_field': 'test',
    'ts': 1583828972
}
```

### Cancel Order[¶](#cancel-order "Permanent link")

`op_type` shows Cancel.

In

```
api.cancel_order(trade)
```

Out

```
OrderState.TFTOrder {
    'operation': {
        'op_type': 'Cancel',
        'op_code': '00',
        'op_msg': ''
    },
    'order': {
        'id': 'c21b876d',
        'seqno': '429832',
        'ordno': 'W2892',
        'action': 'Buy',
        'price': 12.0,
        'quantity': 10,
        'order_cond': 'Cash',
        'order_lot': 'Common',
        'custom_field': 'test',
        'order_type': 'ROD',
        'price_type': 'LMT'
    },
    'status': {
        'id': 'c21b876d',
        'exchange_ts': 1583829131,
        'modified_price': 0,
        'cancel_quantity': 10,
        'web_id': '137'
    },
    'contract': {
        'security_type': 'STK',
        'exchange': 'TSE',
        'code': '2890',
        'symbol': '',
        'name': '',
        'currency': 'TWD'
    }
}
```

### Update Price[¶](#update-price "Permanent link")

`op_type` shows UpdatePrice.

In

```
api.update_order(trade=trade, price=12.5, quantity=10)
```

Out

```
OrderState.TFTOrder {
    'operation': {
        'op_type': 'UpdatePrice',
        'op_code': '00',
        'op_msg': ''
    },
    'order': {
        'id': 'a5cff9b6',
        'seqno': '429833',
        'ordno': 'W2893',
        'action': 'Buy',
        'price': 12.5,
        'quantity': 10,
        'order_cond': 'Cash',
        'order_lot': 'Common',
        'custom_field': 'test',
        'order_type': 'ROD',
        'price_type': 'LMT'
    },
    'status': {
        'id': 'a5cff9b6',
        'exchange_ts': 1583829166,
        'modified_price': 12.5,
        'cancel_quantity': 0
        'web_id': '137'
    },
    'contract': {
        'security_type': 'STK',
        'exchange': 'TSE',
        'code': '2890',
        'symbol': '',
        'name': '',
        'currency': 'TWD'
    }
}
```

### Update Quantity[¶](#update-quantity "Permanent link")

`op_type` shows UpdateQty.

In

```
api.update_order(trade=trade, price=12, quantity=2)
```

Out

```
OrderState.TFTOrder {
    'operation': {
        'op_type': 'UpdateQty',
        'op_code': '00',
        'op_msg': ''
    },
    'order': {
        'id': 'a5cff9b6',
        'seqno': '429833',
        'ordno': 'W2893',
        'action': 'Buy',
        'price': 12.0,
        'quantity': 10,
        'order_cond': 'Cash',
        'order_lot': 'Common',
        'custom_field': 'test',
        'order_type': 'ROD',
        'price_type': 'LMT'
    },
    'status': {
        'id': 'a5cff9b6',
        'exchange_ts': 1583829187,
        'modified_price': 0,
        'cancel_quantity': 2
        'web_id': '137'
    },
    'contract': {
        'security_type': 'STK',
        'exchange': 'TSE',
        'code': '2890',
        'symbol': '',
        'name': '',
        'currency': 'TWD'
    }
}
```

### Set order callback[¶](#set-order-callback "Permanent link")

You can use `set_order_callback` to use the return information. The example prints my\_place\_callback before receiving the event.

In

```
def place_cb(stat, msg):
    print('my_place_callback')
    print(stat, msg)

api.set_order_callback(place_cb)
contract = api.Contracts.Stocks.TSE.TSE2890
order = api.Order(price=12,
                  quantity=10,
                  action=sj.constant.Action.Buy,
                  price_type=sj.constant.StockPriceType.LMT,
                  order_type=sj.constant.TFTOrderType.ROD,
                  custom_field="test",
                  account=api.stock_account
                  )
trade = api.place_order(contract, order)
```

Order Event

```
my_place_callback
OrderState.TFTOrder {
    'operation': {
        'op_type': 'New',
        'op_code': '00',
        'op_msg': ''
    },
    'order': {
        'id': 'c21b876d',
        'seqno': '429832',
        'ordno': 'W2892',
        'action': 'Buy',
        'price': 12.0,
        'quantity': 10,
        'order_cond': 'Cash',
        'order_lot': 'Common',
        'custom_field': 'test',
        'order_type': 'ROD',
        'price_type': 'LMT'
    },
    'status': {
        'id': 'c21b876d',
        'exchange_ts': 1583828972,
        'modified_price': 0,
        'cancel_quantity': 0,
        'web_id': '137'
    },
    'contract': {
        'security_type': 'STK',
        'exchange': 'TSE',
        'code': '2890',
        'symbol': '',
        'name': '',
        'currency': 'TWD'
    }
}
```

Deal Event

```
my_place_callback
OrderState.TFTDeal {
    'trade_id': '12ab3456', 
    'exchange_seq': '123456', 
    'broker_id': 'your_broker_id', 
    'account_id': 'your_account_id', 
    'action': <Action.Buy: 'Buy'>, 
    'code': '2890', 
    'order_cond': <StockOrderCond.Cash: 'Cash'>, 
    'order_lot': <TFTStockOrderLot.Common: 'Common'>,
    'price': 12, 
    'quantity': 10, 
    'web_id': '137',
    'custom_field': 'test',
    'ts': 1583828972
}
```
