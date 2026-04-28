"""
Migration script: stamp model_tier into every agent config.yaml.

Reads configs/model_tiers.yaml to build the engine → tier lookup, then
walks every config.yaml under app/assistant/ and inserts a `model_tier:`
line directly after `engine:` in the llm_params block.

Safe to re-run: skips files that already have model_tier set.

Usage:
    python scripts/stamp_model_tiers.py [--dry-run]
"""
import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
TIERS_FILE = REPO_ROOT / "configs" / "model_tiers.yaml"
AGENTS_ROOT = REPO_ROOT / "app" / "assistant"


def load_engine_to_tier() -> dict:
    with open(TIERS_FILE, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("engine_to_tier", {})


def stamp_file(path: Path, engine_to_tier: dict, dry_run: bool) -> bool:
    """Return True if the file was (or would be) modified."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    # Skip if model_tier already present
    if any("model_tier:" in line for line in lines):
        return False

    new_lines = []
    modified = False
    for line in lines:
        new_lines.append(line)
        stripped = line.strip()
        if stripped.startswith("engine:"):
            # Extract engine value (handles quoted and unquoted)
            parts = stripped.split(":", 1)
            raw_val = parts[1].strip().strip('"').strip("'").split("#")[0].strip()
            tier = engine_to_tier.get(raw_val)
            if tier:
                # Match indentation of the engine: line
                indent = len(line) - len(line.lstrip())
                tier_line = " " * indent + f'model_tier: "{tier}"\n'
                new_lines.append(tier_line)
                modified = True

    if not modified:
        return False

    if not dry_run:
        path.write_text("".join(new_lines), encoding="utf-8")
    return True


def main():
    parser = argparse.ArgumentParser(description="Stamp model_tier into agent config.yaml files")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    args = parser.parse_args()

    engine_to_tier = load_engine_to_tier()
    config_files = list(AGENTS_ROOT.rglob("config.yaml"))

    stamped = []
    skipped = []
    unknown_engine = []

    for cfg_path in sorted(config_files):
        text = cfg_path.read_text(encoding="utf-8")
        if "model_tier:" in text:
            skipped.append(cfg_path)
            continue

        # Check if there's an engine line we know about
        has_known_engine = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("engine:"):
                raw_val = stripped.split(":", 1)[1].strip().split("#")[0].strip().strip('"').strip("'")
                if raw_val in engine_to_tier:
                    has_known_engine = True
                    break
                else:
                    unknown_engine.append((cfg_path, raw_val))

        if not has_known_engine:
            continue

        changed = stamp_file(cfg_path, engine_to_tier, dry_run=args.dry_run)
        if changed:
            stamped.append(cfg_path)

    action = "Would stamp" if args.dry_run else "Stamped"
    print(f"\n{action} {len(stamped)} files:")
    for p in stamped:
        print(f"  [ok]  {p.relative_to(REPO_ROOT)}")

    if skipped:
        print(f"\nSkipped {len(skipped)} (already have model_tier)")

    if unknown_engine:
        print(f"\nWarning: {len(unknown_engine)} files have engine names not in engine_to_tier:")
        for p, eng in unknown_engine:
            print(f"  [?]  {p.relative_to(REPO_ROOT)}  ->  engine: {eng}")

    if args.dry_run:
        print("\n[dry-run] No files written.")
    else:
        print(f"\nDone. Run with --dry-run first if you want to preview changes.")


if __name__ == "__main__":
    main()
