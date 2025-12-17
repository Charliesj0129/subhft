
from enum import Enum
from typing import Any, Dict

def serialize(obj: Any) -> Any:
    """
    Recursively serialize objects to JSON-compatible types.
    Handles:
    - Dicts
    - Lists/Tuples
    - Enums
    - Objects with .to_dict()
    - Objects with __slots__ (dataclasses with slots=True)
    - Objects with __dict__
    """
    if hasattr(obj, "to_dict"): 
        return obj.to_dict()
    
    if isinstance(obj, dict): 
        return {k: serialize(v) for k, v in obj.items()}
    
    if isinstance(obj, (list, tuple)): 
        return [serialize(x) for x in obj]
    
    if isinstance(obj, Enum): 
        return obj.value
    
    # Prioritize slots if present (often used for optimization/immutability)
    if hasattr(obj, "__slots__"):
        data = {}
        for k in obj.__slots__:
            if hasattr(obj, k):
                val = getattr(obj, k)
                data[k] = serialize(val)
        return data
        
    if hasattr(obj, "__dict__"): 
        return {k: serialize(v) for k, v in obj.__dict__.items()}
        
    return obj
