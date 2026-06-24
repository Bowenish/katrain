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
SEP = "/"                     # folder separator inside a category path (e.g. "jinjin/第一节课")


def norm_path(path: Optional[str]) -> str:
    """Collapse a category path: strip blanks, drop empty segments. '' is the root."""
    parts = [p.strip() for p in (path or "").split(SEP)]
    return SEP.join(p for p in parts if p)


def parent_path(path: str) -> str:
    path = path or ""
    return path.rsplit(SEP, 1)[0] if SEP in path else ""


def leaf_name(path: str) -> str:
    """The last segment of a path, i.e. how a folder is shown in the UI."""
    return (path or "").rsplit(SEP, 1)[-1]


def _ancestors_and_self(path: str) -> List[str]:
    parts = [p for p in (path or "").split(SEP) if p]
    return [SEP.join(parts[: i + 1]) for i in range(len(parts))]


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

    def _all_folders(self) -> set:
        """Every folder path that exists, whether registered explicitly or implied by an entry.

        A category like 'jinjin/第一节课' implies its ancestor folder 'jinjin' even if that
        ancestor was never added on its own."""
        folders = set(self.data["categories"])
        for e in self.data["entries"]:
            folders.update(_ancestors_and_self(e.get("category", "")))
        folders.discard("")
        return folders

    def child_folders(self, parent: str = "") -> List[str]:
        """Immediate sub-folder paths under `parent` ('' = root). Returns full paths, sorted."""
        parent = norm_path(parent)
        prefix = parent + SEP if parent else ""
        children = set()
        for c in self._all_folders():
            if parent:
                if not c.startswith(prefix):
                    continue
                rest = c[len(prefix):]
            else:
                rest = c
            if rest:
                children.add(prefix + rest.split(SEP, 1)[0])
        return sorted(children)

    def add_category(self, name: str) -> Optional[str]:
        """Create a folder at `name` (a path); also registers any missing ancestor folders."""
        name = norm_path(name)
        if not name:
            return None
        created = name not in self.data["categories"]
        for anc in _ancestors_and_self(name):
            if anc not in self.data["categories"]:
                self.data["categories"].append(anc)
        if created:
            self.save()
            return name
        return None

    def remove_category(self, name: str) -> None:
        """Delete a folder and every sub-folder under it; affected entries fall back to default."""
        name = norm_path(name)
        if name == DEFAULT_CATEGORY or not name:
            return
        prefix = name + SEP

        def affected(c: str) -> bool:
            return c == name or c.startswith(prefix)

        for e in self.data["entries"]:
            if affected(e.get("category", "")):
                e["category"] = DEFAULT_CATEGORY
        self.data["categories"] = [c for c in self.data["categories"] if not affected(c)]
        self.save()

    def rename_category(self, old: str, new: str) -> None:
        """Rename a folder, re-prefixing all sub-folders and entries beneath it."""
        old = norm_path(old)
        new = norm_path(new)
        if old == DEFAULT_CATEGORY or not old or not new or old == new:
            return
        prefix = old + SEP

        def remap(c: str) -> str:
            if c == old:
                return new
            if c.startswith(prefix):
                return new + SEP + c[len(prefix):]
            return c

        cats = {remap(c) for c in self.data["categories"]}
        cats.update(_ancestors_and_self(new))
        self.data["categories"] = sorted(cats)
        for e in self.data["entries"]:
            e["category"] = remap(e.get("category", ""))
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
        for anc in _ancestors_and_self(entry["category"]):
            if anc not in self.data["categories"]:
                self.data["categories"].append(anc)
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

    def update_entry(self, entry_id: str, sgf: str, image=None, size: Optional[int] = None,
                     nb: Optional[int] = None, nw: Optional[int] = None) -> Optional[Dict]:
        """Overwrite an existing entry's position in place (sgf + thumbnail + counts).

        Keeps the entry's name and category, so editing a saved board saves back onto the same
        card instead of creating a duplicate."""
        e = self.get(entry_id)
        if not e:
            return None
        e["sgf"] = sgf
        if size is not None:
            e["size"] = size
        if nb is not None:
            e["nb"] = nb
        if nw is not None:
            e["nw"] = nw
        if image is not None:
            try:
                os.makedirs(self.thumbs_dir, exist_ok=True)
                im = image.copy()
                im.thumbnail((300, 300))
                im.save(self.thumb_path(e))
            except Exception:
                pass
        self.save()
        return e

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
        category = norm_path(category) or DEFAULT_CATEGORY
        for anc in _ancestors_and_self(category):
            if anc not in self.data["categories"]:
                self.data["categories"].append(anc)
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
