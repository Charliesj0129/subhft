import inspect
from hftbacktest.data.utils import snapshot

print("Docstring:")
print(snapshot.create_last_snapshot.__doc__)
print("\nSignature:")
try:
    print(inspect.signature(snapshot.create_last_snapshot))
except Exception as e:
    print(f"Could not get signature: {e}")
