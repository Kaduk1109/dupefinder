"""
Scenario 6: deleting from the review UI moves files into a Trash subfolder
rather than permanently deleting them immediately, preserving the file's
path relative to its scanned root so files from different folders never
collide (e.g. two different "IMG_0001.jpg" from different subfolders).
"""
import os
import shutil
import time

from config import TRASH_DIRNAME


def trash_path_for(root_path, rel_path):
    return os.path.join(root_path, TRASH_DIRNAME, rel_path)


def move_to_trash(root_path, rel_path, abs_path):
    """Moves abs_path into <root_path>/_DupeFinder_Trash/<rel_path>.
    Returns the new path. Raises on failure (caller should record
    files.status='error' and NOT mark it trashed)."""
    dest = trash_path_for(root_path, rel_path)
    dest_dir = os.path.dirname(dest)
    os.makedirs(dest_dir, exist_ok=True)

    if os.path.exists(dest):
        # Extremely unlikely (would require re-trashing an identical rel_path
        # while an old trashed copy is still sitting there) -- disambiguate
        # rather than silently overwriting a previously trashed file.
        base, ext = os.path.splitext(dest)
        dest = f"{base}.{int(time.time())}{ext}"

    shutil.move(abs_path, dest)
    return dest
