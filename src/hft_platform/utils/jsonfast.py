"""Single JSON codec for the engine runtime, built on ``msgspec.json``.

orjson publishes no free-threaded (cp314t) wheel, so every runtime site that
used it goes through this module instead. The module imports ``msgspec``
unconditionally: a missing serializer must refuse startup, never degrade to a
silent stdlib ``json`` fallback whose bytes differ from what the readers (and
the checkpoint SHA-256) expect.

Compatibility with the orjson calls this replaces
-------------------------------------------------
* Compact output (no whitespace), UTF-8 text unescaped, ints exact, NaN / inf
  encoded as ``null``, tuples as arrays, dataclasses / Enum / IntEnum by value:
  byte-identical to ``orjson.dumps``.
* ``dumps_sorted`` == ``orjson.dumps(obj, option=OPT_SORT_KEYS)`` (nested key
  order is code-point order in both).
* Floats are shortest-round-trip in both, with ONE byte difference: for a
  positive exponent orjson writes ``1e+16``, msgspec writes ``1e16``. Every float
  below 1e16 and every negative exponent is identical, and both spellings
  decode to the same double. Nothing persisted by the engine holds a float that
  large (prices and money are scaled ints), but hash-over-bytes readers
  (checkpoint SHA-256, WAL batch content hash) would see the difference.
* Errors keep orjson's contract: encoding failures raise ``TypeError``, decoding
  failures raise ``ValueError`` (``msgspec.DecodeError`` is a ``ValueError``).
* ``str`` / ``int`` subclasses (e.g. ``numpy.str_``) are accepted as orjson
  accepts them; ``float`` subclasses (``numpy.float64``), numpy scalars and
  arrays, and arbitrary objects are rejected as orjson rejects them (no
  ``OPT_SERIALIZE_NUMPY`` was ever enabled).
* Accepted by msgspec but rejected by orjson (a superset, not a regression):
  ``int``/``float``/``bytes``/Enum dict keys (written as strings), ``bytes`` (base64),
  ``set``/``frozenset``/namedtuple (arrays), ``Decimal`` (string), integers beyond
  64 bits. Beyond 64 bits orjson *decodes* to a lossy float; msgspec decodes
  to the exact int.
* ``datetime`` with tzinfo UTC is ``...Z`` in msgspec and ``...+00:00`` in orjson;
  no persisted payload carries a datetime.

The module-level ``msgspec.json.encode`` / ``decode`` functions are used rather
than shared ``Encoder`` / ``Decoder`` instances: they hold no state between calls,
so there is nothing to reason about when several threads encode at once on a
free-threaded build. The cost over a shared encoder was measured at about 10%
on a depth-5 book payload.
"""

from __future__ import annotations

from typing import Any

import msgspec

DecodeError = msgspec.DecodeError


def _enc_hook(obj: object) -> object:
    """Accept the ``str`` / ``int`` subclasses orjson serialized natively.

    Called by msgspec only for types it cannot encode itself, so the fast path
    pays nothing. Everything else is refused with ``TypeError`` (not the
    ``NotImplementedError`` msgspec documents, which would leak out unchanged).
    """
    if isinstance(obj, str):
        return str.__str__(obj)
    if isinstance(obj, int):
        return int.__int__(obj)
    raise TypeError(f"Type is not JSON serializable: {type(obj).__name__}")


def dumps(obj: Any) -> bytes:
    """Encode *obj* to compact JSON bytes (orjson.dumps equivalent)."""
    try:
        return msgspec.json.encode(obj, enc_hook=_enc_hook)
    except (msgspec.EncodeError, UnicodeEncodeError, RecursionError) as exc:
        raise TypeError(str(exc)) from exc


def dumps_sorted(obj: Any) -> bytes:
    """Encode with every mapping's keys sorted (orjson OPT_SORT_KEYS equivalent).

    Use wherever the bytes are hashed or compared: the output is a function of
    the value alone, not of dict insertion order.
    """
    try:
        return msgspec.json.encode(obj, enc_hook=_enc_hook, order="sorted")
    except (msgspec.EncodeError, UnicodeEncodeError, RecursionError) as exc:
        raise TypeError(str(exc)) from exc


def dumps_str(obj: Any) -> str:
    """Encode *obj* to a JSON ``str`` (orjson.dumps(obj).decode() equivalent)."""
    return dumps(obj).decode("utf-8")


def loads(data: bytes | bytearray | memoryview | str) -> Any:
    """Decode JSON from bytes or str (orjson.loads equivalent).

    Raises ``msgspec.DecodeError`` (a ``ValueError``) on malformed input,
    including nesting deeper than the interpreter can recurse.
    """
    try:
        return msgspec.json.decode(data)
    except RecursionError as exc:
        raise msgspec.DecodeError("JSON nesting too deep") from exc
