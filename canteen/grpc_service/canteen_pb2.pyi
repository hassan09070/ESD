from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class PlaceOrderRequest(_message.Message):
    __slots__ = ("shop", "item", "idempotency_key")
    SHOP_FIELD_NUMBER: _ClassVar[int]
    ITEM_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    shop: str
    item: str
    idempotency_key: str
    def __init__(self, shop: _Optional[str] = ..., item: _Optional[str] = ..., idempotency_key: _Optional[str] = ...) -> None: ...

class Order(_message.Message):
    __slots__ = ("order_id", "shop", "item", "state", "placed_at", "replayed")
    ORDER_ID_FIELD_NUMBER: _ClassVar[int]
    SHOP_FIELD_NUMBER: _ClassVar[int]
    ITEM_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    PLACED_AT_FIELD_NUMBER: _ClassVar[int]
    REPLAYED_FIELD_NUMBER: _ClassVar[int]
    order_id: str
    shop: str
    item: str
    state: str
    placed_at: float
    replayed: bool
    def __init__(self, order_id: _Optional[str] = ..., shop: _Optional[str] = ..., item: _Optional[str] = ..., state: _Optional[str] = ..., placed_at: _Optional[float] = ..., replayed: _Optional[bool] = ...) -> None: ...
