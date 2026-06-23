"""
Board-shape library: a small on-disk collection of imported positions, organised
into user-defined categories (life & death, joseki, ...).

Layout under ~/.katrain/board_library/:
    manifest.json          {"categories": [...], "entries": [ {...}, ... ]}
    thumbs/<id>.png        a thumbnail of each saved capture

Each entry:
    {id, name, category, created, sgf, size, nb, nw}

Pure stdlib + (optional) PIL for thumbnails, so it is fully unit-testable without
a GUI.
"""

from __future__ import annotations

import datetime
import json
import os
import uuid
from typing import Dict, List, Optional

DEFAULT_CATEGORY = "未分类"   # "uncategorised"


class BoardLibrary:
    def __init__(self, root: str):
        self.root = root
        self.thumbs_dir = os.path.join(root, "thumbs")
        self.manifest_path = os.path.join(root, "manifest.json")
        self.data: Dict = {"categories": [DEFAULT_CATEGORY], "entries": []}
        self.load()

    # ------------------------------------------------------------------ io --
    def load(self) -> None:
        try:
            with open(self.manifest_path, encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {"categories": [DEFAULT_CATEGORY], "entries": []}
        self.data.setdefault("categories", [DEFAULT_CATEGORY])
        self.data.setdefault("entries", [])
        if DEFAULT_CATEGORY not in self.data["categories"]:
            self.data["categories"].insert(0, DEFAULT_CATEGORY)

    def save(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.manifest_path)   # atomic on the same volume

    # ---------------------------------------------------------- categories --
    def categories(self) -> List[str]:
        return list(self.data["categories"])

    def add_category(self, name: str) -> Optional[str]:
        name = (name or "").strip()
        if name and name not in self.data["categories"]:
            self.data["categories"].append(name)
            self.save()
            return name
        return None

    def remove_category(self, name: str) -> None:
        """Delete a category; its entries fall back to the default category."""
        if name == DEFAULT_CATEGORY or name not in self.data["categories"]:
            return
        for e in self.data["entries"]:
            if e.get("category") == name:
                e["category"] = DEFAULT_CATEGORY
        self.data["categories"] = [c for c in self.data["categories"] if c != name]
        self.save()

    def rename_category(self, old: str, new: str) -> None:
        new = (new or "").strip()
        if old == DEFAULT_CATEGORY or not new or new in self.data["categories"]:
            return
        self.data["categories"] = [new if c == old else c for c in self.data["categories"]]
        for e in self.data["entries"]:
            if e.get("category") == old:
                e["category"] = new
        self.save()

    # ------------------------------------------------------------- entries --
    def entries(self, category: Optional[str] = None) -> List[Dict]:
        es = self.data["entries"]
        if category is not None:
            es = [e for e in es if e.get("category") == category]
        return sorted(es, key=lambda e: e.get("created", ""), reverse=True)

    def get(self, entry_id: str) -> Optional[Dict]:
        return next((e for e in self.data["entries"] if e["id"] == entry_id), None)

    def thumb_path(self, entry: Dict) -> str:
        return os.path.join(self.thumbs_dir, entry["id"] + ".png")

    def add_entry(self, sgf: str, image=None, name: Optional[str] = None,
                  category: str = DEFAULT_CATEGORY, size: int = 19,
                  nb: int = 0, nw: int = 0) -> Dict:
        entry = {
            "id": uuid.uuid4().hex[:12],
            "name": (name or "棋形").strip() or "棋形",
            "category": category or DEFAULT_CATEGORY,
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "sgf": sgf,
            "size": size,
            "nb": nb,
            "nw": nw,
        }
        if entry["category"] not in self.data["categories"]:
            self.data["categories"].append(entry["category"])
        if image is not None:
            try:
                os.makedirs(self.thumbs_dir, exist_ok=True)
                im = image.copy()
                im.thumbnail((300, 300))
                im.save(self.thumb_path(entry))
            except Exception:
                pass
        self.data["entries"].append(entry)
        self.save()
        return entry

    def remove_entry(self, entry_id: str) -> None:
        e = self.get(entry_id)
        if not e:
            return
        try:
            os.remove(self.thumb_path(e))
        except OSError:
            pass
        self.data["entries"] = [x for x in self.data["entries"] if x["id"] != entry_id]
        self.save()

    def rename_entry(self, entry_id: str, name: str) -> None:
        e = self.get(entry_id)
        if e and (name or "").strip():
            e["name"] = name.strip()
            self.save()

    def set_category(self, entry_id: str, category: str) -> None:
        e = self.get(entry_id)
        if not e:
            return
        if category not in self.data["categories"]:
            self.data["categories"].append(category)
        e["category"] = category
        self.save()


_DEFAULT_LIB: Optional[BoardLibrary] = None


def default_library() -> BoardLibrary:
    """Singleton rooted at ~/.katrain/board_library (matches KaTrain's DATA_FOLDER)."""
    global _DEFAULT_LIB
    if _DEFAULT_LIB is None:
        from katrain.core.constants import DATA_FOLDER
        root = os.path.join(os.path.expanduser(DATA_FOLDER), "board_library")
        _DEFAULT_LIB = BoardLibrary(root)
    return _DEFAULT_LIB
