# Event Callback

In this api, we use solace as mesh broker. This event mean the status for your client with solace connection situation.
If you have no experience with networking, please skip this part, In defalut, we help you reconnect solace broker 50 times without any setting. Best way is keep your network connection alive.

In

```
@api.quote.on_event
def event_callback(resp_code: int, event_code: int, info: str, event: str):
    print(f'Event code: {event_code} | Event: {event}')
```

Out

```
Event code: 16 | Event: Subscribe or Unsubscribe ok
```

Like the quote callback, your can also set event cllback with two way.

In

```
api.quote.set_event_callback?
```

Out

```
Signature: api.quote.set_event_callback(func:Callable[[int, int, str, str], NoneType]) -> None
Docstring: <no docstring>
Type:      method
```

### Event Code[¶](#event-code "Permanent link")

| Event Code | Event Code Enumerator | Description |
| --- | --- | --- |
| 0 | SOLCLIENT\_SESSION\_EVENT\_UP\_NOTICE | The Session is established. |
| 1 | SOLCLIENT\_SESSION\_EVENT\_DOWN\_ERROR | The Session was established and then went down. |
| 2 | SOLCLIENT\_SESSION\_EVENT\_CONNECT\_FAILED\_ERROR | The Session attempted to connect but was unsuccessful. |
| 3 | SOLCLIENT\_SESSION\_EVENT\_REJECTED\_MSG\_ERROR | The appliance rejected a published message. |
| 4 | SOLCLIENT\_SESSION\_EVENT\_SUBSCRIPTION\_ERROR | The appliance rejected a subscription (add or remove). |
| 5 | SOLCLIENT\_SESSION\_EVENT\_RX\_MSG\_TOO\_BIG\_ERROR | The API discarded a received message that exceeded the Session buffer size. |
| 6 | SOLCLIENT\_SESSION\_EVENT\_ACKNOWLEDGEMENT | The oldest transmitted Persistent/Non-Persistent message that has been acknowledged. |
| 7 | SOLCLIENT\_SESSION\_EVENT\_ASSURED\_PUBLISHING\_UP | Deprecated -- see notes in solClient\_session\_startAssuredPublishing.The AD Handshake (that is, Guaranteed Delivery handshake) has completed for the publisher and Guaranteed messages can be sent. |
| 8 | SOLCLIENT\_SESSION\_EVENT\_ASSURED\_CONNECT\_FAILED | Deprecated -- see notes in solClient\_session\_startAssuredPublishing.The appliance rejected the AD Handshake to start Guaranteed publishing. Use SOLCLIENT\_SESSION\_EVENT\_ASSURED\_DELIVERY\_DOWN instead. |
| 8 | SOLCLIENT\_SESSION\_EVENT\_ASSURED\_DELIVERY\_DOWN | Guaranteed Delivery publishing is not available.The guaranteed delivery capability on the session has been disabled by some action on the appliance. |
| 9 | SOLCLIENT\_SESSION\_EVENT\_TE\_UNSUBSCRIBE\_ERROR | The Topic Endpoint unsubscribe command failed. |
| 9 | SOLCLIENT\_SESSION\_EVENT\_DTE\_UNSUBSCRIBE\_ERROR | Deprecated name; SOLCLIENT\_SESSION\_EVENT\_TE\_UNSUBSCRIBE\_ERROR is preferred. |
| 10 | SOLCLIENT\_SESSION\_EVENT\_TE\_UNSUBSCRIBE\_OK | The Topic Endpoint unsubscribe completed. |
| 10 | SOLCLIENT\_SESSION\_EVENT\_DTE\_UNSUBSCRIBE\_OK | Deprecated name; SOLCLIENT\_SESSION\_EVENT\_TE\_UNSUBSCRIBE\_OK is preferred. |
| 11 | SOLCLIENT\_SESSION\_EVENT\_CAN\_SEND | The send is no longer blocked. |
| 12 | SOLCLIENT\_SESSION\_EVENT\_RECONNECTING\_NOTICE | The Session has gone down, and an automatic reconnect attempt is in progress. |
| 13 | SOLCLIENT\_SESSION\_EVENT\_RECONNECTED\_NOTICE | The automatic reconnect of the Session was successful, and the Session was established again. |
| 14 | SOLCLIENT\_SESSION\_EVENT\_PROVISION\_ERROR | The endpoint create/delete command failed. |
| 15 | SOLCLIENT\_SESSION\_EVENT\_PROVISION\_OK | The endpoint create/delete command completed. |
| 16 | SOLCLIENT\_SESSION\_EVENT\_SUBSCRIPTION\_OK | The subscribe or unsubscribe operation has succeeded. |
| 17 | SOLCLIENT\_SESSION\_EVENT\_VIRTUAL\_ROUTER\_NAME\_CHANGED | The appliance's Virtual Router Name changed during a reconnect operation.This could render existing queues or temporary topics invalid. |
| 18 | SOLCLIENT\_SESSION\_EVENT\_MODIFYPROP\_OK | The session property modification completed. |
| 19 | SOLCLIENT\_SESSION\_EVENT\_MODIFYPROP\_FAIL | The session property modification failed. |
| 20 | SOLCLIENT\_SESSION\_EVENT\_REPUBLISH\_UNACKED\_MESSAGES | After successfully reconnecting a disconnected session, the SDK received an unknown publisher flow name response when reconnecting the GD publisher flow. |
