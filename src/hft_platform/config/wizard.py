
import os
import json
import yaml
from typing import Dict, Any, List

PRESET_UNIVERSES = {
    "1": ("TSE Top 10", ["2330", "2317", "2454", "2308", "2303", "2881", "2412", "2382", "2882", "2891"]),
    "2": ("Semiconductor Focus", ["2330", "2454", "2303", "3711", "3034"]),
    "3": ("Financials", ["2881", "2882", "2891", "2886", "2884"]),
    "4": ("Shipping", ["2603", "2609", "2615"]),
}

def clear_screen():
    print("\033[H\033[J", end="")

def print_header():
    print("\n" + "="*60)
    print(" 🚀 HFT Platform Configuration Wizard v2.0 ")
    print("="*60)
    print("Configure your Trading Universe, Risk Limits, and Strategies.")
    print("-" * 60)

def get_input(prompt: str, default: str = None, options: List[str] = None) -> str:
    prompt_str = f"{prompt}"
    if default:
        prompt_str += f" (default: {default})"
    if options:
        prompt_str += f" [{'/'.join(options)}]"
    prompt_str += ": "
    
    while True:
        val = input(prompt_str).strip()
        if not val:
            if default is not None:
                return default
            continue
            
        if options and val not in options:
            print(f"Invalid option. Please choose from: {', '.join(options)}")
            continue
            
        return val

def configure_universe() -> List[Dict[str, Any]]:
    print("\n[Trading Universe Configuration]")
    print("How would you like to select symbols?")
    print("1. Manual Entry (Comma separated)")
    print("2. Use Preset Group")
    print("3. Import from File (Not implemented)")
    
    choice = get_input("Select option", "1", ["1", "2", "3"])
    
    symbols = []
    
    if choice == "1":
        raw = get_input("Enter symbols (e.g. 2330,2317)", "2330")
        codes = [c.strip() for c in raw.split(",") if c.strip()]
        symbols = [{"code": c, "exchange": "TSE"} for c in codes] # Default to TSE
        
    elif choice == "2":
        print("\nAvailable Presets:")
        for k, v in PRESET_UNIVERSES.items():
            print(f"{k}. {v[0]} ({', '.join(v[1][:5])}...)")
            
        p_choice = get_input("Select Preset", "1", list(PRESET_UNIVERSES.keys()))
        name, codes = PRESET_UNIVERSES[p_choice]
        print(f"Selected: {name}")
        symbols = [{"code": c, "exchange": "TSE"} for c in codes]
        
    return symbols

def configure_risk() -> Dict[str, Any]:
    print("\n[Risk Management Configuration]")
    print("Set limits to protect your capital.")
    
    max_pos = int(get_input("Max Position Size (Lots/Contracts per symbol)", "10"))
    max_loss = int(get_input("Daily Stop Loss Limit ($)", "10000"))
    kill_switch = get_input("Enable Auto-Kill Switch on Disconnect?", "Y", ["Y", "N", "y", "n"]).upper() == "Y"
    
    return {
        "max_position_lots": max_pos,
        "daily_loss_limit": max_loss,
        "kill_switch_enabled": kill_switch
    }

def configure_strategy() -> Dict[str, Any]:
    print("\n[Strategy Configuration]")
    print("1. Use Existing Strategy")
    print("2. Create New Strategy Template")
    
    choice = get_input("Select option", "1", ["1", "2"])
    
    strat_config = {}
    
    if choice == "1":
        strat_id = get_input("Enter Strategy Name (e.g. advanced_mm)", "advanced_mm")
        strat_config = {
            "id": strat_id,
            "module": f"hft_platform.strategies.{strat_id}" if "hft_platform" not in strat_id else strat_id,
            "enabled": True
        }
    else:
        name = get_input("Enter New Strategy Name", "my_new_alert")
        fname = name.lower().replace(" ", "_")
        cname = "".join(x.title() for x in fname.split("_"))
        
        # Generator
        path = f"src/hft_platform/strategies/{fname}.py"
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(f'''
from hft_platform.strategy.base import BaseStrategy, StrategyContext

class {cname}(BaseStrategy):
    def __init__(self, strategy_id="{fname}"):
        super().__init__(strategy_id)
        
    def on_book(self, ctx: StrategyContext, event: dict):
        """
        Callback for LOB updates.
        ctx.lob: Current Order Book snapshot
        ctx.features: Calculated Alpha features
        """
        # Implement your logic here
        pass
''')
            print(f"Created template at {path}")
            
        strat_config = {
            "id": fname,
            "module": f"hft_platform.strategies.{fname}",
            "class_name": cname,
            "enabled": True
        }
        
    return strat_config

def run_wizard():
    # clear_screen()
    print_header()
    
    # Configuration State
    config_state = {}
    
    # 1. Mode
    config_state["mode"] = get_input("Execution Mode", "sim", ["sim", "live", "backtest"])
    
    # 2. Universe
    config_state["symbols"] = configure_universe()
    
    # 3. Risk
    config_state["risk"] = configure_risk()
    
    # 4. Strategy
    config_state["strategy"] = configure_strategy()
    
    # Summary
    print("\n" + "-"*60)
    print(" Configuration Summary ")
    print("-" * 60)
    print(f"Mode: {config_state['mode']}")
    print(f"Universe: {len(config_state['symbols'])} symbols ({', '.join(s['code'] for s in config_state['symbols'][:5])}...)")
    print(f"Risk: Max {config_state['risk']['max_position_lots']} lots, Loss Limit ${config_state['risk']['daily_loss_limit']}")
    print(f"Strategy: {config_state['strategy']['id']}")
    print("-" * 60)
    
    if get_input("Save configurations?", "Y", ["Y", "N"]).upper() == "Y":
        os.makedirs("config", exist_ok=True)
        
        # 1. symbols.yaml
        with open("config/symbols.yaml", "w") as f:
            yaml.dump({"symbols": config_state["symbols"]}, f)
        print("✅ Saved config/symbols.yaml")
        
        # 2. strategies.yaml (New Standard)
        with open("config/strategies.yaml", "w") as f:
            # We wrap in a list for registry
            yaml.dump({"strategies": [config_state["strategy"]]}, f)
        print("✅ Saved config/strategies.yaml")
        
        # 3. risk.yaml
        with open("config/risk.yaml", "w") as f:
            yaml.dump(config_state["risk"], f)
        print("✅ Saved config/risk.yaml")
        
        # 4. system.json (Env override replacements)
        sys_conf = {"mode": config_state["mode"]}
        with open("config/system.json", "w") as f:
            json.dump(sys_conf, f, indent=2)
        print("✅ Saved config/system.json")
        
        print("\nConfiguration Complete! 🚀")
        print("Run 'hft run' to start.")

if __name__ == "__main__":
    run_wizard()
