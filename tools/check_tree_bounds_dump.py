import argparse
import json
from pathlib import Path


def load_last_json(path):
    text = Path(path).read_text(encoding="utf-8", errors="ignore").strip()
    if not text:
        raise ValueError(f"{path} is empty")
    decoder = json.JSONDecoder()
    index = 0
    last = None
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        value, end = decoder.raw_decode(text, index)
        last = value
        index = end
    if last is None:
        raise ValueError(f"No JSON object found in {path}")
    return last


def is_nonzero_vec(vec):
    if not isinstance(vec, dict):
        return False
    return any(abs(float(vec.get(axis, 0))) > 0.0001 for axis in ("x", "y", "z"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        default=str(Path(__file__).resolve().parents[1] / "map-data-741" / "data" / "741" / "mapdata.json"),
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "outputs" / "tree_bounds_dump" / "tree_bounds_summary.json"),
    )
    args = parser.parse_args()

    payload = load_last_json(args.input)
    trees = payload.get("data", {}).get("ent_dota_tree", [])
    fields = [
        "boundsMins",
        "boundsMaxs",
        "worldBoundsMins",
        "worldBoundsMaxs",
        "boundingMins",
        "boundingMaxs",
        "hullMins",
        "hullMaxs",
        "modelName",
        "absOrigin",
    ]
    counts = {field: 0 for field in fields}
    nonzero = {field: 0 for field in fields}
    examples = {field: [] for field in fields}
    for tree in trees:
        for field in fields:
            if field not in tree:
                continue
            counts[field] += 1
            value = tree[field]
            if isinstance(value, str):
                good = bool(value)
            else:
                good = is_nonzero_vec(value)
            if good:
                nonzero[field] += 1
                if len(examples[field]) < 5:
                    examples[field].append({
                        "x": tree.get("x"),
                        "y": tree.get("y"),
                        "value": value,
                    })

    enriched = []
    for tree in trees:
        item = {
            "x": tree.get("x"),
            "y": tree.get("y"),
            "z": tree.get("z"),
        }
        for field in fields:
            if field in tree:
                item[field] = tree[field]
        enriched.append(item)

    summary = {
        "input": str(Path(args.input).resolve()),
        "treeCount": len(trees),
        "fieldCounts": counts,
        "nonzeroFieldCounts": nonzero,
        "examples": examples,
        "trees": enriched,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(out),
        "treeCount": len(trees),
        "fieldCounts": counts,
        "nonzeroFieldCounts": nonzero,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
