#!/usr/bin/env python3
# Ultra-light interactive loader for walNUT-style drivers
# Place next to: plugin.yaml, driver.py, optional .venv/, config.yaml/json, secrets.yaml/json
# Run: python3 loader.py

import json, os, sys, time, importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------- minimal YAML/JSON loader ----------
def _load_yaml_or_json(path: Path) -> dict:
    if not path.exists(): return {}
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text())
    try:
        import yaml  # optional
        return yaml.safe_load(path.read_text()) or {}
    except Exception:
        # fallback: allow JSON-in-.yaml for zero-deps runs
        return json.loads(path.read_text())

def _save_yaml_or_json(path: Path, data: dict):
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(data, indent=2))
        return
    try:
        import yaml
        path.write_text(yaml.safe_dump(data, sort_keys=False))
    except Exception:
        path.write_text(json.dumps(data, indent=2))

# ---------- simple secret prompt ----------
def prompt_secret(label: str) -> str:
    try:
        import getpass  # stdlib, masks input
        return getpass.getpass(f"{label}: ")
    except Exception:
        # fallback if getpass console not available
        return input(f"{label}: ")

# ---------- tiny shims ----------
@dataclass
class IntegrationInstance:
    id: str = "inst-TEST"
    name: str = "Test Instance"
    type_id: str = "test.type"
    config: Dict[str, Any] = field(default_factory=dict)

@dataclass
class Target:
    type: str
    external_id: str
    name: str = ""
    attrs: Dict[str, Any] = field(default_factory=dict)
    labels: Dict[str, Any] = field(default_factory=dict)

# ---------- per-plugin .venv path scoping ----------
class PluginVenvPath:
    def __init__(self, plugin_dir: Path): self.plugin_dir, self.added = plugin_dir, []
    def __enter__(self):
        venv = self.plugin_dir / ".venv"
        pyver = f"python{sys.version_info.major}.{sys.version_info.minor}"
        candidates = [
            venv / "lib" / pyver / "site-packages",  # POSIX
            venv / "Lib" / "site-packages",          # Windows
        ]
        for p in candidates:
            if p.exists():
                sys.path.insert(0, str(p)); self.added.append(str(p))
        sys.path.insert(0, str(self.plugin_dir)); self.added.append(str(self.plugin_dir))
        return self
    def __exit__(self, *exc):
        for p in reversed(self.added):
            try: sys.path.remove(p)
            except ValueError: pass

# ---------- driver load ----------
def load_manifest(here: Path) -> dict:
    mf = here / "plugin.yaml"
    if not mf.exists(): raise RuntimeError("plugin.yaml not found")
    m = _load_yaml_or_json(mf)
    if not isinstance(m, dict): raise RuntimeError("plugin.yaml invalid")
    return m

def load_driver(here: Path, config: dict, secrets: dict):
    m = load_manifest(here)
    entry = (m.get("driver") or {}).get("entrypoint") or "driver:Driver"
    mod_name, cls_name = entry.split(":", 1)
    drv_path = here / f"{mod_name}.py"
    if not drv_path.exists(): raise RuntimeError(f"Driver file missing: {drv_path.name}")
    with PluginVenvPath(here):
        spec = importlib.util.spec_from_file_location("plugin_driver", drv_path)
        if not spec or not spec.loader: raise RuntimeError("Failed to create import spec")
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        cls = getattr(mod, cls_name, None)
        if cls is None: raise RuntimeError(f"Class '{cls_name}' not found in {drv_path.name}")
        inst = IntegrationInstance(name=config.get("name","Test Instance"),
                                   type_id=m.get("id","unknown.type"),
                                   config=config)
        try:
            driver = cls(instance=inst, secrets=secrets)
        except TypeError:
            driver = cls(inst, secrets)
    return driver, m

# ---------- capability helpers ----------
def list_caps(mf: dict) -> List[dict]:
    caps = mf.get("capabilities") or []
    return [{
        "id": c.get("id"),
        "verbs": c.get("verbs", []),
        "targets": c.get("targets", []),
        "dry_run": c.get("dry_run", "optional"),
    } for c in caps]

def cap_method(cap_id: str) -> str:
    return cap_id.replace(".", "_")

# ---------- interactive UI ----------
def press_enter(): input("\n↩︎  Enter to continue...")

def choose_idx(prompt: str, items: List[str], allow_back=True) -> Optional[int]:
    while True:
        print(f"\n{prompt}")
        for i, it in enumerate(items, 1): print(f"  {i}) {it}")
        if allow_back: print("  0) ← Back")
        sel = input("> ").strip()
        if allow_back and sel == "0": return None
        if sel.isdigit() and 1 <= int(sel) <= len(items): return int(sel)-1
        print("Invalid selection. Try again.")

def interactive_loop(here: Path):
    # load or prompt config/secrets
    cfg = {}
    for n in ("config.yaml","config.json"):
        p = here / n
        if p.exists(): cfg = _load_yaml_or_json(p); break
    sec = {}
    for n in ("secrets.yaml","secrets.json"):
        p = here / n
        if p.exists(): sec = _load_yaml_or_json(p); break

    # check schema for required/optional fields with prompts
    manifest = load_manifest(here)
    schema = manifest.get("schema", {}).get("connection", {})
    properties = schema.get("properties", {})
    required_fields = schema.get("required", [])
    defaults = manifest.get("defaults", {})
    
    # prompt for required fields
    for field in required_fields:
        if field == "password":
            if not sec.get("password"):
                sec["password"] = prompt_secret("Password")
        else:
            if not cfg.get(field):
                cfg[field] = input(f"Enter {field}: ").strip()
    
    # prompt for boolean toggles with y/n
    for field, prop in properties.items():
        if prop.get("type") == "boolean" and field not in cfg:
            title = prop.get("title", field)
            default = prop.get("default", defaults.get(field, None))
            default_str = " [Y/n]" if default is True else " [y/N]" if default is False else " [y/n]"
            response = input(f"{title}{default_str}: ").strip().lower()
            
            if response in ("y", "yes", "1", "true"):
                cfg[field] = True
            elif response in ("n", "no", "0", "false"):
                cfg[field] = False
            elif response == "" and default is not None:
                cfg[field] = default

    # lazy driver import so we can edit config first if needed
    try:
        driver, manifest = load_driver(here, cfg, sec)
    except Exception as e:
        print(f"\n[load] {e}")
        return

    def do_probe():
        print("\n▶ Probe / test_connection")
        t0 = time.time()
        try:
            res = driver.test_connection() if hasattr(driver,"test_connection") else {}
            ms = int((time.time()-t0)*1000)
            status = res.get("status","unknown")
            latency = res.get("latency_ms", ms)
            print(f"status={status} latency_ms={latency} msg={res.get('message','')}")
        except Exception as e:
            print(f"Probe error: {e}")

    def do_caps():
        caps = list_caps(manifest)
        print(f"\nCapabilities ({len(caps)}):")
        for c in caps:
            print(f"  • {c['id']}  verbs={c['verbs']} targets={c['targets']} dry_run={c['dry_run']}")

    def do_inventory():
        caps = list_caps(manifest)
        inv_targets = set()
        for c in caps:
            if c["id"] == "inventory.list":
                for t in c.get("targets", []): inv_targets.add(t)
        if not inv_targets:
            print("\nNo inventory targets exposed.")
            return
        choices = sorted(inv_targets)
        idx = choose_idx("Select inventory type", choices)
        if idx is None: return
        inv_type = choices[idx]
        active = input("Active-only? [Y/n]: ").strip().lower() not in ("n","no","0")
        if not hasattr(driver,"inventory_list"):
            print("Driver lacks inventory_list()"); return
        try:
            items = driver.inventory_list(inv_type, active_only=active, options=None) or []
            print(f"\n{inv_type} count={len(items)}")
            for it in items:
                t = it.get("type", inv_type)
                eid = it.get("external_id") or it.get("id")
                name = it.get("name","")
                attrs = it.get("attrs",{})
                print(f"  - {t} id={eid} name={name} attrs={attrs}")
        except Exception as e:
            print(f"Inventory error: {e}")

    def do_action():
        caps = [c for c in list_caps(manifest) if c["id"] != "inventory.list"]
        if not caps:
            print("\nNo actionable capabilities."); return
        labels = [f"{c['id']}  (verbs={c['verbs']}, targets={c['targets']})" for c in caps]
        idx = choose_idx("Select capability", labels)
        if idx is None: return
        cap = caps[idx]
        verb_choices = cap.get("verbs", [])
        if not verb_choices:
            print("This capability defines no verbs."); return
        vi = choose_idx("Select verb", verb_choices)
        if vi is None: return
        verb = verb_choices[vi]

        # Target (optional)
        tgt_type = None; tgt_id = None
        possible_targets = cap.get("targets", [])
        if possible_targets:
            # Pick target type, then id
            ti = choose_idx("Select target type", possible_targets)
            if ti is None: return
            tgt_type = possible_targets[ti]
            tgt_id = input(f"Enter target external_id for {tgt_type} (or blank to skip): ").strip() or None

        dry = input("Dry-run? [Y/n]: ").strip().lower() not in ("n","no","0")
        opts_txt = input("Options JSON (or blank): ").strip()
        try:
            options = json.loads(opts_txt) if opts_txt else {}
        except Exception as e:
            print(f"Invalid JSON: {e}"); return

        method_name = cap_method(cap["id"])
        if not hasattr(driver, method_name):
            print(f"Driver missing method {method_name}"); return
        fn = getattr(driver, method_name)

        target = Target(type=tgt_type, external_id=str(tgt_id)) if (tgt_type and tgt_id) else None
        # Try common signatures
        attempts = [
            lambda: fn(verb=verb, target=target, options=options, dry_run=dry),
            lambda: fn(verb, target, options, dry),
            lambda: fn(verb=verb, target=target, dry_run=dry),
            lambda: fn(verb, target, dry),
        ]
        err = None
        for call in attempts:
            try:
                res = call() or {"success": True}
                print(json.dumps(res, indent=2, default=str)); return
            except TypeError as te:
                err = te; continue
            except Exception as e:
                print(f"Action error: {e}"); return
        print(f"Could not call with standard signatures: {err}")

    def do_config():
        print("\nCurrent config:")
        print(json.dumps(cfg, indent=2))
        print("\n1) Edit hostname  2) Edit username  3) Edit password  4) Save config.yaml  0) Back")
        ch = input("> ").strip()
        if ch == "1": cfg["hostname"] = input("hostname: ").strip()
        elif ch == "2": cfg["username"] = input("username: ").strip()
        elif ch == "3": sec["password"] = prompt_secret("password")
        elif ch == "4":
            _save_yaml_or_json(here / "config.yaml", cfg)
            _save_yaml_or_json(here / "secrets.yaml", {"password": sec.get("password")})
            print("Saved config.yaml and secrets.yaml")

    while True:
        print("\n=== walNUT Driver Loader ===")
        print("1) Probe/test_connection")
        print("2) List capabilities")
        print("3) Inventory…")
        print("4) Action…")
        print("5) Config & secrets…")
        print("0) Exit")
        sel = input("> ").strip()
        if sel == "1": do_probe(); press_enter()
        elif sel == "2": do_caps(); press_enter()
        elif sel == "3": do_inventory(); press_enter()
        elif sel == "4": do_action(); press_enter()
        elif sel == "5": do_config(); press_enter()
        elif sel == "0": break
        else: print("Invalid selection.")

def main():
    here = Path(__file__).resolve().parent
    try:
        interactive_loop(here)
    except KeyboardInterrupt:
        print("\nBye.")

if __name__ == "__main__":
    main()